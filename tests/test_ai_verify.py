"""P11-6 後半: AI が書いた根拠の数値がプロンプトに実在するかの機械的な照合（SPEC §2.9.9）。

`app.ai.verify.verify_numbers` は純関数（DB にも外部にも触れない）なので、ここでは合成の
プロンプト文字列と合成の `AnalysisReport` だけを使う。ネットワークには一切アクセスしない。

末尾の `test_verify_failure_does_not_lose_the_analysis_result` だけ、`app.ai.analyze.run_analysis`
の配線（verify が例外を投げても分析結果を失わない）を確かめるため、フェイクの Gemini クライアントと
一時 DB を使う（実通信は行わない）。
"""

from __future__ import annotations

import pytest

from app.ai.schema import AnalysisReport, SectionAnalysis
from app.ai.verify import verify_numbers


def _report(**overrides) -> AnalysisReport:
    base = dict(
        technical=SectionAnalysis(evidence=["SMAが上向き"], assessment="上昇基調にある"),
        fundamental=SectionAnalysis(evidence=["ROEが改善"], assessment="財務は堅調"),
        disclosure=SectionAnalysis(evidence=["特段の開示なし"], assessment="材料は乏しい"),
        risks=["急な悪材料が出た場合の下振れリスク"],
        watch_points=["次回の有価証券報告書の提出時期"],
        verdict="bullish",
        confidence="medium",
        summary="総じて強気",
        data_scope_note="会社予想との比較・同業他社との比較は対象外。需給データは分析に含まない。",
    )
    base.update(overrides)
    return AnalysisReport(**base)


