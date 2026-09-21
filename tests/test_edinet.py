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


# ---------- 書類本文の取得・テキスト化（P9-1。SPEC §2.4.8） ----------


def _document_zip(members: dict[str, bytes]) -> bytes:
    """`{ZIP 内パス: 中身}` から ZIP のバイト列を作る（本文取得のテスト用）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            zf.writestr(name, content)
    return buf.getvalue()


_HEADER_HTML = (
    "<html><head><title>臨時報告書</title></head><body>"
    "<script>var x = 1;</script><style>.a{color:red}</style>"
    "<p>提出会社</p>"
    "</body></html>"
).encode("utf-8")
_HONBUN_HTML = (
    "<html><body><p>1 概要</p><p></p><p></p><p>本文</p></body></html>"
).encode("utf-8")


@pytest.fixture(autouse=True)
def _clear_document_cache_around_each_test():
    edinet.clear_document_cache()
    yield
    edinet.clear_document_cache()


class _FakeStreamResponse(_FakeResponse):
    """`max_bytes` の検証用。`headers` を持ち、`.content` へのアクセスを検知できる。"""

    def __init__(self, content: bytes = b"", headers: dict | None = None, status_code: int = 200):
        super().__init__(content=content, status_code=status_code)
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True


class _FakeStreamSession:
    """`stream=True` を受け取れる、Content-Length チェック用のフェイクセッション。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None, stream=None):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout, "stream": stream})
        return self.response


def _stream_client_with(response):
    session = _FakeStreamSession(response)
    client = HttpClient(
        source="edinet",
        min_interval=0,
        agent="ChronosChart-test",
        session=session,
        clock=lambda: 0.0,
        sleep=lambda s: None,
    )
    return client, session


# ---- extract_document_text ----


def test_extract_document_text_concatenates_files_in_ascending_filename_order():
    zip_bytes = _document_zip(
        {
            "XBRL/PublicDoc/0101010_honbun_x_ixbrl.htm": _HONBUN_HTML,
            "XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML,
            "XBRL/PublicDoc/manifest_PublicDoc.xml": b"<manifest/>",
        }
    )
    result = edinet.extract_document_text(zip_bytes)
    assert result["title"] == "臨時報告書"
    header_pos = result["text"].index("提出会社")
    honbun_pos = result["text"].index("本文")
    assert header_pos < honbun_pos, "0000000 (表紙) より 0101010 (本文) が後に来ていない"
    assert result["truncated"] is False


def test_extract_document_text_strips_script_and_style_content():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    result = edinet.extract_document_text(zip_bytes)
    assert "var x = 1" not in result["text"]
    assert "color:red" not in result["text"]


def test_extract_document_text_collapses_consecutive_blank_lines():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0101010_honbun_x_ixbrl.htm": _HONBUN_HTML})
    result = edinet.extract_document_text(zip_bytes)
    assert "\n\n\n" not in result["text"]


def test_extract_document_text_no_htm_in_public_doc_returns_empty():
    zip_bytes = _document_zip({"XBRL/PublicDoc/manifest_PublicDoc.xml": b"<manifest/>"})
    result = edinet.extract_document_text(zip_bytes)
    assert result == {"title": None, "text": "", "truncated": False}


def test_extract_document_text_prefers_public_doc_over_other_folders():
    """PublicDoc がある限り、監査報告書などの他フォルダは読まない。"""
    zip_bytes = _document_zip(
        {
            "XBRL/AuditDoc/0000000_header_x_ixbrl.htm": "<html><body>監査</body></html>".encode("utf-8"),
            "XBRL/PublicDoc/0101010_honbun_x_ixbrl.htm": "<html><body>本文</body></html>".encode("utf-8"),
        }
    )
    result = edinet.extract_document_text(zip_bytes)
    assert result["text"] == "本文"


