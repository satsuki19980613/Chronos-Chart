"""app/api.py の開示関連メソッド（P4-4）用のテスト。

- estimate_disclosures / get_disclosures / open_disclosure / test_connection
- app/disclosures.py に追加した viewer_url / list_for_symbol

ネットワークは使わない。EDINET の HTTP 呼び出しは monkeypatch で差し替える。
実際にブラウザを開くこともしない（webbrowser.open を monkeypatch で差し替える）。
"""

from __future__ import annotations

import json

import pytest

from app import disclosures
from app.api import Api
from app.database import Database
from app.errors import UserFacingError
from app.service import StockService
from app.settings import Settings
from app.sources import edinet
from app.sources.base import HttpError


def assert_json_ok(result):
    json.dumps(result, allow_nan=False)
    return result


def assert_error(result):
    assert result["ok"] is False
    assert isinstance(result["error"], str) and result["error"] != ""
    json.dumps(result, allow_nan=False)
    return result


# ---------------------------------------------------------------------------
# フィクスチャ
# ---------------------------------------------------------------------------
def _doc(
    doc_id: str,
    submit_at: str,
    category: str = "report",
    doc_type_code: str = "120",
    description: str | None = "説明",
    filer_name: str | None = "テスト株式会社",
    withdrawal: int | None = None,
    reason: str | None = None,
) -> dict:
    """`disclosures.save_documents` に渡す1件分の dict を組み立てる（テスト用の最小セット）。"""
    return {
        "doc_id": doc_id,
        "edinet_code": "E00001",
        "sec_code": "12340",
        "filer_name": filer_name,
        "issuer_edinet_code": None,
        "subject_edinet_code": None,
        "doc_type_code": doc_type_code,
        "form_code": None,
        "ordinance_code": None,
        "description": description,
        "reason": reason,
        "period_start": None,
        "period_end": None,
        "submit_at": submit_at,
        "parent_doc_id": None,
        "withdrawal": withdrawal,
        "disclosure": None,
        "category": category,
    }


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    db.upsert_stock("1234.T", "1234", "テスト株式会社", "東証", "JPY")
    service = StockService(db, fetcher=None, csv_dir=tmp_path / "csv", output_dir=tmp_path / "output")
    settings = Settings(db, keyring_backend=object())
    api = Api(service, settings=settings)
    return api, db, settings


# ---------------------------------------------------------------------------
# viewer_url
# ---------------------------------------------------------------------------
def test_viewer_url_builds_spec_shaped_url():
    assert (
        disclosures.viewer_url("S100Z2KC")
        == "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100Z2KC,,2"
    )


@pytest.mark.parametrize("doc_id", ["../x", "a b", "", "S100Z2KC?x=1", "S100/Z2KC"])
def test_viewer_url_rejects_non_alnum_doc_id(doc_id):
    with pytest.raises(UserFacingError):
        disclosures.viewer_url(doc_id)


# ---------------------------------------------------------------------------
# list_for_symbol
# ---------------------------------------------------------------------------
def test_list_for_symbol_merges_multiple_roles_into_one_item(env):
    _, db, _ = env
    doc = _doc("S1000001", "2026-09-01 09:00")
    disclosures.save_documents(db, [(doc, [("1234.T", "subject"), ("1234.T", "filer")])])

    result = disclosures.list_for_symbol(db, "1234.T")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["doc_id"] == "S1000001"
    assert set(item["roles"]) == {"subject", "filer"}
    assert result["counts"]["total"] == 1


def test_list_for_symbol_includes_reason(env):
    _, db, _ = env
    doc = _doc("S1000001", "2026-09-01 09:00", reason="臨時報告書の提出事由")
    disclosures.save_documents(db, [(doc, [("1234.T", "filer")])])

    result = disclosures.list_for_symbol(db, "1234.T")
    assert result["items"][0]["reason"] == "臨時報告書の提出事由"


def test_list_for_symbol_items_are_sorted_by_submit_at_descending(env):
    _, db, _ = env
    docs = [
        (_doc("S1000001", "2026-09-01 09:00"), [("1234.T", "filer")]),
        (_doc("S1000002", "2026-09-03 09:00"), [("1234.T", "filer")]),
        (_doc("S1000003", "2026-09-02 09:00"), [("1234.T", "filer")]),
    ]
    disclosures.save_documents(db, docs)

    result = disclosures.list_for_symbol(db, "1234.T")
    assert [item["doc_id"] for item in result["items"]] == ["S1000002", "S1000003", "S1000001"]


