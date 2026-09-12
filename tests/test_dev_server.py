"""dev_server.py の HTTP エンドポイントの検証（実ネットワーク・実ブラウザ不使用）。

ThreadingHTTPServer を空きポートでインプロセス起動し、urllib ではなく http.client を
直接使う（4xx で例外を投げず、応答が来ない/接続が切れるケースも観測できるようにするため）。
"""

from __future__ import annotations

import http.client
import json
import threading
from functools import partial
from http.server import ThreadingHTTPServer

import numpy as np
import pandas as pd
import pytest
from conftest import make_prices

import dev_server
from app.api import Api
from app.config import WEB_DIR
from app.database import Database
from app.fetcher import FetchError, SearchResult
from app.service import StockService


class FakeFetcher:
    """ネットワークを使わずに yfinance の代わりをする（tests/test_service.py と同じ方針）。"""

    def __init__(self, prices: pd.DataFrame):
        self.prices = prices
        self.calls = []

    def search(self, query):
        return [SearchResult("7203.T", "Toyota", "東証", "EQUITY")]

    def fetch_currency(self, symbol):
        return "JPY"

    def fetch_history(self, symbol, period=None, start=None):
        self.calls.append({"period": period, "start": start})
        df = self.prices if start is None else self.prices[self.prices["date"] >= start]
        if df.empty:
            raise FetchError("no data")
        return df.reset_index(drop=True)


@pytest.fixture
def server(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    prices = make_prices(100 + np.sin(np.arange(220) / 8) * 10)
    fetcher = FakeFetcher(prices)
    service = StockService(db, fetcher, tmp_path / "csv", tmp_path / "output")
    api = Api(service)

    handler_cls = partial(dev_server.make_handler(api), directory=str(WEB_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": httpd.server_port, "httpd": httpd, "fetcher": fetcher, "service": service}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# ヘルパー: urllib ではなく http.client を直接使う
# ---------------------------------------------------------------------------
def _get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        conn.close()


def _post(port: int, path: str, body: bytes):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        try:
            parsed = json.loads(data) if data else None
        except json.JSONDecodeError:
            parsed = None  # 404 等、JSON でないエラーページの場合
        return resp.status, parsed
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 静的ファイル配信
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path, content_type_prefix",
    [
        ("/", "text/html"),
        ("/index.html", "text/html"),
        ("/css/style.css", "text/css"),
        ("/js/app.js", "text/javascript"),
        ("/vendor/lightweight-charts.standalone.production.js", "text/javascript"),
    ],
)
def test_static_files_served(server, path, content_type_prefix):
    status, headers, body = _get(server["port"], path)
    assert status == 200
    assert headers.get("content-type", "").startswith(content_type_prefix)
    assert len(body) > 0


def test_server_binds_to_loopback_only(server):
    assert server["httpd"].server_address[0] == "127.0.0.1"


# ---------------------------------------------------------------------------
# API 呼び出し（正常系）
# ---------------------------------------------------------------------------
def test_list_stocks_empty(server):
    status, res = _post(server["port"], "/api/list_stocks", b"[]")
    assert status == 200
    assert res == {"ok": True, "data": []}


def test_register_dashboard_export_end_to_end(server):
    port = server["port"]

    status, res = _post(port, "/api/register", json.dumps(["7203.T", "Toyota", "東証"]).encode())
    assert status == 200 and res["ok"] is True
    assert res["data"]["added"] == 220
    assert res["data"]["stock"]["symbol"] == "7203.T"

    status, res = _post(port, "/api/list_stocks", b"[]")
    assert status == 200 and res["ok"] is True
    assert [s["symbol"] for s in res["data"]] == ["7203.T"]

    status, res = _post(port, "/api/dashboard", json.dumps(["7203.T"]).encode())
    assert status == 200 and res["ok"] is True
    data = res["data"]
    assert data["stock"]["symbol"] == "7203.T"
    assert len(data["chart"]["dates"]) == 220
    assert "cards" in data and "signals" in data and "table" in data

    status, res = _post(port, "/api/export", json.dumps([["7203.T"], "csv", 20]).encode())
    assert status == 200 and res["ok"] is True
    assert res["data"]["rows"] == 20
    assert res["data"]["symbols"] == ["7203.T"]

    status, res = _post(port, "/api/export", json.dumps([["7203.T"], "markdown", None]).encode())
    assert status == 200 and res["ok"] is True
    assert res["data"]["format"] == "markdown"

    status, res = _post(port, "/api/list_exports", b"[]")
    assert status == 200 and res["ok"] is True
    assert len(res["data"]) == 2


# ---------------------------------------------------------------------------
# セキュリティ / 堅牢性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method_name", ["_service", "__init__", "nonexistent"])
def test_private_and_nonexistent_methods_are_404(server, method_name):
    status, _ = _post(server["port"], f"/api/{method_name}", b"[]")
    assert status == 404


@pytest.mark.parametrize(
    "path",
    [
        "/../app/config.py",
        "/%2e%2e/app/config.py",
        "/../../app/config.py",
        "/../dev_server.py",
    ],
)
def test_path_traversal_blocked(server, path):
    status, headers, body = _get(server["port"], path)
    assert status != 200
    # 設定ファイルの中身（BASE_DIR / DATA_DIR 等）が漏れていないことも確認する
    assert b"BASE_DIR" not in body
    assert b"AUTOTECHNICAL_DATA_DIR" not in body


def test_wrong_argument_count_returns_graceful_error_not_crash(server):
    """引数が足りない/多すぎる場合、Api._response が例外を吸収して ok:false を返す（クラッシュしない）。"""
    port = server["port"]
    status, res = _post(port, "/api/search", b"[]")  # search(query) に対して引数0個
    assert status == 200
    assert res["ok"] is False

    status, res = _post(port, "/api/search", json.dumps(["a", "b", "c", "d"]).encode())
    assert status == 200
    assert res["ok"] is False

    # サーバースレッド自体は生きている
    status, res = _post(port, "/api/list_stocks", b"[]")
    assert status == 200 and res == {"ok": True, "data": []}


def test_server_survives_malformed_json_request(server):
    """不正な JSON は 400 で拒否され、その後もサーバーは応答し続ける。"""
    port = server["port"]
    status, _ = _post(port, "/api/list_stocks", b"not json")
    assert status == 400

    status, res = _post(port, "/api/list_stocks", b"[]")
    assert status == 200 and res == {"ok": True, "data": []}


def test_malformed_json_body_should_return_400(server):
    status, _ = _post(server["port"], "/api/list_stocks", b"not json")
    assert status == 400


def test_object_body_should_be_rejected_not_silently_reinterpreted(server):
    port = server["port"]
    status, res = _post(port, "/api/search", json.dumps({"query": "7203"}).encode())
    # 配列でないボディは 400 で拒否される（以前は dict のキーが引数として渡されていた）
    assert status == 400
