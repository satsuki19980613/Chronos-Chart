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
        # (名前, 関数)。関数は (ctx, stocks) -> {"summary": str, ...}。需給・開示は後続フェーズでここに足す
        self.steps: list[tuple[str, Callable[[JobContext, list[dict]], dict]]] = [("prices", self._update_prices)]

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
            "updated_symbols": updated,
            "skipped": skipped,
            "added": added,
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