def test_extract_document_text_accepts_public_doc_without_xbrl_prefix():
    """確認書の ZIP は `PublicDoc/…`（`XBRL/` が付かない）だった（2026-09-21 実データで確認）。"""
    zip_bytes = _document_zip(
        {
            "PublicDoc/0000000_header.htm": "<html><body>表紙</body></html>".encode("utf-8"),
            "PublicDoc/0101010_e02778-00.htm": "<html><body>確認した旨</body></html>".encode("utf-8"),
        }
    )
    result = edinet.extract_document_text(zip_bytes)
    assert result["text"] == "表紙" + chr(10) * 2 + "確認した旨"


def test_extract_document_text_falls_back_to_any_html_when_no_public_doc():
    """PublicDoc が無い ZIP でも、htm があればテキストにする（何も出ないよりよい）。"""
    zip_bytes = _document_zip(
        {
            "XBRL/AuditDoc/0000000_header_x_ixbrl.htm": "<html><body>監査</body></html>".encode("utf-8"),
        }
    )
    result = edinet.extract_document_text(zip_bytes)
    assert result["text"] == "監査"


def test_extract_document_text_returns_empty_when_no_html_at_all():
    zip_bytes = _document_zip({"XBRL/PublicDoc/manifest_PublicDoc.xml": b"<manifest/>"})
    result = edinet.extract_document_text(zip_bytes)
    assert result == {"title": None, "text": "", "truncated": False}


def test_extract_document_text_not_a_zip_returns_empty_without_raising():
    result = edinet.extract_document_text(b"not a zip file at all")
    assert result == {"title": None, "text": "", "truncated": False}


def test_extract_document_text_truncates_over_max_chars():
    long_body = "あ" * (edinet.DOCUMENT_MAX_CHARS + 1000)
    html = f"<html><body><p>{long_body}</p></body></html>".encode("utf-8")
    zip_bytes = _document_zip({"XBRL/PublicDoc/0101010_honbun_x_ixbrl.htm": html})
    result = edinet.extract_document_text(zip_bytes)
    assert result["truncated"] is True
    assert len(result["text"]) == edinet.DOCUMENT_MAX_CHARS


# ---- fetch_document_zip ----


def test_fetch_document_zip_uses_spec_url_and_params_via_get():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, session = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    result = edinet.fetch_document_zip(client, "S100YMVN", "secret-key-value")
    assert result == zip_bytes
    call = session.calls[0]
    assert call["url"] == edinet.DOCUMENT_URL.format(doc_id="S100YMVN")
    assert call["params"] == {"type": 1, "Subscription-Key": "secret-key-value"}


def test_fetch_document_zip_rejects_non_alnum_doc_id():
    client, session = _stream_client_with(_FakeStreamResponse(content=b""))
    with pytest.raises(UserFacingError):
        edinet.fetch_document_zip(client, "../evil", "secret-key-value")
    assert session.calls == [], "doc_id を検証する前に通信してしまっている"


def test_fetch_document_zip_empty_api_key_raises():
    client, session = _stream_client_with(_FakeStreamResponse(content=b""))
    with pytest.raises(UserFacingError):
        edinet.fetch_document_zip(client, "S100YMVN", "")
    assert session.calls == []


def test_fetch_document_zip_does_not_leak_api_key_in_logs(caplog):
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, _ = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    secret = "sk-super-secret-doc-value"
    with caplog.at_level(logging.INFO):
        edinet.fetch_document_zip(client, "S100YMVN", secret)
    assert secret not in caplog.text
    assert "Subscription-Key=***" in caplog.text


def test_fetch_document_zip_too_large_aborts_without_reading_body():
    """`Content-Length` だけで中止する。`.content` には（実物とは桁違いに小さい）ダミーしか入れていない。"""
    response = _FakeStreamResponse(
        content=b"x" * 10,
        headers={"Content-Length": str(edinet.DOCUMENT_MAX_BYTES + 1)},
    )
    client, session = _stream_client_with(response)
    with pytest.raises(edinet.DocumentTooLarge) as err:
        edinet.fetch_document_zip(client, "S100YMVN", "secret-key-value")
    assert err.value.size_bytes == edinet.DOCUMENT_MAX_BYTES + 1
    assert response.closed is True, "本体を読まずに中止するなら接続を閉じているはず"
    assert session.calls[0]["stream"] is True, "ヘッダだけ見るために stream=True で呼んでいるはず"


