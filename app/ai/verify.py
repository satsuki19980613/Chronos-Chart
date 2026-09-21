"""AI が書いた根拠に出てくる数値が、実際に送ったプロンプトに実在するかの機械的な照合（SPEC §2.9.9）。

`docs/research/llm-prompting.md` §6 の指摘（Turpin et al., NeurIPS 2023）どおり、
「根拠（evidence）を先に書かせる」だけでは、その根拠が後付けの合理化になっていないかまでは
防げない。この検証は、根拠等に現れた数値・日付がプロンプト本文にそのまま存在するかを見るだけの
機械的なチェックであり、　**その数値の使い方（どの指標の値として引用したか等）が正しいかどうかは判定しない**。
プロンプトに同じ数字がたまたま存在するだけの偶然の一致もあり得る。あくまで「実在しない数値」が
挙がったときに、レポートの読み手が本文を疑う手がかりとして使うものである（この限界は画面表示にも
必ず出すこと。`app/ai/report.py` の `_VERIFY_LIMITATION_NOTE` を参照）。

**このモジュールは純関数だけで構成する。** DB にも外部（Gemini 等）にも一切触れない。
受け取るのはプロンプト本文の文字列と `AnalysisReport`（AI の出力）だけで、需給データを扱う経路が
そもそも存在しない。
"""

from __future__ import annotations

import re

from .schema import AnalysisReport

# 日付（YYYY-MM-DD）はまず丸ごと1トークンとして拾う。年・月・日をバラバラの数値として照合すると
# （例: 2026-09-18 を 2026・09・18 の3つの数値とみなす）、意味の無い一致・不一致を量産してしまうため。
# 分岐の順序が重要: alternation は先頭から順に試すので、日付パターンを数値パターンより先に置く。
_DATE_PATTERN = r"\d{4}-\d{2}-\d{2}"
# 3桁区切りのカンマ数値。カンマの無い数値（例: 12345）を誤って先頭3桁だけ拾ってしまわないよう、
# 「カンマ区切りが最低1回は続く」ことを必須にする（`(?:,\d{3})+`。`*` にすると "123" だけを
# 拾って残りの "45" を別トークンとして誤検出する）
_COMMA_NUMBER_PATTERN = r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
# カンマ無しの数値（整数・小数）
_PLAIN_NUMBER_PATTERN = r"-?\d+(?:\.\d+)?"

_TOKEN_RE = re.compile(f"{_DATE_PATTERN}|{_COMMA_NUMBER_PATTERN}|{_PLAIN_NUMBER_PATTERN}")

# 1〜10の単独の整数（カンマも小数点も無いもの）は照合から除く。「3件」「2つ」「5期」のような
# 数え上げ・件数の語に現れる小さな整数は、値そのものを引用しているわけではなく、プロンプト中にも
# 偶然同じ小さい整数がありふれて存在するため、拾っても「実在した/しなかった」の判定に意味が無く、
# 逆に検証結果（`checked`/`found`）を無意味な一致・不一致で汚してしまう。11以上は年やコードなどと
# 誤認しにくく、単なる件数としては出にくいため対象外にする
_EXCLUDED_SMALL_INT_MIN = 1
_EXCLUDED_SMALL_INT_MAX = 10


def _iter_tokens(text: str) -> list[tuple[str, str]]:
    """`text` から日付・数値のトークンを出現順に取り出す。戻り値は (元の文字列, kind) の並び。

    kind は "date" か "number"。
    """
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        kind = "date" if re.fullmatch(_DATE_PATTERN, raw) else "number"
        tokens.append((raw, kind))
    return tokens


def _is_excluded_small_integer(raw: str) -> bool:
    if "," in raw or "." in raw:
        return False
    try:
        value = int(raw)
    except ValueError:  # pragma: no cover - _TOKEN_RE が保証するので実際には起きない
        return False
    return _EXCLUDED_SMALL_INT_MIN <= value <= _EXCLUDED_SMALL_INT_MAX


def _parse_number(raw: str) -> tuple[float, int]:
    """カンマを除いて数値化し、(値, 小数桁数) を返す。小数桁数は丸め許容照合に使う。"""
    stripped = raw.replace(",", "")
    value = float(stripped)
    decimals = len(stripped.split(".", 1)[1]) if "." in stripped else 0
    return value, decimals