def test_list_for_symbol_stable_order_for_same_submit_at(env):
    _, db, _ = env
    docs = [
        (_doc("S1000002", "2026-09-01 09:00"), [("1234.T", "filer")]),
        (_doc("S1000001", "2026-09-01 09:00"), [("1234.T", "filer")]),
    ]
    disclosures.save_documents(db, docs)

    result = disclosures.list_for_symbol(db, "1234.T")
    assert [item["doc_id"] for item in result["items"]] == ["S1000001", "S1000002"]


def test_list_for_symbol_counts_by_category_and_withdrawn_and_total(env):
    _, db, _ = env
    docs = [
        (_doc("S1000001", "2026-09-01 09:00", category="report"), [("1234.T", "filer")]),
        (_doc("S1000002", "2026-09-02 09:00", category="supply"), [("1234.T", "filer")]),
        (_doc("S1000003", "2026-09-03 09:00", category="supply"), [("1234.T", "subject"), ("1234.T", "filer")]),
        (_doc("S1000004", "2026-09-04 09:00", category="other"), [("1234.T", "filer")]),
        (_doc("S1000005", "2026-09-05 09:00", category="other", withdrawal=1), [("1234.T", "filer")]),
        (_doc("S1000006", "2026-09-06 09:00", category="other", withdrawal=0), [("1234.T", "filer")]),
    ]
    disclosures.save_documents(db, docs)

    result = disclosures.list_for_symbol(db, "1234.T")
    counts = result["counts"]
    assert counts["report"] == 1
    assert counts["supply"] == 2
    assert counts["other"] == 3
    assert counts["withdrawn"] == 1  # withdrawal=0 は取下げではない
    # total は書類数であって role の延べ数ではない（S1000003 は role が2つ）
    assert counts["total"] == 6


def test_list_for_symbol_no_disclosures_returns_all_zero(env):
    _, db, _ = env
    result = disclosures.list_for_symbol(db, "1234.T")
    assert result == {
        "counts": {"report": 0, "supply": 0, "other": 0, "withdrawn": 0, "total": 0},
        "items": [],
        "fetched_days": 0,
    }


def test_list_for_symbol_fetched_days_distinguishes_not_fetched_from_no_filings(env):
    """0件には「未取得」と「取得したがこの銘柄の提出が無い」の2通りがあり、画面の文言が変わる。"""
    _, db, _ = env
    assert disclosures.list_for_symbol(db, "1234.T")["fetched_days"] == 0

    db.log_fetch("edinet", "2026-09-17", "empty")
    db.log_fetch("edinet", "2026-09-18", "ok")
    db.log_fetch("edinet", "2026-09-19", "error:boom")  # 失敗した日は数えない
    db.log_fetch("karauri", "7203.T", "ok")  # 別ソースは数えない

    result = disclosures.list_for_symbol(db, "1234.T")
    assert result["counts"]["total"] == 0
    assert result["fetched_days"] == 2


# ---------------------------------------------------------------------------
# Api.get_disclosures / estimate_disclosures
# ---------------------------------------------------------------------------
def test_get_disclosures_happy_path(env):
    api, db, _ = env
    doc = _doc("S1000001", "2026-09-01 09:00")
    disclosures.save_documents(db, [(doc, [("1234.T", "filer")])])

    result = assert_json_ok(api.get_disclosures("1234.T"))
    assert result["ok"] is True
    assert result["data"]["counts"]["total"] == 1
    assert result["data"]["items"][0]["doc_id"] == "S1000001"


def test_estimate_disclosures_happy_path(env):
    api, *_ = env
    result = assert_json_ok(api.estimate_disclosures())
    assert result["ok"] is True
    assert "targets" in result["data"]
    assert "api_key_ok" in result["data"]


# ---------------------------------------------------------------------------
# Api.open_disclosure
# ---------------------------------------------------------------------------
def test_open_disclosure_opens_correct_url_exactly_once(env, monkeypatch):
    api, *_ = env
    calls = []
    monkeypatch.setattr("app.api.webbrowser.open", lambda url: calls.append(url))

    result = assert_json_ok(api.open_disclosure("S100Z2KC"))
    assert result["ok"] is True
    assert result["data"] == "https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100Z2KC,,2"
    assert calls == ["https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100Z2KC,,2"]


def test_open_disclosure_invalid_doc_id_returns_error_and_does_not_open_browser(env, monkeypatch):
    api, *_ = env
    calls = []
    monkeypatch.setattr("app.api.webbrowser.open", lambda url: calls.append(url))

    result = assert_error(api.open_disclosure("../evil"))
    assert calls == []


# ---------------------------------------------------------------------------
# Api.test_connection
# ---------------------------------------------------------------------------
def test_test_connection_edinet_missing_api_key(env):
    api, *_ = env  # settings は keyring_backend=object() で API キー未設定
    result = assert_error(api.test_connection("edinet"))
    assert "設定" in result["error"]