def test_fetch_document_zip_within_limit_succeeds():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    response = _FakeStreamResponse(content=zip_bytes, headers={"Content-Length": str(len(zip_bytes))})
    client, _ = _stream_client_with(response)
    result = edinet.fetch_document_zip(client, "S100YMVN", "secret-key-value")
    assert result == zip_bytes


# ---- fetch_document_text / キャッシュ ----


def test_fetch_document_text_returns_extracted_result():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, session = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    result = edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    assert result["title"] == "臨時報告書"
    assert result["truncated"] is False


def test_fetch_document_text_does_not_write_fetch_log(tmp_path):
    """表示のたびに叩く操作であって取得履歴ではないので fetch_log には書かない。"""
    db = _db(tmp_path)
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, _ = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM fetch_log").fetchall()
    assert rows == []


def test_fetch_document_text_caches_and_calls_http_once():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, session = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    first = edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    second = edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    assert first == second
    assert len(session.calls) == 1


def test_fetch_document_text_lru_evicts_oldest_after_17_entries():
    def client_for(doc_id: str):
        zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
        client, session = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
        return client, session

    first_client, first_session = client_for("S100000001")
    edinet.fetch_document_text(first_client, "S100000001", "secret-key-value")

    for i in range(2, 18):  # S100000002 〜 S100000017（16件追加。合計17件目で先頭が落ちる）
        doc_id = f"S1000000{i:02d}"
        client, _ = client_for(doc_id)
        edinet.fetch_document_text(client, doc_id, "secret-key-value")

    # 最初のものが呼び直されれば HTTP が再度飛ぶはず
    edinet.fetch_document_text(first_client, "S100000001", "secret-key-value")
    assert len(first_session.calls) == 2, "17件目の追加で最初のキャッシュが落ちていない"


def test_clear_document_cache_forces_refetch():
    zip_bytes = _document_zip({"XBRL/PublicDoc/0000000_header_x_ixbrl.htm": _HEADER_HTML})
    client, session = _stream_client_with(_FakeStreamResponse(content=zip_bytes))
    edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    edinet.clear_document_cache()
    edinet.fetch_document_text(client, "S100YMVN", "secret-key-value")
    assert len(session.calls) == 2


# ---------- 財務数値（type=5 CSV）の取得・パース（P11-1。SPEC §2.9） ----------
#
# 実データ（`tests/fixtures/real/*.zip`）の中身はここには持ち込まない。すべて自分で組み立てた
# 合成データ（会社名は「合成商事」のような架空のもの）。列の並びだけを実物どおりにする。

_FINANCIAL_CSV_HEADER = [
    "要素ID", "項目名", "コンテキストID", "相対年度", "連結・個別", "期間・時点", "ユニットID", "単位", "値",
]


def _financial_csv(rows: list[list[str]]) -> bytes:
    """行のリストから、実物どおり UTF-16（BOM 付き）・タブ区切りの CSV バイト列を組み立てる。"""
    lines = ["\t".join(_FINANCIAL_CSV_HEADER)]
    lines.extend("\t".join(row) for row in rows)
    return ("\n".join(lines) + "\n").encode("utf-16")


