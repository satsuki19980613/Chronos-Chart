"""開発用: 画面をブラウザで確認するための簡易サーバー。

    python dev_server.py            # http://127.0.0.1:8765/?dev を開く
    python dev_server.py --port 9000

pywebview の代わりに HTTP 経由で同じ Api クラスを呼び出す。
ローカル (127.0.0.1) でのみ待ち受ける。通常の利用は main.py を使うこと。
"""

from __future__ import annotations

import argparse
import json
import logging
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from app.api import Api
from app.config import CSV_DIR, DB_PATH, WEB_DIR
from app.database import Database
from app.fetcher import YahooFetcher
from app.service import StockService


def make_handler(api: Api):
    class Handler(SimpleHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            method = self.path.removeprefix("/api/")
            func = getattr(api, method, None) if not method.startswith("_") else None
            if func is None or not callable(func):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            args = json.loads(self.rfile.read(length) or b"[]")
            body = json.dumps(func(*args), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *args):
            logging.getLogger("dev_server").debug(fmt, *args)

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    db = Database(DB_PATH)
    db.init_schema()
    api = Api(StockService(db, YahooFetcher(), CSV_DIR))

    handler = partial(make_handler(api), directory=str(WEB_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"http://127.0.0.1:{args.port}/?dev")
    server.serve_forever()


if __name__ == "__main__":
    main()