def test_test_connection_edinet_success_reports_count_and_date(env, monkeypatch):
    api, db, settings = env
    monkeypatch.setattr(settings, "get_secret", lambda key: "FAKEKEY123" if key == "edinet_api_key" else "")

    calls = []

    def fake_fetch_documents(client, date, api_key, cancel=None):
        calls.append((date, api_key))
        return {"metadata": {"status": "200"}, "results": [{"docID": "S1"}, {"docID": "S2"}]}

    monkeypatch.setattr(edinet, "fetch_documents", fake_fetch_documents)

    result = assert_json_ok(api.test_connection("edinet"))
    assert result["ok"] is True
    assert result["data"]["documents"] == 2
    assert "2" in result["data"]["message"]
    assert len(calls) == 1  # 1回だけ呼ぶ


def test_test_connection_edinet_uses_a_weekday(env, monkeypatch):
    """直近の平日を選ぶこと（土日を渡り歩いて避ける）。"""
    api, db, settings = env
    monkeypatch.setattr(settings, "get_secret", lambda key: "FAKEKEY123" if key == "edinet_api_key" else "")
    monkeypatch.setattr(disclosures, "today_jst", lambda: "2026-09-20")  # 2026-09-20 は日曜日

    calls = []

    def fake_fetch_documents(client, date, api_key, cancel=None):
        calls.append(date)
        return {"metadata": {"status": "200"}, "results": []}

    monkeypatch.setattr(edinet, "fetch_documents", fake_fetch_documents)

    api.test_connection("edinet")
    assert calls == ["2026-09-18"]  # 直近の平日（金曜）


def test_test_connection_edinet_does_not_write_cache_or_fetch_log(env, monkeypatch, tmp_path):
    api, db, settings = env
    monkeypatch.setattr(settings, "get_secret", lambda key: "FAKEKEY123" if key == "edinet_api_key" else "")

    def fake_fetch_documents(client, date, api_key, cancel=None):
        return {"metadata": {"status": "200"}, "results": [{"docID": "S1"}]}

    monkeypatch.setattr(edinet, "fetch_documents", fake_fetch_documents)

    result = api.test_connection("edinet")
    assert result["ok"] is True
    date = result["data"]["date"]
    # fetch_log に記録が無いこと（疎通確認であって取得ではない）
    assert db.get_fetch(edinet.SOURCE, date) is None
    # キャッシュも書かれていないこと
    assert edinet.cached_dates(base_dir=tmp_path) == [] or date not in edinet.cached_dates()


def test_test_connection_edinet_forbidden_error_masks_status_and_no_key_leak(env, monkeypatch):
    api, db, settings = env
    monkeypatch.setattr(settings, "get_secret", lambda key: "FAKEKEY123" if key == "edinet_api_key" else "")

    def fake_fetch_documents(client, date, api_key, cancel=None):
        raise HttpError("edinet がエラーを返しました（HTTP 403）", status=403)

    monkeypatch.setattr(edinet, "fetch_documents", fake_fetch_documents)

    result = assert_error(api.test_connection("edinet"))
    assert "403" in result["error"]
    assert "FAKEKEY123" not in result["error"]
    assert "FAKEKEY123" not in json.dumps(result)


def test_test_connection_edinet_other_error_is_readable_and_does_not_leak_key(env, monkeypatch):
    api, db, settings = env
    monkeypatch.setattr(settings, "get_secret", lambda key: "FAKEKEY123" if key == "edinet_api_key" else "")

    def fake_fetch_documents(client, date, api_key, cancel=None):
        raise HttpError("edinet に接続できませんでした（ConnectionError）")

    monkeypatch.setattr(edinet, "fetch_documents", fake_fetch_documents)

    result = assert_error(api.test_connection("edinet"))
    assert result["error"] != ""
    assert "FAKEKEY123" not in result["error"]


def test_test_connection_gemini_not_implemented(env):
    api, *_ = env
    result = assert_error(api.test_connection("gemini"))
    assert "Gemini" in result["error"]


def test_test_connection_unknown_target(env):
    api, *_ = env
    result = assert_error(api.test_connection("bogus"))
    assert "bogus" in result["error"]


def test_test_connection_without_settings_configured(tmp_path):
    db = Database(tmp_path / "nosettings.db")
    db.init_schema()
    service = StockService(db, fetcher=None, csv_dir=tmp_path / "csv", output_dir=tmp_path / "output")
    api = Api(service)  # settings=None
    result = assert_error(api.test_connection("edinet"))
    assert "設定" in result["error"]