def _financial_zip(
    csv_bytes: bytes | None,
    member_name: str = "XBRL_TO_CSV/jpcrp030000-asr-001_E99999-000_2026-03-31_01_2026-06-01.csv",
    extra_members: dict[str, bytes] | None = None,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        if csv_bytes is not None:
            zf.writestr(member_name, csv_bytes)
        for name, content in (extra_members or {}).items():
            zf.writestr(name, content)
    return buf.getvalue()


def _dei_row(element: str, value: str) -> list[str]:
    """DEI の1行（実測では常に `FilingDateInstant` コンテキスト）。"""
    return [f"jpdei_cor:{element}", "架空の項目名", "FilingDateInstant", "", "その他", "", "", "", value]


def _fact_row(element: str, ctx: str, value: str, unit_id: str = "JPY") -> list[str]:
    """「主要な経営指標等の推移」の1行。`連結・個別` 列は実測どおり常に `その他` にする。"""
    return [f"jpcrp_cor:{element}", "架空の項目名", ctx, "", "その他", "", unit_id, "", value]


# 有報（FY）・日本基準・合成商事の DEI。決算日はキリのいい 2026-03-31（月末保持のテストとは別会社）
_FY_JGAAP_DEI = [
    _dei_row("AccountingStandardsDEI", "Japan GAAP"),
    _dei_row("TypeOfCurrentPeriodDEI", "FY"),
    _dei_row("CurrentFiscalYearEndDateDEI", "2026-03-31"),
    _dei_row("PreviousFiscalYearEndDateDEI", "2025-03-31"),
    _dei_row("CurrentPeriodEndDateDEI", "2026-03-31"),
    _dei_row("WhetherConsolidatedFinancialStatementsArePreparedDEI", "true"),
]

# 有報（FY）・IFRS・合成商事
_FY_IFRS_DEI = [
    _dei_row("AccountingStandardsDEI", "IFRS"),
    _dei_row("TypeOfCurrentPeriodDEI", "FY"),
    _dei_row("CurrentFiscalYearEndDateDEI", "2026-03-31"),
    _dei_row("PreviousFiscalYearEndDateDEI", "2025-03-31"),
    _dei_row("CurrentPeriodEndDateDEI", "2026-03-31"),
    _dei_row("WhetherConsolidatedFinancialStatementsArePreparedDEI", "true"),
]

# 半期報（HY）・日本基準・合成商事
_HY_JGAAP_DEI = [
    _dei_row("AccountingStandardsDEI", "Japan GAAP"),
    _dei_row("TypeOfCurrentPeriodDEI", "HY"),
    _dei_row("CurrentFiscalYearEndDateDEI", "2027-01-31"),
    _dei_row("PreviousFiscalYearEndDateDEI", "2026-01-31"),
    _dei_row("CurrentPeriodEndDateDEI", "2026-07-31"),
    _dei_row("ComparativePeriodEndDateDEI", "2025-07-31"),
]

_HY_MEMBER_NAME = "XBRL_TO_CSV/jpcrp040300-ssr-001_E99999-000_2026-07-31_01_2026-09-01.csv"


def _parse(rows: list[list[str]], member_name: str | None = None) -> dict:
    csv_bytes = _financial_csv(rows)
    if member_name is None:
        return edinet.parse_financial_csv(_financial_zip(csv_bytes))
    return edinet.parse_financial_csv(_financial_zip(csv_bytes, member_name=member_name))


# ---- 1. 日本基準・連結あり ----


def test_parse_financial_csv_jgaap_consolidated_five_periods():
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "500000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior1YearDuration", "480000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior2YearDuration", "460000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior3YearDuration", "440000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior4YearDuration", "420000000"),
    ]
    result = _parse(rows)
    assert result["standard"] == "jgaap"
    revenue_rows = [r for r in result["rows"] if r["item"] == "revenue"]
    assert len(revenue_rows) == 5
    assert all(r["basis"] == "consolidated" for r in revenue_rows)
    assert all(r["period_type"] == "FY" for r in revenue_rows)
    assert sorted(r["period_end"] for r in revenue_rows) == [
        "2022-03-31", "2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31",
    ]


# ---- 2. IFRS。要素IDの罠（EquityToAssetRatioIFRS→bps） ----


