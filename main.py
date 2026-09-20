"""Chronos Chart の起動スクリプト。

    python main.py          # 通常起動
    python main.py --debug  # 開発者ツール付きで起動
"""

from __future__ import annotations

import argparse
import logging
from logging.handlers import RotatingFileHandler

import webview

from app import __version__
from app.api import Api
from app.config import CSV_DIR, DB_PATH, LOG_DIR, WEB_DIR
from app.database import Database
from app.fetcher import YahooFetcher
from app.jobs import JobManager, selftest_job
from app.service import StockService


def setup_logging(debug: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Chronos Chart - テクニカル分析ツール")
    parser.add_argument("--debug", action="store_true", help="開発者ツールを有効にする")
    args = parser.parse_args()

    setup_logging(args.debug)

    db = Database(DB_PATH)
    schema_changed = db.init_schema()
    service = StockService(db, YahooFetcher(), CSV_DIR)
    rebuilt = service.rebuild_all(only_missing_csv=not schema_changed)
    if rebuilt:
        logging.getLogger(__name__).info("recomputed indicators/CSV for %d stocks", rebuilt)
    jobs = JobManager()
    if args.debug:
        jobs.register("selftest", selftest_job)
    api = Api(service, jobs)

    webview.create_window(
        f"Chronos Chart {__version__}",
        str(WEB_DIR / "index.html"),
        js_api=api,
        width=1440,
        height=920,
        min_size=(1100, 700),
        background_color="#0f1420",
    )
    webview.start(debug=args.debug)


if __name__ == "__main__":
    main()
