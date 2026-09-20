"""pywebview から JavaScript に公開する API。

JS 側では window.pywebview.api.<メソッド名>() で呼び出す。
戻り値はすべて {"ok": bool, "data" | "error": ...} の形にそろえる。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from functools import wraps
from pathlib import Path

from .fetcher import FetchError
from .jobs import JobManager
from .service import StockService
from .settings import Settings
from .sources import karauri, taisyaku

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


def _open_folder(folder: Path) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(folder)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])
    return str(folder)