def test_parse_financial_csv_ifrs_maps_items_and_avoids_the_equity_ratio_trap():
    rows = _FY_IFRS_DEI + [
        _fact_row("RevenueIFRSSummaryOfBusinessResults", "CurrentYearDuration", "300000000"),
        _fact_row(
            "EquityToAssetRatioIFRSSummaryOfBusinessResults",
            "CurrentYearInstant",
            "3057.72",
            unit_id="JPYPerShares",
        ),
    ]
    result = _parse(rows)
    assert result["standard"] == "ifrs"
    items = {r["item"] for r in result["rows"]}
    assert "revenue" in items
    assert "bps" in items, "EquityToAssetRatioIFRS... は要素IDに反して bps（1株当たり親会社所有者帰属持分）"
    assert "equity_ratio" not in items, "自己資本比率に誤って割り当てられている"
    bps_row = next(r for r in result["rows"] if r["item"] == "bps")
    assert bps_row["unit"] == "JPY/share"
    assert bps_row["value"] == pytest.approx(3057.72)


# ---- 3. 連結が1行も無い書類 ----


def test_parse_financial_csv_no_consolidated_rows_falls_back_to_nonconsolidated():
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetIncomeLossSummaryOfBusinessResults", "CurrentYearDuration_NonConsolidatedMember", "50000000"),
        _fact_row("TotalAssetsSummaryOfBusinessResults", "CurrentYearInstant_NonConsolidatedMember", "900000000"),
    ]
    result = _parse(rows)
    assert result["consolidated_available"] is False
    assert result["rows"], "単体側の行も出ないと会社側にデータが全く無いことになってしまう"
    assert all(r["basis"] == "nonconsolidated" for r in result["rows"])
    assert {r["item"] for r in result["rows"]} == {"net_income", "total_assets"}


# ---- 4. セグメント別（_NonConsolidatedMember 以外の接尾辞）は現れない ----


def test_parse_financial_csv_drops_segment_member_rows():
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration_ConsumerSegmentMember", "999999"),
    ]
    result = _parse(rows)
    assert result["rows"] == []
    assert result["consolidated_available"] is False


# ---- 5. 半期報のコンテキスト対応 ----


def test_parse_financial_csv_half_year_context_mapping():
    rows = _HY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "InterimDuration", "150000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior1InterimDuration", "140000000"),
        _fact_row("NetSalesSummaryOfBusinessResults", "Prior1YearDuration", "290000000"),
    ]
    result = _parse(rows, member_name=_HY_MEMBER_NAME)
    assert result["period_type"] == "HY"
    by_period = {(r["period_end"], r["period_type"]) for r in result["rows"]}
    assert ("2026-07-31", "HY") in by_period, "InterimDuration は CurrentPeriodEndDateDEI"
    assert ("2025-07-31", "HY") in by_period, "Prior1InterimDuration は ComparativePeriodEndDateDEI"
    assert ("2026-01-31", "FY") in by_period, (
        "半期報の Prior1YearDuration は前事業年度そのもの（offset 0）で、period_type は FY"
    )


# ---- 6. うるう年（月末を保つ） ----


def test_parse_financial_csv_prior_year_keeps_month_end_across_leap_year():
    leap_dei = [
        _dei_row("AccountingStandardsDEI", "Japan GAAP"),
        _dei_row("TypeOfCurrentPeriodDEI", "FY"),
        _dei_row("CurrentFiscalYearEndDateDEI", "2026-02-28"),
        _dei_row("PreviousFiscalYearEndDateDEI", "2025-02-28"),
        _dei_row("CurrentPeriodEndDateDEI", "2026-02-28"),
    ]
    rows = leap_dei + [
        _fact_row("NetAssetsSummaryOfBusinessResults", "Prior2YearInstant", "100000000"),
    ]
    result = _parse(rows)
    assert len(result["rows"]) == 1
    assert result["rows"][0]["period_end"] == "2024-02-29"


# ---- 7. 比率の100倍・PER は100倍しない ----