# ---------------------------------------------------------------------------
# 1. プロンプトにある数値だけを引用 → missing が空・rate == 1.0
# ---------------------------------------------------------------------------
def test_all_numbers_present_in_prompt_means_no_missing():
    prompt = "date,close\n2025-01-06,6810\n2025-01-07,6900\n"
    report = _report(
        technical=SectionAnalysis(evidence=["終値は6810円だった"], assessment="堅調"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []
    assert result["rate"] == 1.0
    assert result["found"] == result["checked"]
    assert result["checked"] >= 1


# ---------------------------------------------------------------------------
# 2. プロンプトに無い数値を混ぜると missing に出て、field が正しい位置を指す
# ---------------------------------------------------------------------------
def test_missing_number_is_reported_with_correct_field():
    prompt = "date,close\n2025-01-06,6810\n"
    report = _report(
        technical=SectionAnalysis(
            evidence=["終値は6810円だった", "出来高は999999999株で急増した"],
            assessment="堅調",
        ),
    )
    result = verify_numbers(prompt, report)
    fields = [m["field"] for m in result["missing"]]
    assert "technical.evidence[1]" in fields
    entry = next(m for m in result["missing"] if m["field"] == "technical.evidence[1]")
    assert entry["number"] == "999999999.0"
    assert "999999999" in entry["text"]
    assert result["rate"] < 1.0


# ---------------------------------------------------------------------------
# 3. カンマ付き（6,810）とカンマ無し（6810）が同じものとして照合される
# ---------------------------------------------------------------------------
def test_comma_and_plain_numbers_are_treated_as_the_same_value():
    prompt = "date,close\n2025-01-06,6810\n"
    report = _report(
        technical=SectionAnalysis(evidence=["終値は6,810円だった"], assessment="堅調"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []


def test_comma_and_plain_numbers_are_treated_as_the_same_value_reverse():
    """プロンプト側がカンマ付き、AI 側がカンマ無しの場合も同様。"""
    prompt = "売上高は7,798,650百万円だった\n"
    report = _report(
        fundamental=SectionAnalysis(evidence=["売上高は7798650百万円"], assessment="堅調"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []


# ---------------------------------------------------------------------------
# 4. 丸め違い（AI 7.7 / プロンプト 7.66）が実在扱いになる。逆向きも
# ---------------------------------------------------------------------------
def test_rounding_difference_is_treated_as_present():
    prompt = "PERは7.66倍だった\n"
    report = _report(
        fundamental=SectionAnalysis(evidence=["PERは7.7倍程度"], assessment="割安感がある"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []


def test_rounding_difference_is_treated_as_present_reverse():
    """逆向き: プロンプトが 7.7、AI が 7.66 と細かく書いた場合。"""
    prompt = "PERは7.7倍だった\n"
    report = _report(
        fundamental=SectionAnalysis(evidence=["PERは7.66倍程度"], assessment="割安感がある"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []


# ---------------------------------------------------------------------------
# 5. 日付が分解されず、日付の文字列として照合される
# ---------------------------------------------------------------------------
def test_dates_are_matched_as_whole_strings_not_decomposed():
    # プロンプトには 2026-09-18 という日付は無いが、2026・09・18 という数値バラバラでなら
    # 部分的に存在しうる状況を作る（year=2026, day=18 が別の文脈で出現）。分解して照合していたら
    # 誤って「実在した」と判定されてしまう
    prompt = "2026年の株価と、9月中旬の出来高、18日移動平均線について\n"
    report = _report(
        disclosure=SectionAnalysis(evidence=["2026-09-18に開示があった"], assessment="材料あり"),
    )
    result = verify_numbers(prompt, report)
    fields = [m["field"] for m in result["missing"]]
    assert "disclosure.evidence[0]" in fields
    entry = next(m for m in result["missing"] if m["field"] == "disclosure.evidence[0]")
    assert entry["number"] == "2026-09-18"


def test_dates_present_in_prompt_are_found_as_whole_strings():
    prompt = "date,close\n2026-09-18,6810\n"
    report = _report(
        disclosure=SectionAnalysis(evidence=["2026-09-18に開示があった"], assessment="材料あり"),
    )
    result = verify_numbers(prompt, report)
    assert result["missing"] == []


# ---------------------------------------------------------------------------
# 6. 1〜10 の単独の整数が照合対象から外れている
# ---------------------------------------------------------------------------
def test_small_lone_integers_are_excluded_from_checking():
    prompt = "（プロンプトに1〜10の数字はどこにも出てこない）\n"
    report = _report(
        watch_points=["開示が3件あった", "指標は2つ改善した", "10期分のデータを参照した"],
    )
    result = verify_numbers(prompt, report)
    assert result["checked"] == 0
    assert result["missing"] == []
    assert result["rate"] is None


def test_small_lone_integers_excluded_but_larger_numbers_still_checked():
    prompt = "date,close\n2025-01-06,6810\n"
    report = _report(
        watch_points=["開示が3件あった。終値は6810円。出来高は12件で急減した"],
    )
    result = verify_numbers(prompt, report)
    # "3件" "12件" の 3, 12 のうち、3 は除外対象（1-10）。12 は除外対象外なのでプロンプトに
    # 無ければ missing に出る。6810 はプロンプトに実在するので missing に出ない
    numbers = [m["number"] for m in result["missing"]]
    assert "3.0" not in numbers
    assert "12.0" in numbers
    assert "6810.0" not in numbers


# ---------------------------------------------------------------------------
# 7. 数値を1つも含まない出力で checked == 0・rate is None・例外なし
# ---------------------------------------------------------------------------
def test_no_numbers_at_all_does_not_raise():
    prompt = "date,close\n2025-01-06,6810\n"
    report = _report(
        technical=SectionAnalysis(evidence=["株価は堅調に推移している"], assessment="良好"),
        fundamental=SectionAnalysis(evidence=["業績は安定している"], assessment="良好"),
        disclosure=SectionAnalysis(evidence=["特段の材料はない"], assessment="平常"),
        risks=["市況の急変"],
        watch_points=["次の開示"],
        summary="総じて堅調",
        data_scope_note="会社予想との比較・同業他社との比較は対象外。需給データは分析に含まない。",
    )
    result = verify_numbers(prompt, report)
    assert result["checked"] == 0
    assert result["found"] == 0
    assert result["missing"] == []
    assert result["rate"] is None


# ---------------------------------------------------------------------------
# 追加: 全フィールド（risks/watch_points/summary/data_scope_note）が対象になっていること
# ---------------------------------------------------------------------------
def test_all_documented_fields_are_scanned():
    prompt = "何も数字は無いプロンプト\n"
    report = _report(
        risks=["リスクは99999件ある"],
        watch_points=["注目点は88888件ある"],
        summary="サマリーの数値は77777",
        data_scope_note="対象外77776。需給データは分析に含まない。",
    )
    result = verify_numbers(prompt, report)
    fields = {m["field"] for m in result["missing"]}
    assert fields == {"risks[0]", "watch_points[0]", "summary", "data_scope_note"}


# ---------------------------------------------------------------------------
# 10. verify_numbers が例外を投げても run_analysis の分析結果が失われない
# ---------------------------------------------------------------------------
def test_verify_failure_does_not_lose_the_analysis_result(tmp_path, monkeypatch):
    import threading

    from app import config
    from app.ai import analyze
    from app.ai.client import Reply, Usage
    from app.database import Database
    from app.settings import Settings

    class _FakeKeyring:
        def __init__(self, values):
            self.values = dict(values)

        def get_password(self, service, key):
            return self.values.get(key)

        def set_password(self, service, key, value):
            self.values[key] = value

        def delete_password(self, service, key):
            self.values.pop(key, None)

    class _FakeClient:
        def __init__(self, text: str):
            self.model = "gemini-test"
            self._text = text

        def count_tokens(self, prompt):
            return 1000

        def generate(self, prompt, schema=None):
            return Reply(
                text=self._text,
                finish_reason="STOP",
                usage=Usage(prompt_tokens=1000, output_tokens=200, thoughts_tokens=0, total_tokens=1200),
            )

    class _Ctx:
        def __init__(self):
            self.cancel = threading.Event()

        def progress(self, current, total, label=""):
            pass

        def wait(self, seconds):
            pass

    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")

    db = Database(tmp_path / "verify.db")
    db.init_schema()
    db.upsert_stock("1234.T", "1234", "テスト株式会社", "東証", "JPY")
    from app import indicators as ind
    import numpy as np

    from conftest import make_prices

    prices = make_prices(100 + np.arange(140) * 0.1)
    db.upsert_prices("1234.T", prices)
    db.replace_indicators("1234.T", ind.compute_indicators(prices))

    settings = Settings(db, keyring_backend=_FakeKeyring({"gemini_api_key": "KEY"}))
    settings.update({"gemini_model": "gemini-test", "gemini_rpm": 10, "gemini_tpm": 1_000_000, "gemini_rpd": 50})

    valid_json = _report().model_dump_json()
    client = _FakeClient(text=valid_json)

    def _boom(*args, **kwargs):
        raise RuntimeError("検証の内部エラー（テスト用）")

    monkeypatch.setattr(analyze, "verify_numbers", _boom)

    result = analyze.run_analysis(db, settings, "1234.T", 60, client=client, cancel=None, sleep=lambda s: None)

    assert result.report.summary == "総じて強気"
    assert result.verify is None
