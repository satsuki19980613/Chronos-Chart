"""pywebview から JavaScript に公開する API。

JS 側では window.pywebview.api.<メソッド名>() で呼び出す。
戻り値はすべて {"ok": bool, "data" | "error": ...} の形にそろえる。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from . import disclosures
from .ai import analyze
from .ai import quota as ai_quota_mod
from .ai import report as ai_report
from .ai.client import GeminiClient
from .config import REPORTS_DIR
from .errors import UserFacingError
from .fetcher import FetchError
from .jobs import JobManager
from .service import StockService
from .settings import Settings
from .sources import edinet, karauri, taisyaku
from .sources.base import HttpError

log = logging.getLogger(__name__)


def _response(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": func(*args, **kwargs)}
        except (FetchError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("api error in %s", func.__name__)
            return {"ok": False, "error": f"予期しないエラーが発生しました: {exc}"}

    return wrapper


class Api:
    def __init__(self, service: StockService, jobs: JobManager | None = None, settings: Settings | None = None):
        # 先頭が _ の属性は JS に公開されない
        self._service = service
        self._jobs = jobs or JobManager()
        self._settings = settings

    @_response
    def search(self, query: str):
        return self._service.search(query)

    @_response
    def register(self, symbol: str, name: str, exchange: str | None = None):
        return self._service.register(symbol, name, exchange)

    @_response
    def update(self, symbol: str):
        return self._service.update(symbol)

    @_response
    def update_all(self):
        return self._service.update_all()

    @_response
    def delete(self, symbol: str):
        self._service.delete(symbol)
        return True

    @_response
    def list_stocks(self):
        return self._service.list_stocks()

    @_response
    def dashboard(self, symbol: str):
        return self._service.dashboard(symbol)

    @_response
    def export(self, symbols: list[str], fmt: str, days: int | None = None):
        return self._service.export(symbols, fmt, days)

    @_response
    def list_exports(self):
        return self._service.list_exports()

    # ---------- 設定 ----------
    @_response
    def get_settings(self):
        """API キーはマスク済み。平文は reveal_secret でのみ返す。"""
        return self._require_settings().public_view()

    @_response
    def save_settings(self, values: dict):
        settings = self._require_settings()
        settings.update(values)
        return settings.public_view()

    @_response
    def reveal_secret(self, key: str):
        """設定画面の「表示」ボタン用。ユーザーの明示操作でのみ呼ぶこと。"""
        return self._require_settings().get_secret(key)

    def _require_settings(self) -> Settings:
        if self._settings is None:
            raise ValueError("設定機能が初期化されていません")
        return self._settings

    # ---------- ジョブ（長時間処理）----------
    @_response
    def start_job(self, kind: str, params: dict | None = None):
        return self._jobs.start(kind, params)

    @_response
    def job_status(self, job_id: str):
        return self._jobs.status(job_id)

    @_response
    def cancel_job(self, job_id: str):
        return self._jobs.cancel(job_id)

    @_response
    def active_jobs(self):
        return self._jobs.active()

    # ---------- 需給データの取得（P2-6。SPEC §2.2.4・§2.3.2）----------
    @_response
    def fetch_short(self, symbol: str):
        """単一銘柄の空売り残高を取得する（ブロッキング）。再取得の抑止は掛からない。"""
        return karauri.fetch_one(self._service.db, self._require_settings(), symbol)

    @_response
    def fetch_taisyaku(self):
        """日証金の貸借取引残高を1回取得する（ブロッキング。全銘柄分が1回で入る）。"""
        return taisyaku.fetch_and_save(self._service.db, self._require_settings())

    @_response
    def estimate_short_all(self, symbols: list[str] | None = None):
        """空売り残高の一括取得の事前見積り（画面の確認ダイアログ用）。"""
        return karauri.estimate(self._service.db, self._require_settings(), symbols)

    @_response
    def open_csv_folder(self):
        return _open_folder(Path(self._service.csv_dir))

    @_response
    def open_output_folder(self):
        return _open_folder(Path(self._service.output_dir))

    # ---------- 開示（P4-4。SPEC §2.4.6・§4.2）----------
    @_response
    def estimate_disclosures(self, redo_days: int = 0):
        """開示取得の事前見積り（画面の確認ダイアログ用）。"""
        return disclosures.estimate(
            self._service.db, self._require_settings(), redo_days=int(redo_days or 0)
        )

    @_response
    def get_disclosures(self, symbol: str):
        """銘柄に紐づく開示の一覧と分類別件数。"""
        return disclosures.list_for_symbol(self._service.db, symbol)

    @_response
    def open_disclosure(self, doc_id: str):
        """EDINET の書類閲覧ページを OS 既定のブラウザで開くだけ（SPEC §2.4.7。中身は取得しない）。"""
        url = disclosures.viewer_url(doc_id)
        webbrowser.open(url)
        return url

    @_response
    def get_disclosure_text(self, doc_id: str):
        """開示モーダル用に書類本文をテキストで返す（P9-2。SPEC §2.4.8）。

        テキストにできない・大きすぎる・404 等のときは例外にせず `available=False` と
        日本語の `reason` を返す（画面はこれを見て「EDINET で開く」に誘導する）。
        401/403/429 はキーの誤りやレート制限の問題なので、`UserFacingError` として投げて
        画面にエラー扱いで出す（_response が {"ok": False, "error": ...} にする）。
        """
        settings = self._require_settings()
        api_key = settings.get_secret("edinet_api_key")
        if not api_key:
            raise UserFacingError("EDINET の API キーが設定されていません。設定タブで登録してください")

        url = disclosures.viewer_url(doc_id)
        client = edinet.make_client(settings)

        def unavailable(reason: str, title: str | None = None) -> dict:
            return {
                "doc_id": doc_id,
                "available": False,
                "reason": reason,
                "title": title,
                "text": "",
                "truncated": False,
                "url": url,
            }

        try:
            result = edinet.fetch_document_text(client, doc_id, api_key)
        except edinet.DocumentTooLarge as exc:
            mb = exc.size_bytes / (1024 * 1024)
            return unavailable(
                f"書類が大きいため本文を表示しません（約 {mb:.1f} MB）。EDINET の閲覧ページで確認してください。"
            )
        except HttpError as exc:
            if exc.status in (401, 403, 429):
                raise UserFacingError(
                    f"EDINET に拒否されました（HTTP {exc.status}）。API キーやアクセス回数を確認してください"
                ) from None
            return unavailable(f"本文を取得できませんでした（{exc}）。EDINET の閲覧ページで確認してください。")

        if not result["text"]:
            return unavailable(
                "この書類は本文をテキストにできません。EDINET の閲覧ページで確認してください。",
                title=result["title"],
            )

        return {
            "doc_id": doc_id,
            "available": True,
            "reason": None,
            "title": result["title"],
            "text": result["text"],
            "truncated": result["truncated"],
            "url": url,
        }

    # ---------- AI 分析（P6-6。SPEC §2.7・§4.2）----------
    @_response
    def ai_quota(self):
        """本日の使用量・残量と打ち切りフラグ（SPEC §2.7.2）。"""
        return ai_quota_mod.Quota(self._service.db, self._require_settings()).snapshot()

    @_response
    def ai_reset_exhausted(self):
        """打ち切りフラグの手動解除（SPEC §2.1.3・§2.7.2）。誤判定に備えた逃げ道。"""
        cleared = ai_quota_mod.reset_exhausted(self._service.db)
        return {"cleared": cleared}

    @_response
    def ai_estimate(self, symbol: str, days: int):
        """実行前の確認ダイアログ用の見積り（SPEC §2.7.1）。

        送れないときも例外にせず `can_run: false` と `reason` で返す（理由を画面に出すため）。
        """
        return analyze.estimate(self._service.db, self._require_settings(), symbol, int(days))

    @_response
    def list_reports(self):
        """生成済みレポートの一覧（新しい順）。"""
        return ai_report.list_reports(self._service.db)

    @_response
    def open_report(self, report_id: int):
        """レポートの HTML を OS 既定のアプリで開く。"""
        row = ai_report.get_report(self._service.db, int(report_id))
        path = Path(row["path"])
        if not path.exists():
            raise UserFacingError(f"レポートのファイルが見つかりません: {path}")
        webbrowser.open(path.as_uri())
        return {"path": str(path)}

    @_response
    def open_reports_folder(self):
        return _open_folder(REPORTS_DIR)

    @_response
    def test_connection(self, target: str):
        """データソースへの疎通確認（SPEC §2.1.3）。"""
        if target == "edinet":
            return _test_edinet_connection(self._require_settings())
        if target == "gemini":
            return _test_gemini_connection(self._service.db, self._require_settings())
        raise UserFacingError(f"不明な接続テスト対象です: {target}")


def _last_weekday(date: str) -> str:
    """`date`（`YYYY-MM-DD`）以前で直近の平日を返す（土日を遡って避ける）。"""
    d = datetime.strptime(date, "%Y-%m-%d")
    while d.weekday() >= 5:  # 5=土, 6=日
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _test_edinet_connection(settings: Settings) -> dict:
    """EDINET への疎通確認。`fetch_documents` を1回だけ呼び、キャッシュにも fetch_log にも書かない。"""
    api_key = settings.get_secret("edinet_api_key")
    if not api_key:
        raise UserFacingError("EDINET の API キーが設定されていません")

    date = _last_weekday(disclosures.today_jst())
    client = edinet.make_client(settings)
    try:
        data = edinet.fetch_documents(client, date, api_key)
    except HttpError as exc:
        if exc.status in (401, 403):
            raise UserFacingError(
                f"EDINET に拒否されました（HTTP {exc.status}）。API キーが正しいか確認してください"
            ) from None
        raise

    count = len(data.get("results") or [])
    message = f"EDINET に接続できました（{date} の書類 {count} 件）"
    if count == 0:
        # 疎通は取れている。当日の朝など、まだ公表前だと 0 件で返る
        message += "。この日の書類はまだ公表されていない可能性があります"
    return {"target": "edinet", "date": date, "documents": count, "message": message}


def _test_gemini_connection(db, settings: Settings) -> dict:
    """Gemini への疎通確認。最小のプロンプトを1回だけ送る。

    上限（RPM/TPM/RPD）が未設定でもテストできるように `Quota.check` は通さないが、
    **送信は1回ぶん実際に消費する**ので `ai_usage.requests` には必ず加算する
    （CLAUDE.md の不変条件13: 送信を試みるたびに加算する）。
    """
    client = GeminiClient.from_settings(settings)  # キー・モデル未設定はここで UserFacingError
    ai_quota_mod.record_request(db, client.model)
    reply = client.ping()
    ai_quota_mod.record_tokens(db, client.model, reply.usage.prompt_tokens, reply.usage.output_tokens)
    return {
        "target": "gemini",
        "model": client.model,
        "message": f"Gemini に接続できました（{client.model}・入出力 {reply.usage.total_tokens} トークン）",
    }


def _open_folder(folder: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(folder)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return str(folder)