def test_parse_financial_csv_percent_items_scaled_by_100_but_per_is_not():
    rows = _FY_JGAAP_DEI + [
        _fact_row("EquityToAssetRatioSummaryOfBusinessResults", "CurrentYearInstant", "0.638", unit_id="pure"),
        _fact_row("RateOfReturnOnEquitySummaryOfBusinessResults", "CurrentYearDuration", "0.052", unit_id="pure"),
        _fact_row("PayoutRatioSummaryOfBusinessResults", "CurrentYearDuration", "0.066", unit_id="pure"),
        _fact_row("PriceEarningsRatioSummaryOfBusinessResults", "CurrentYearInstant", "11.7", unit_id="pure"),
    ]
    result = _parse(rows)
    by_item = {r["item"]: r for r in result["rows"]}
    assert by_item["equity_ratio"]["value"] == pytest.approx(63.8)
    assert by_item["equity_ratio"]["unit"] == "%"
    assert by_item["roe"]["value"] == pytest.approx(5.2)
    assert by_item["payout_ratio"]["value"] == pytest.approx(6.6)
    assert by_item["per"]["value"] == pytest.approx(11.7), "PER は100倍しない"
    assert by_item["per"]["unit"] == "times"


def test_parse_financial_csv_percent_scaling_rounds_away_binary_float_remainder():
    """`0.29 * 100` は `28.999999999999996` になる（2進浮動小数の端数）。丸めて `29.0` ちょうどにする。

    丸めが効いているかを確かめるのが目的なので、`pytest.approx` ではなく厳密な `==` で検証する
    （メインのレビュー指摘・2026-09-21。実測で自己資本比率 `0.29` に端数が出た）。
    """
    rows = _FY_JGAAP_DEI + [
        _fact_row("EquityToAssetRatioSummaryOfBusinessResults", "CurrentYearInstant", "0.29", unit_id="pure"),
    ]
    result = _parse(rows)
    equity_ratio = next(r for r in result["rows"] if r["item"] == "equity_ratio")
    assert equity_ratio["value"] == 29.0


# ---- 8. 「－」の行は現れない ----


def test_parse_financial_csv_missing_value_marker_is_dropped():
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "－"),
    ]
    result = _parse(rows)
    assert result["rows"] == []


# ---- 9. 連結採用でも dps/payout_ratio/shares_outstanding/capital_stock は単体側から補う ----


def test_parse_financial_csv_supplements_company_wide_items_from_nonconsolidated():
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "500000000"),  # 連結を確定させる
        _fact_row(
            "DividendPaidPerShareSummaryOfBusinessResults",
            "CurrentYearDuration_NonConsolidatedMember",
            "25",
            unit_id="JPYPerShares",
        ),
        _fact_row(
            "PayoutRatioSummaryOfBusinessResults",
            "CurrentYearDuration_NonConsolidatedMember",
            "0.3",
            unit_id="pure",
        ),
        _fact_row(
            "TotalNumberOfIssuedSharesSummaryOfBusinessResults",
            "CurrentYearInstant_NonConsolidatedMember",
            "1000000",
            unit_id="shares",
        ),
        _fact_row(
            "CapitalStockSummaryOfBusinessResults",
            "CurrentYearInstant_NonConsolidatedMember",
            "10000000",
        ),
    ]
    result = _parse(rows)
    assert result["consolidated_available"] is True
    by_item = {r["item"]: r for r in result["rows"]}
    for item in ("dps", "payout_ratio", "shares_outstanding", "capital_stock"):
        assert item in by_item, f"{item} が単体側から補われていない"
        assert by_item[item]["basis"] == "consolidated", "補った行も basis は採用した basis にする"
    assert by_item["dps"]["value"] == pytest.approx(25)
    assert by_item["payout_ratio"]["value"] == pytest.approx(30.0)


# ---- 10. 壊れた ZIP・本体無し・空の CSV ----


def test_parse_financial_csv_bad_zip_returns_empty_rows_without_raising():
    result = edinet.parse_financial_csv(b"not a zip file at all")
    assert result == {"standard": None, "consolidated_available": False, "period_type": None, "rows": []}


