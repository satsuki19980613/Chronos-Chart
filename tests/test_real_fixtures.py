"""採取した実物に対するパーサの確認（SPEC §10.1）。

`tests/fixtures/real/` は Git 対象外なので、**実物が無い環境では skip する**。
実物の値そのものは検証せず、構造だけを見る（値はサイト側でいつでも変わるし、
取り直すこともしないため）。合成フィクスチャが実物からずれていないかの保険。
"""

from pathlib import Path

import pytest

from app.sources import karauri, taisyaku

REAL = Path(__file__).parent / "fixtures" / "real"
KARAURI_HTML = next(REAL.glob("karauri_*.html"), None) if REAL.is_dir() else None
ZANDAKA_CSV = REAL / "zandaka.csv"

karauri_only = pytest.mark.skipif(
    KARAURI_HTML is None, reason="採取した karauri.net の実ページが無い（Git 対象外）"
)
zandaka_only = pytest.mark.skipif(
    not ZANDAKA_CSV.exists(), reason="採取した zandaka.csv が無い（Git 対象外）"
)


@karauri_only
def test_karauri_parses_the_captured_page():
    rows = karauri.parse(KARAURI_HTML.read_text(encoding="utf-8"))

    assert rows, "実ページから1行も取れていない"
    # 表示上限は100件（SPEC §2.2.2a）。超えていたら上限の前提が変わったということ
    assert len(rows) <= 100
    assert all(len(r["calc_date"]) == 10 and r["calc_date"][4] == "-" for r in rows)
    assert all(r["holder_id"] for r in rows)
    assert all(r["ratio"] is None or 0 <= r["ratio"] < 100 for r in rows)
    # 同一 (calc_date, holder_id) は1件に畳まれている
    keys = [(r["calc_date"], r["holder_id"]) for r in rows]
    assert len(keys) == len(set(keys))


@karauri_only
def test_karauri_totals_from_the_captured_page():
    totals = karauri.compute_totals(karauri.parse(KARAURI_HTML.read_text(encoding="utf-8")))

    assert totals
    assert [t["date"] for t in totals] == sorted(t["date"] for t in totals)
    assert all(t["holders"] >= 0 and t["total_ratio"] >= 0 for t in totals)


@zandaka_only
def test_taisyaku_parses_the_captured_csv():
    rows = taisyaku.parse(ZANDAKA_CSV.read_bytes())

    assert rows, "実ファイルから1行も取れていない"
    # 東証の行だけを採った結果、銘柄コードは重複しないはず（SPEC §2.3.1）
    codes = [r["code"] for r in rows]
    assert len(codes) == len(set(codes))
    # 1ファイルには1申込日分しか入っていない
    assert len({r["date"] for r in rows}) == 1
    assert all(r["kind"] in ("prelim", "final") for r in rows)
    assert all(len(r["date"]) == 10 and r["date"][4] == "-" for r in rows)
