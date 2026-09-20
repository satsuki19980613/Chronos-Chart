"""EDINET クライアントとコードリストの取り込み（SPEC §2.4.1, §2.4.1a, §2.4.5, §2.4.7）。

ネットワークは使わない。書類一覧はフェイクのセッション、コードリストは合成 CSV から
その場で組み立てた ZIP を使う。`documents.json` のパースや `disclosures` への登録は
このモジュールの範囲外（P4-2・P4-3）。
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import pytest

from app.database import Database
from app.errors import Cancelled, UserFacingError
from app.sources import edinet
from app.sources.base import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"
CODE_LIST_CSV = FIXTURES / "edinet_codes_synthetic.csv"


def _zip_bytes(member_name: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member_name, content)
    return buf.getvalue()


def _code_list_zip() -> bytes:
    return _zip_bytes(edinet.CODE_LIST_MEMBER, CODE_LIST_CSV.read_bytes())


def _minimal_code_list_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    lines = ["ダウンロード実行日,2026年09月20日現在,件数,{}件".format(len(rows))]
    lines.append(",".join(headers))
    for row in rows:
        lines.append(",".join(row))
    return ("\r\n".join(lines) + "\r\n").encode("cp932")


FULL_HEADERS = [
    "ＥＤＩＮＥＴコード", "提出者種別", "上場区分", "連結の有無", "資本金", "決算日",
    "提出者名", "提出者名（英字）", "提出者名（ヨミ）", "所在地", "提出者業種",
    "証券コード", "提出者法人番号",
]


# ---------- フィクスチャ自体の性質 ----------


def test_fixture_keeps_cp932_and_crlf():
    """合成 CSV は実物どおり cp932・CRLF であること（SPEC §2.4.1a、§10.1）。

    `.gitattributes` で `-text` にしていないと、チェックアウト時に改行が LF へ正規化されて
    実物と構造が変わってしまう。それを検知するためのテスト。
    """
    raw = CODE_LIST_CSV.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n"), "CRLF でない改行が混ざっている"
    raw.decode("cp932")  # cp932 で読めること（読めなければここで落ちる）
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")  # UTF-8 ではないこと（全角文字を含むため）


# ---------- parse_code_list ----------


def test_parse_code_list_skips_download_info_line_and_uses_second_line_as_header():
    rows = edinet.parse_code_list(_code_list_zip())
    assert rows, "行が1つも取れていない"
    by_code = {r["edinet_code"]: r for r in rows}
    row = by_code["E00001"]
    assert row["sec_code"] == "13010"
    assert row["name"] == "架空商事株式会社"


def test_parse_code_list_empty_sec_code_becomes_none():
    rows = edinet.parse_code_list(_code_list_zip())
    by_code = {r["edinet_code"]: r for r in rows}
    assert by_code["E00003"]["sec_code"] is None
    assert by_code["E00004"]["sec_code"] is None


def test_parse_code_list_keeps_alphanumeric_sec_code_as_string():
    rows = edinet.parse_code_list(_code_list_zip())
    by_code = {r["edinet_code"]: r for r in rows}
    assert by_code["E00005"]["sec_code"] == "409A0"
    assert isinstance(by_code["E00005"]["sec_code"], str)


def test_parse_code_list_missing_required_column_raises_and_returns_nothing():
    headers_without_sec_code = [h for h in FULL_HEADERS if h != "証券コード"]
    row = ["E00099", "内国法人・組合", "上場", "有", "1", "03-31", "テスト", "Test", "テスト", "東京都", "業種", "9999999999999"]
    content = _minimal_code_list_csv(headers_without_sec_code, [row])
    with pytest.raises(UserFacingError):
        edinet.parse_code_list(_zip_bytes(edinet.CODE_LIST_MEMBER, content))


def test_parse_code_list_missing_member_in_zip_raises():
    zip_bytes = _zip_bytes("SomeOtherFile.csv", b"dummy")
    with pytest.raises(UserFacingError):
        edinet.parse_code_list(zip_bytes)


def test_parse_code_list_bad_zip_raises():
    with pytest.raises(UserFacingError):
        edinet.parse_code_list(b"not a zip file at all")


# ---------- save_code_list ----------


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "edinet.db")
    db.init_schema()
    return db


def _all_edinet_codes(db: Database) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM edinet_codes ORDER BY edinet_code").fetchall()
    return [dict(r) for r in rows]


def test_save_code_list_full_replace(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO edinet_codes (edinet_code, sec_code, name) VALUES (?, ?, ?)",
            ("E99999", "99990", "旧データ株式会社"),
        )

    rows = edinet.parse_code_list(_code_list_zip())
    result = edinet.save_code_list(db, rows)

    assert result["saved"] == len(rows)
    saved = _all_edinet_codes(db)
    codes = {r["edinet_code"] for r in saved}
    assert "E99999" not in codes, "全置換のはずが前のデータが残っている"
    assert "E00001" in codes
    assert len(saved) == len(rows)


def test_save_code_list_does_not_touch_fetch_log(tmp_path):
    """fetch_log は「取得」の記録なので、保存だけでは書かない（記録は fetch_and_save 側）。"""
    db = _db(tmp_path)
    rows = edinet.parse_code_list(_code_list_zip())
    edinet.save_code_list(db, rows)
    assert db.get_fetch("edinet_codes", "list") is None


# ---------- edinet_code_for ----------


def test_edinet_code_for_matches_four_digit_code_plus_zero(tmp_path):
    db = _db(tmp_path)
    rows = edinet.parse_code_list(_code_list_zip())
    edinet.save_code_list(db, rows)
    db.upsert_stock("9876.T", "9876", "テスト銘柄", "東証", "JPY")
    assert edinet.edinet_code_for(db, "9876.T") is None  # 未登録の証券コード

    db.upsert_stock("5555.T", "5555", "五桁商事", "東証", "JPY")
    assert edinet.edinet_code_for(db, "5555.T") == "E00006"


def test_edinet_code_for_non_domestic_symbol_returns_none(tmp_path):
    db = _db(tmp_path)
    rows = edinet.parse_code_list(_code_list_zip())
    edinet.save_code_list(db, rows)
    assert edinet.edinet_code_for(db, "AAPL") is None


def test_edinet_code_for_unregistered_sec_code_returns_none(tmp_path):
    db = _db(tmp_path)
    rows = edinet.parse_code_list(_code_list_zip())
    edinet.save_code_list(db, rows)
    assert edinet.edinet_code_for(db, "9999.T") is None


# ---------- fetch_and_save_code_list ----------


class _FakeResponse:
    def __init__(self, content: bytes = b"", json_data: dict | None = None, status_code: int = 200):
        self.content = content
        self._json = json_data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._json


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers, timeout))
        return self.response


def _client_with(response, source="edinet"):
    session = _FakeSession(response)
    client = HttpClient(
        source=source,
        min_interval=0,
        agent="ChronosChart-test",
        session=session,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    return client, session


def test_fetch_and_save_code_list_end_to_end(tmp_path):
    db = _db(tmp_path)
    client, session = _client_with(_FakeResponse(content=_code_list_zip()))
    result = edinet.fetch_and_save_code_list(db, client=client)
    assert result["saved"] > 0
    assert session.calls[0][0] == edinet.CODE_LIST_URL
    logged = db.get_fetch("edinet_codes", "list")
    assert logged["result"] == "ok"


def test_fetch_and_save_code_list_logs_error_and_reraises_on_parse_failure(tmp_path):
    db = _db(tmp_path)
    client, _ = _client_with(_FakeResponse(content=b"not a zip"))
    with pytest.raises(UserFacingError):
        edinet.fetch_and_save_code_list(db, client=client)
    logged = db.get_fetch("edinet_codes", "list")
    assert logged["result"].startswith("error:")


def test_fetch_and_save_code_list_cancelled_does_not_log_as_error(tmp_path):
    import threading

    db = _db(tmp_path)
    cancel = threading.Event()
    cancel.set()
    client, session = _client_with(_FakeResponse(content=_code_list_zip()))
    with pytest.raises(Cancelled):
        edinet.fetch_and_save_code_list(db, cancel=cancel, client=client)
    assert session.calls == []
    assert db.get_fetch("edinet_codes", "list") is None


# ---------- fetch_documents ----------


def test_fetch_documents_uses_spec_url_and_params():
    payload = {"metadata": {"status": "200", "message": ""}, "results": []}
    client, session = _client_with(_FakeResponse(json_data=payload))
    result = edinet.fetch_documents(client, "2026-09-20", "secret-key-value")
    assert result == payload
    url, params, headers, timeout = session.calls[0]
    assert url == edinet.DOCUMENTS_URL
    assert params["date"] == "2026-09-20"
    assert params["type"] == 2
    assert params["Subscription-Key"] == "secret-key-value"


def test_fetch_documents_non_200_status_raises_with_message():
    payload = {"metadata": {"status": "404", "message": "存在しない日付です"}}
    client, _ = _client_with(_FakeResponse(json_data=payload))
    with pytest.raises(UserFacingError, match="存在しない日付です"):
        edinet.fetch_documents(client, "2026-09-20", "secret-key-value")


def test_fetch_documents_empty_api_key_raises():
    client, session = _client_with(_FakeResponse(json_data={"metadata": {"status": "200"}}))
    with pytest.raises(UserFacingError):
        edinet.fetch_documents(client, "2026-09-20", "")
    assert session.calls == [], "キーが無い時点で弾かれ、通信していないこと"


def test_fetch_documents_does_not_leak_api_key_in_logs(caplog):
    """URL をログに出す箇所で、API キーの値が現れず `Subscription-Key=***` になっていること。"""
    secret = "sk-super-secret-value-12345"
    payload = {"metadata": {"status": "200", "message": ""}, "results": []}
    client, _ = _client_with(_FakeResponse(json_data=payload))
    with caplog.at_level(logging.INFO):
        edinet.fetch_documents(client, "2026-09-20", secret)
    assert secret not in caplog.text
    assert "Subscription-Key=***" in caplog.text


# ---------- make_client ----------


def test_make_client_uses_edinet_source_and_min_interval():
    client = edinet.make_client()
    assert isinstance(client, HttpClient)
    assert client.source == "edinet"
    assert client.min_interval >= 1.0


def test_make_client_keeps_429_retryable():
    client = edinet.make_client()
    assert 429 not in client.no_retry_statuses


# ---------- 日次キャッシュ（SPEC §2.4.2） ----------


def test_write_cache_read_cache_roundtrip_with_japanese(tmp_path):
    data = {"metadata": {"status": "200", "message": ""}, "results": [{"docDescription": "有価証券報告書"}]}
    edinet.write_cache("2026-09-20", data, base_dir=tmp_path)
    assert edinet.read_cache("2026-09-20", base_dir=tmp_path) == data


def test_write_cache_is_byte_identical_for_same_content(tmp_path):
    data = {"metadata": {"status": "200", "message": ""}, "results": []}
    edinet.write_cache("2026-09-20", data, base_dir=tmp_path)
    first = edinet.cache_path("2026-09-20", base_dir=tmp_path).read_bytes()
    edinet.write_cache("2026-09-20", data, base_dir=tmp_path)
    second = edinet.cache_path("2026-09-20", base_dir=tmp_path).read_bytes()
    assert first == second


def test_read_cache_corrupted_file_returns_none(tmp_path):
    path = edinet.cache_path("2026-09-20", base_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not gzip")
    assert edinet.read_cache("2026-09-20", base_dir=tmp_path) is None


def test_read_cache_missing_date_returns_none(tmp_path):
    assert edinet.read_cache("2026-09-20", base_dir=tmp_path) is None


def test_cache_path_rejects_invalid_date(tmp_path):
    with pytest.raises(UserFacingError):
        edinet.cache_path("2026-13-99", base_dir=tmp_path)
    with pytest.raises(UserFacingError):
        edinet.cache_path("../evil", base_dir=tmp_path)


def test_cache_path_rejects_date_without_zero_padding():
    """`2026-9-1` を通すと、同じ日のキャッシュが2つのファイル名で作られ fetch_log のキーともずれる。"""
    with pytest.raises(UserFacingError):
        edinet.cache_path("2026-9-1")


def test_read_cache_truncated_gzip_returns_none(tmp_path):
    """途中で切れた gzip は EOFError になる。壊れたキャッシュは無いものとして扱う（SPEC §2.4.2）。"""
    edinet.write_cache("2026-09-20", {"metadata": {}, "results": [{"docID": "S100ABCD"}]}, base_dir=tmp_path)
    path = edinet.cache_path("2026-09-20", base_dir=tmp_path)
    path.write_bytes(path.read_bytes()[:-5])
    assert edinet.read_cache("2026-09-20", base_dir=tmp_path) is None


def test_cached_dates_sorted_ascending_and_ignores_unrelated_files(tmp_path):
    edinet.write_cache("2026-09-20", {"metadata": {}, "results": []}, base_dir=tmp_path)
    edinet.write_cache("2026-01-05", {"metadata": {}, "results": []}, base_dir=tmp_path)
    (tmp_path / "readme.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / "not-a-date.json.gz").write_bytes(b"dummy")
    assert edinet.cached_dates(base_dir=tmp_path) == ["2026-01-05", "2026-09-20"]


def test_cached_dates_returns_empty_list_when_dir_missing(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert edinet.cached_dates(base_dir=missing) == []


def test_write_cache_leaves_no_temp_file_behind(tmp_path):
    edinet.write_cache("2026-09-20", {"metadata": {}, "results": []}, base_dir=tmp_path)
    entries = list(tmp_path.iterdir())
    assert [e.name for e in entries] == ["2026-09-20.json.gz"]


# ---------- fetch_day ----------


def test_fetch_day_empty_results_is_empty_but_writes_cache(tmp_path):
    db = _db(tmp_path)
    payload = {"metadata": {"status": "200", "message": ""}, "results": []}
    client, session = _client_with(_FakeResponse(json_data=payload))
    result = edinet.fetch_day(db, "2026-09-20", "secret-key", client=client, base_dir=tmp_path / "cache")

    assert result["result"] == "empty"
    assert result["documents"] == 0
    assert edinet.read_cache("2026-09-20", base_dir=tmp_path / "cache") == payload
    logged = db.get_fetch(edinet.SOURCE, "2026-09-20")
    assert logged["result"] == "empty"


def test_fetch_day_with_results_is_ok_and_logged(tmp_path):
    db = _db(tmp_path)
    payload = {
        "metadata": {"status": "200", "message": ""},
        "results": [{"docID": "S100ABCD"}, {"docID": "S100EFGH"}],
    }
    client, session = _client_with(_FakeResponse(json_data=payload))
    result = edinet.fetch_day(db, "2026-09-20", "secret-key", client=client, base_dir=tmp_path / "cache")

    assert result["result"] == "ok"
    assert result["documents"] == 2
    logged = db.get_fetch(edinet.SOURCE, "2026-09-20")
    assert logged["result"] == "ok"


def test_fetch_day_http_error_logs_error_and_reraises(tmp_path):
    db = _db(tmp_path)
    payload = {"metadata": {"status": "404", "message": "存在しない日付です"}}
    client, session = _client_with(_FakeResponse(json_data=payload))
    with pytest.raises(UserFacingError):
        edinet.fetch_day(db, "2026-09-20", "secret-key", client=client, base_dir=tmp_path / "cache")

    logged = db.get_fetch(edinet.SOURCE, "2026-09-20")
    assert logged["result"].startswith("error:")


def test_fetch_day_cancelled_does_not_log(tmp_path):
    import threading

    db = _db(tmp_path)
    cancel = threading.Event()
    cancel.set()
    payload = {"metadata": {"status": "200", "message": ""}, "results": []}
    client, session = _client_with(_FakeResponse(json_data=payload))
    with pytest.raises(Cancelled):
        edinet.fetch_day(db, "2026-09-20", "secret-key", cancel=cancel, client=client, base_dir=tmp_path / "cache")

    assert session.calls == []
    assert db.get_fetch(edinet.SOURCE, "2026-09-20") is None


def test_fetch_day_error_log_does_not_leak_api_key(tmp_path):
    db = _db(tmp_path)
    secret = "sk-super-secret-value-98765"
    payload = {"metadata": {"status": "404", "message": "存在しない日付です"}}
    client, session = _client_with(_FakeResponse(json_data=payload))
    with pytest.raises(UserFacingError):
        edinet.fetch_day(db, "2026-09-20", secret, client=client, base_dir=tmp_path / "cache")

    logged = db.get_fetch(edinet.SOURCE, "2026-09-20")
    assert secret not in logged["result"]