def test_parse_financial_csv_no_body_member_returns_empty_rows():
    """`jpaud-*`（監査報告書）しか入っていない ZIP は本体と誤認しない（10・11 を兼ねる）。"""
    zip_bytes = _financial_zip(
        None,
        extra_members={
            "XBRL_TO_CSV/jpaud-aai-cc-001_E99999-000_2026-03-31_01_2026-06-01.csv": _financial_csv(_FY_JGAAP_DEI),
        },
    )
    result = edinet.parse_financial_csv(zip_bytes)
    assert result == {"standard": None, "consolidated_available": False, "period_type": None, "rows": []}


def test_parse_financial_csv_empty_csv_returns_empty_rows():
    empty_csv = ("\t".join(_FINANCIAL_CSV_HEADER) + "\n").encode("utf-16")
    result = edinet.parse_financial_csv(_financial_zip(empty_csv))
    assert result == {"standard": None, "consolidated_available": False, "period_type": None, "rows": []}


# ---- フォールバック要素（revenue の OperatingRevenue1、net_income の NetIncomeLoss） ----


def test_parse_financial_csv_prefers_primary_element_over_fallback():
    rows = _FY_JGAAP_DEI + [
        _fact_row("OperatingRevenue1SummaryOfBusinessResults", "CurrentYearDuration", "111111"),
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "222222"),
    ]
    result = _parse(rows)
    revenue = next(r for r in result["rows"] if r["item"] == "revenue")
    assert revenue["value"] == pytest.approx(222222)


def test_parse_financial_csv_uses_fallback_element_when_primary_is_absent():
    rows = _FY_JGAAP_DEI + [
        _fact_row("OperatingRevenue1SummaryOfBusinessResults", "CurrentYearDuration", "111111"),
    ]
    result = _parse(rows)
    revenue = next(r for r in result["rows"] if r["item"] == "revenue")
    assert revenue["value"] == pytest.approx(111111)


# ---- 未知の要素ID・想定外の単位 ----


def test_parse_financial_csv_drops_unmapped_element_ids_silently():
    rows = _FY_JGAAP_DEI + [
        _fact_row("SomeUnknownElementSummaryOfBusinessResults", "CurrentYearDuration", "12345"),
    ]
    result = _parse(rows)
    assert result["rows"] == []


def test_parse_financial_csv_unexpected_pure_unit_drops_row_and_warns(caplog):
    rows = _FY_JGAAP_DEI + [
        _fact_row("NetSalesSummaryOfBusinessResults", "CurrentYearDuration", "12345", unit_id="pure"),
    ]
    with caplog.at_level(logging.WARNING):
        result = _parse(rows)
    assert result["rows"] == []
    assert "pure" in caplog.text


# ---- 12. fetch_financial_csv（type=5） ----


def test_fetch_financial_csv_uses_type_5_and_expected_url():
    body = _financial_zip(_financial_csv(_FY_JGAAP_DEI))
    client, session = _stream_client_with(_FakeStreamResponse(content=body))
    result = edinet.fetch_financial_csv(client, "S100YMVN", "secret-key-value")
    assert result == body
    call = session.calls[0]
    assert call["url"] == edinet.DOCUMENT_URL.format(doc_id="S100YMVN")
    assert call["params"] == {"type": 5, "Subscription-Key": "secret-key-value"}


def test_fetch_financial_csv_rejects_non_alnum_doc_id():
    client, session = _stream_client_with(_FakeStreamResponse(content=b""))
    with pytest.raises(UserFacingError):
        edinet.fetch_financial_csv(client, "../evil", "secret-key-value")
    assert session.calls == [], "doc_id を検証する前に通信してしまっている"


def test_fetch_financial_csv_empty_api_key_raises():
    client, session = _stream_client_with(_FakeStreamResponse(content=b""))
    with pytest.raises(UserFacingError):
        edinet.fetch_financial_csv(client, "S100YMVN", "")
    assert session.calls == []


def test_fetch_financial_csv_too_large_aborts_without_reading_body():
    response = _FakeStreamResponse(
        content=b"x" * 10,
        headers={"Content-Length": str(edinet.FINANCIAL_DOCUMENT_MAX_BYTES + 1)},
    )
    client, session = _stream_client_with(response)
    with pytest.raises(edinet.DocumentTooLarge) as err:
        edinet.fetch_financial_csv(client, "S100YMVN", "secret-key-value")
    assert err.value.size_bytes == edinet.FINANCIAL_DOCUMENT_MAX_BYTES + 1
    assert response.closed is True
    assert session.calls[0]["stream"] is True


