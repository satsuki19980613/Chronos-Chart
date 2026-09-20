"""起動時の自動更新（SPEC §2.8.2）。

アプリを起動するたびに1回だけ、登録銘柄のデータを差分更新する。ジョブ（auto_update）として
バックグラウンドで走り、失敗しても起動や操作を妨げない。常駐ポーリングはしない。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

from .database import Database
from .errors import Cancelled
from .jobs import JobContext
from .service import StockService
from .settings import Settings
from .sources import karauri, taisyaku
from .sources.base import HttpError

log = logging.getLogger(__name__)

PRICE_PAUSE_SEC = 1.0  # 株価取得の銘柄間の待機
# 通信できない状態で全銘柄分の失敗を待たないよう、連続して失敗したらそのソースを打ち切る。
# 1回で打ち切らないのは、上場廃止など銘柄固有の失敗で残りの銘柄まで更新されなくなるのを避けるため
MAX_CONSECUTIVE_FAILURES = 2


class AutoUpdater:
    def __init__(
        self,
        db: Database,
        service: StockService,
        settings: Settings,
        now: Callable[[], datetime] = datetime.now,
    ):
        self.db = db
        self.service = service
        self.settings = settings
        self._now = now
        self._ran = False
        self._lock = threading.Lock()
        # (名前, 関数)。関数は (ctx, stocks) -> {"summary": str, ...}。SPEC §2.8.2 の順で並べる。
        # ("edinet", ...) は P4-5 でここ（taisyaku の前）に入る
        self.steps: list[tuple[str, Callable[[JobContext, list[dict]], dict]]] = [
            ("prices", self._update_prices),
            # ("edinet", self._update_edinet),  # P4-5 で追加
            ("taisyaku", self._update_taisyaku),
            ("short", self._update_short),
        ]

    def run(self, ctx: JobContext, params: dict) -> dict:
        """auto_update ジョブの本体。params["force"] が真なら設定と実行済みフラグを無視する。"""
        force = bool(params.get("force"))
        with self._lock:
            if self._ran and not force:
                return {"skipped": "already_ran"}  # 起動時の1回だけ。画面の再読込では走らせない
            self._ran = True
        if not force and not self.settings.get("auto_update_on_start"):
            return {"skipped": "disabled"}
        stocks = self.db.list_stocks()
        if not stocks:
            return {"skipped": "no_stocks"}

        steps: dict[str, dict] = {}
        for name, step in self.steps:
            ctx.check()
            try:
                steps[name] = step(ctx, stocks)
            except Cancelled:
                raise
            except Exception as exc:  # 1つのソースの失敗で全体を止めない
                log.exception("auto update step %s failed", name)
                steps[name] = {"summary": f"{name}: 失敗", "failed": True, "errors": [str(exc)]}
        return {
            "steps": steps,
            "summary": "／".join(s["summary"] for s in steps.values() if s.get("summary")),
            "failed": any(s.get("failed") for s in steps.values()),
            "changed": any(s.get("changed") for s in steps.values()),
            "updated_symbols": sorted({sym for s in steps.values() for sym in s.get("updated_symbols", [])}),
        }

    # ---------- 株価（yfinance）----------
    def _update_prices(self, ctx: JobContext, stocks: list[dict]) -> dict:
        updated, skipped, errors, added = [], 0, [], 0
        failures_in_a_row = 0
        aborted = False
        total = len(stocks)
        for i, stock in enumerate(stocks):
            symbol = stock["symbol"]
            ctx.progress(i, total, f"株価を更新中 {i + 1}/{total}")
            if self._fetched_recently("yahoo", symbol):
                skipped += 1
                continue
            if updated or errors:
                ctx.wait(PRICE_PAUSE_SEC)
            else:
                ctx.check()
            try:
                result = self.service.update(symbol)
            except Exception as exc:
                log.warning("auto update failed for %s: %s", symbol, exc)
                errors.append(f"{symbol}: {exc}")
                failures_in_a_row += 1
                if failures_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                    aborted = True
                    break
                continue
            failures_in_a_row = 0
            updated.append(symbol)
            added += result.get("added", 0)
        ctx.progress(total, total, "株価の更新が完了")

        if aborted:
            summary = f"株価 取得できず（{len(updated)}件更新後に中止）"
        elif not updated and not errors:
            summary = "株価 取得済み"
        else:
            summary = f"株価 {len(updated)}件更新" + (f"・{len(errors)}件失敗" if errors else "")
        return {
            "summary": summary,
            "failed": bool(errors),
            "changed": bool(updated),
            "updated_symbols": updated,
            "skipped": skipped,
            "added": added,
            "errors": errors,
        }

    # ---------- 貸借取引残高（日証金）----------
    def _update_taisyaku(self, ctx: JobContext, stocks: list[dict]) -> dict:
        domestic = [s["symbol"] for s in stocks if s["symbol"].endswith(".T")]
        if not domestic:
            return {"summary": "日証金 対象銘柄なし", "failed": False, "changed": False, "updated_symbols": []}
        if self._fetched_recently("taisyaku", "zandaka"):
            return {"summary": "日証金 取得済み", "failed": False, "changed": False, "updated_symbols": []}

        ctx.progress(0, 1, "貸借取引残高を取得中")
        try:
            result = taisyaku.fetch_and_save(self.db, self.settings, symbols=domestic, cancel=ctx.cancel)
        except Cancelled:
            raise
        except Exception as exc:
            log.warning("auto update: taisyaku failed: %s", exc)
            return {
                "summary": "日証金 失敗",
                "failed": True,
                "changed": False,
                "updated_symbols": [],
                "errors": [str(exc)],
            }
        ctx.progress(1, 1, "貸借取引残高の取得が完了")

        missing = set(result.get("missing") or [])
        updated_symbols = [symbol for symbol in domestic if symbol not in missing]
        if updated_symbols:
            summary = f"日証金 {result['date']} 分を取得（{len(updated_symbols)}銘柄）"
        else:
            # 登録銘柄がどれも貸借銘柄でない場合。ファイル自体は取れているので失敗ではない。
            # この分岐では save() が申込日を返さない（result["date"] は None）ので日付を出さない
            summary = "日証金 取得（登録銘柄の行なし）"
        return {
            "summary": summary,
            "failed": False,
            "changed": bool(updated_symbols),
            "updated_symbols": updated_symbols,
        }

    # ---------- 空売り残高（karauri.net）----------
    def _update_short(self, ctx: JobContext, stocks: list[dict]) -> dict:
        if not self.settings.get("auto_update_short"):
            return {"summary": "空売り スキップ（設定オフ）", "failed": False, "changed": False, "updated_symbols": []}
        contact = str(self.settings.get("scrape_contact") or "").strip()
        if not contact:
            return {
                "summary": "空売り スキップ（連絡先が未設定）",
                "failed": False,
                "changed": False,
                "updated_symbols": [],
            }

        targets, _skipped = karauri.select_targets(self.db, self.settings, force=False)
        total = len(targets)
        if total == 0:
            return {"summary": "空売り 取得済み", "failed": False, "changed": False, "updated_symbols": []}

        client = karauri.make_client(self.settings)
        updated: list[str] = []
        errors: list[str] = []
        aborted: str | None = None
        consecutive_failures = 0
        for i, symbol in enumerate(targets):
            ctx.progress(i, total, f"空売り残高を取得中 {i + 1}/{total}")
            ctx.check()
            try:
                karauri.fetch_one(self.db, self.settings, symbol, cancel=ctx.cancel, client=client)
            except Cancelled:
                raise
            except HttpError as exc:
                errors.append(f"{symbol}: {exc}")
                # 403/429 は拒否の意思表示。リトライも次の銘柄への続行もしない（SPEC §2.2.4）
                if exc.status in (403, 429):
                    aborted = "forbidden"
                    break
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            consecutive_failures = 0
            updated.append(symbol)
        ctx.progress(total, total, "空売り残高の取得が完了")

        if aborted == "forbidden":
            summary = f"空売り アクセスを拒否されたため中止（{len(updated)}件取得後）"
        elif aborted == "failures":
            summary = f"空売り 取得できず（{len(updated)}件取得後に中止）"
        elif not updated and not errors:
            summary = "空売り 取得済み"
        else:
            summary = f"空売り {len(updated)}件更新" + (f"・{len(errors)}件失敗" if errors else "")

        return {
            "summary": summary,
            "failed": bool(errors),
            "changed": bool(updated),
            "updated_symbols": updated,
            "errors": errors,
        }

    def _fetched_recently(self, source: str, key: str) -> bool:
        """auto_update_min_interval_min 以内に取得に成功していれば True。"""
        minutes = self.settings.get("auto_update_min_interval_min")
        row = self.db.get_fetch(source, key)
        if minutes <= 0 or row is None or row["result"] != "ok":
            return False
        try:
            fetched_at = datetime.strptime(row["fetched_at"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return self._now() - fetched_at < timedelta(minutes=minutes)