def _numbers_match(value: float, decimals: int, candidates: list[tuple[float, int]]) -> bool:
    """`value`（小数桁数 `decimals`）が `candidates` のいずれかと「実在する」とみなせるか。

    単純な完全一致だけでなく、**丸め違いを許す**（依頼元の指定）。桁数の少ないほうに合わせて
    もう一方を丸め、文字列表現が一致すれば実在とみなす。どちら向きの丸めも試す
    （AI が短い桁数で書いた場合・プロンプト側が短い桁数の場合の両方）。float 同士の `==` は
    誤差で食い違うことがあるため、丸めた後は文字列（`.{n}f` 書式）で比較する。

    なお、**単位の言い換えは追わない**（例: "7,798,650百万円" と "7兆7,986億円" は別物として扱う）。
    プロンプト側は常に百万円単位・3桁区切りの数値で財務数値を書いている（`app/ai/report.py` の
    `_JPY_UNIT_DIVISOR` 等）という前提に乗り、その素直な数値表現同士を突き合わせるだけにとどめる。
    単位変換まで追いかけると際限が無く、かつ「アプリが送った数値表現をそのまま照合する」という
    このチェックの趣旨（後付けの数値創作の検出）からも外れるため
    """
    value_repr = f"{value:.{decimals}f}"
    for cand_value, cand_decimals in candidates:
        # 相手をこちらの桁数に丸めて比較
        if f"{cand_value:.{decimals}f}" == value_repr:
            return True
        # こちらを相手の桁数に丸めて比較（逆向き）
        cand_repr = f"{cand_value:.{cand_decimals}f}"
        if f"{value:.{cand_decimals}f}" == cand_repr:
            return True
    return False


def _collect_fields(report: AnalysisReport) -> list[tuple[str, str]]:
    """照合対象のフィールドを (位置を表す名前, 本文) の並びで返す（SPEC §2.9.9）。

    対象は `technical`/`fundamental`/`disclosure` の `evidence`・`assessment`、
    `risks`・`watch_points`・`summary`・`data_scope_note`。`verdict`/`confidence` は列挙型で
    自由記述ではないので対象外
    """
    fields: list[tuple[str, str]] = []
    for section_name in ("technical", "fundamental", "disclosure"):
        section = getattr(report, section_name)
        for i, item in enumerate(section.evidence):
            fields.append((f"{section_name}.evidence[{i}]", item))
        fields.append((f"{section_name}.assessment", section.assessment))
    for i, item in enumerate(report.risks):
        fields.append((f"risks[{i}]", item))
    for i, item in enumerate(report.watch_points):
        fields.append((f"watch_points[{i}]", item))
    fields.append(("summary", report.summary))
    fields.append(("data_scope_note", report.data_scope_note))
    return fields


def verify_numbers(prompt_text: str, report: AnalysisReport) -> dict:
    """AI の出力（`report`）の根拠等に現れる数値・日付が、`prompt_text` に実在するかを照合する。

    **純関数。** DB にも外部にも一切触れない。`prompt_text` は実際に Gemini へ送った文字列そのもの
    （`app.ai.prompt.build_prompt` の戻り値）を渡すこと。同じ入力から同じプロンプト文字列が
    決定的に組み立てられることを前提にしているなら `build_prompt` を呼び直しても構わないが、
    可能なら送信時に使った文字列をそのまま渡すほうが確実（呼び出し側は `app/ai/analyze.py`）。

    戻り値の形（表示側 `app/ai/report.py` がこの形をそのまま使う）:

        {
            "checked": 23,   # 照合した数値・日付の総数（1〜10の単独整数は含まない）
            "found": 21,     # そのうちプロンプトに実在した数
            "missing": [     # 実在しなかったものの一覧
                {"field": "technical.evidence[0]", "number": "6810.0", "text": "終値は6,810円で…"},
                ...
            ],
            "rate": 0.913,   # found / checked（checked が 0 なら None）
        }

    `missing` の `number` は、数値なら（カンマを除いた）Python の `float` 文字列表現、
    日付ならそのままの日付文字列（分解しない）。`text` は数値が出てきたフィールドの本文全体
    （該当文だけを抜き出す処理はしない。抜き出しの精度より、呼び出し側が元の文をそのまま
    確認できることを優先した）。

    **この検査の限界（必ず理解した上で使うこと）**: プロンプトに同じ数字が**在るか**を見るだけで、
    その数字の**使い方が正しいか**（どの指標の値として引用したか、文脈が合っているか）は分からない。
    偶然の一致もあり得る。実在しない数値が挙がったときの手がかりとして使うものであり、
    「実在した」ことは正しさの証明にはならない
    """
    prompt_dates: set[str] = set()
    prompt_numbers: list[tuple[float, int]] = []
    for raw, kind in _iter_tokens(prompt_text):
        if kind == "date":
            prompt_dates.add(raw)
        else:
            prompt_numbers.append(_parse_number(raw))

    checked = 0
    found = 0
    missing: list[dict] = []
    for field_name, text in _collect_fields(report):
        for raw, kind in _iter_tokens(text):
            if kind == "number" and _is_excluded_small_integer(raw):
                continue
            checked += 1
            if kind == "date":
                is_found = raw in prompt_dates
                number_repr = raw
            else:
                value, decimals = _parse_number(raw)
                is_found = _numbers_match(value, decimals, prompt_numbers)
                number_repr = str(value)
            if is_found:
                found += 1
            else:
                missing.append({"field": field_name, "number": number_repr, "text": text})

    rate = (found / checked) if checked else None
    return {"checked": checked, "found": found, "missing": missing, "rate": rate}