def test_fetch_financial_csv_does_not_leak_api_key_in_logs(caplog):
    body = _financial_zip(_financial_csv(_FY_JGAAP_DEI))
    client, _ = _stream_client_with(_FakeStreamResponse(content=body))
    secret = "sk-super-secret-financial-value"
    with caplog.at_level(logging.INFO):
        edinet.fetch_financial_csv(client, "S100YMVN", secret)
    assert secret not in caplog.text
    assert "Subscription-Key=***" in caplog.text


# ---------- 実データがあるときだけ走るテスト（tests/test_real_fixtures.py と同じ考え方） ----------
#
# `tests/fixtures/real/` は Git 対象外なので、実物が無い環境（CI 含む）では skip する。
# 値そのものは検証せず、「例外にならず一定数以上の行が取れる」「期待した standard/basis になる」
# という構造だけを見る（実数値をここに書くこと自体が実データの再配布になってしまうため）。
# このファイルは `app/sources/edinet.py` 用のテストファイルなので、`tests/test_real_fixtures.py`
# を編集せずこちらにまとめて追記する（P11-1 の作業指示で編集してよいファイルに含まれていないため）。

_REAL_FINANCIAL = Path(__file__).parent / "fixtures" / "real"
_REAL_FINANCIAL_CASES = [
    # (ファイル名, 期待する standard, 期待する consolidated_available)
    ("S100YGH5.zip", "ifrs", True),  # ソフトバンクグループ 有報・IFRS・連結あり
    ("S100Y62Z.zip", "jgaap", True),  # 有報・日本基準・連結あり
    ("S100Z1Z3.zip", "jgaap", True),  # モロゾフ 半期報告書・日本基準
    ("S100Z2OT.zip", "jgaap", False),  # ユーザーローカル 有報・連結決算なし（単体のみ）
]
real_financial_only = pytest.mark.skipif(
    not all((_REAL_FINANCIAL / name).exists() for name, _, _ in _REAL_FINANCIAL_CASES),
    reason="採取した EDINET 財務数値 ZIP（type=5）が無い（Git 対象外）",
)


@real_financial_only
@pytest.mark.parametrize("filename,expected_standard,expected_consolidated", _REAL_FINANCIAL_CASES)
def test_parse_financial_csv_parses_the_captured_real_documents(
    filename, expected_standard, expected_consolidated
):
    content = (_REAL_FINANCIAL / filename).read_bytes()
    result = edinet.parse_financial_csv(content)

    assert result["standard"] == expected_standard
    assert result["consolidated_available"] is expected_consolidated
    assert result["period_type"] in ("FY", "HY")
    assert len(result["rows"]) >= 10, "「主要な経営指標等の推移」なら最低でもこの程度は取れるはず"

    expected_basis = "consolidated" if expected_consolidated else "nonconsolidated"
    assert all(r["basis"] == expected_basis for r in result["rows"])
    assert all(r["period_type"] in ("FY", "HY") for r in result["rows"])
    assert all(len(r["period_end"]) == 10 and r["period_end"][4] == "-" for r in result["rows"])
    # revenue は「主要な経営指標等の推移」で必ず開示される項目のはず
    assert any(r["item"] == "revenue" for r in result["rows"])


@real_financial_only
def test_fetch_financial_csv_real_zip_sizes_are_within_the_shared_max_bytes():
    """実測の ZIP サイズが `FINANCIAL_DOCUMENT_MAX_BYTES` の上限内であることの保険（SPEC §2.9.2）。"""
    for filename, _, _ in _REAL_FINANCIAL_CASES:
        size = (_REAL_FINANCIAL / filename).stat().st_size
        assert size <= edinet.FINANCIAL_DOCUMENT_MAX_BYTES
