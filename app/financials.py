"""財務数値（有報・半期報の「主要な経営指標等の推移」）の保存・取り込み記録・読み出し（SPEC §2.9・§3）。

このモジュールは保存と読み出しだけを担当する。CSV のパース（P11-1）と指標の算出（P11-4）は別モジュール。

- `financials` の主キーは (symbol, period_end, item, basis)。行単位の UPSERT で、
  訂正報告書は「提出日時が新しい行だけ上書き」（SPEC §2.9.5）。訂正に無い項目を消してはいけないので、
  銘柄単位・期間単位の DELETE は書かない
- `period_type` は 'FY'（通期）/ 'HY'（中間期）。半期報告書には中間期と前事業年度の数値が同居しており、
  混ぜると成長率が「半年 ÷ 1年」で壊れるため、`load_series` で必ず分ける（SPEC §2.9.4a）
- `financial_docs` は取り込み済み書類の記録（成功・失敗どちらも残す。SPEC §2.9.1）
- `load_series` は P11-4（指標算出）・P11-5（プロンプト組み立て）が使う凍結済みの形を返す
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Callable

from .database import Database
from .errors import Cancelled, UserFacingError
from .jobs import JobContext
from .sources import edinet
from .sources.base import HttpError

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_financials(db: Database, symbol: str, doc: dict, rows: list[dict]) -> dict:
    """財務数値の行を UPSERT する。

    `doc` は {"doc_id", "submit_at", "period_end", "standard"}（後2つは記録用で、
    行ごとの period_end は rows 側の値を使う）。

    `rows` の各要素は period_type（'FY' / 'HY'）を持ちうる。省略されていたら 'FY' 扱いにする
    （半期報告書のパーサだけが 'HY' を明示すればよく、有報側の呼び出しを変えずに済む）。

    訂正の扱い（SPEC §2.9.5）: 既存行があるとき、新しい行の submit_at が既存行の submit_at
    **以上**のときだけ上書きする（同時刻の再取り込みも上書きを許す）。古い書類を後から
    取り込んだ場合は submit_at が既存行より古くなるので、その行は書き込まずに skipped に数える。
    訂正書類に載っていない項目の行はそもそも rows に無いので、既存行はそのまま残る
    （このテーブルに対して DELETE は行わない）。

    rows が空でもエラーにしない（0件保存として返す）。
    """
    if not rows:
        return {"saved": 0, "skipped": 0}

    doc_id = doc["doc_id"]
    submit_at = doc["submit_at"]
    standard = doc.get("standard")

    saved = 0
    skipped = 0
    with db.write() as conn:
        for row in rows:
            existing = conn.execute(
                """
                SELECT submit_at FROM financials
                WHERE symbol = ? AND period_end = ? AND item = ? AND basis = ?
                """,
                (symbol, row["period_end"], row["item"], row["basis"]),
            ).fetchone()
            if existing is not None and submit_at < existing["submit_at"]:
                # 古い書類を後から取り込んだ。新しい値を壊さないよう何もしない
                skipped += 1
                continue
            conn.execute(
                """
                INSERT INTO financials
                    (symbol, period_end, item, value, unit, basis, standard, period_type, doc_id, submit_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, period_end, item, basis) DO UPDATE SET
                    value       = excluded.value,
                    unit        = excluded.unit,
                    standard    = excluded.standard,
                    period_type = excluded.period_type,
                    doc_id      = excluded.doc_id,
                    submit_at   = excluded.submit_at
                """,
                (
                    symbol,
                    row["period_end"],
                    row["item"],
                    row.get("value"),
                    row.get("unit"),
                    row["basis"],
                    standard,
                    row.get("period_type") or "FY",
                    doc_id,
                    submit_at,
                ),
            )
            saved += 1

    return {"saved": saved, "skipped": skipped}


def record_doc(
    db: Database, symbol: str, doc_id: str, submit_at: str, period_end: str | None, result: str
) -> None:
    """`financial_docs` へ取り込み結果を UPSERT する（成功・失敗のどちらも記録する。SPEC §2.9.1）。

    `result` は 'ok' / 'empty' / 'error:...' を想定。失敗した書類も記録することで、
    未取得の有報・半期報を選ぶときに毎回取り直さないようにする。
    """
    with db.write() as conn:
        conn.execute(
            """
            INSERT INTO financial_docs (symbol, doc_id, submit_at, period_end, result, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, doc_id) DO UPDATE SET
                submit_at  = excluded.submit_at,
                period_end = excluded.period_end,
                result     = excluded.result,
                fetched_at = excluded.fetched_at
            """,
            (symbol, doc_id, submit_at, period_end, result, _now()),
        )


def imported_doc_ids(db: Database, symbol: str) -> set[str]:
    """記録済みの doc_id を結果によらず全部返す（失敗した書類を毎回取り直さないため）。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT doc_id FROM financial_docs WHERE symbol = ?", (symbol,)
        ).fetchall()
    return {r["doc_id"] for r in rows}


def load_series(db: Database, symbol: str) -> dict | None:
    """P11-4・P11-5 が使う凍結済みの形で財務数値を返す（この形は変えない）。

    - basis: consolidated の行が1行でもあれば consolidated 側だけを使う。無ければ nonconsolidated
    - standard: 採用した行のうち最も新しい submit_at の値（全部 NULL なら None）。FY/HY で分けない
    - periods: **period_type='FY' の行だけ**。period_end 昇順。items には value が NULL の行は入れない
    - interim: 最新の中間期（period_type='HY'）と、その1つ前を `prior` に持つ
      {"period_end", "items", "prior": {...} | None}。中間期が1件も無ければ None
    - source_docs: submit_at 降順（FY・HY 両方の書類を含む）

    財務数値が1件も無い銘柄では None を返す（SPEC §2.9.7）。
    FY が無く HY だけの銘柄でも None にはしない（`periods: []` と `interim` を返す。
    「使えるデータがあるか」の判定は呼び出し側の役割）。
    """
    with db.connect() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT period_end, period_type, item, value, unit, basis, standard, doc_id, submit_at
                FROM financials WHERE symbol = ?
                """,
                (symbol,),
            ).fetchall()
        ]

    if not rows:
        return None

    basis = "consolidated" if any(r["basis"] == "consolidated" for r in rows) else "nonconsolidated"
    selected = [r for r in rows if r["basis"] == basis]
    if not selected:
        return None

    max_submit_at = max(r["submit_at"] for r in selected)
    standard = next(
        (r["standard"] for r in selected if r["submit_at"] == max_submit_at and r["standard"] is not None),
        None,
    )

    fy_by_end: dict[str, dict] = {}
    hy_by_end: dict[str, dict] = {}
    docs: dict[str, str] = {}
    for r in selected:
        docs[r["doc_id"]] = r["submit_at"]
        by_end = fy_by_end if r["period_type"] == "FY" else hy_by_end
        period = by_end.setdefault(r["period_end"], {"period_end": r["period_end"], "items": {}})
        if r["value"] is not None:
            period["items"][r["item"]] = r["value"]

    periods = [fy_by_end[key] for key in sorted(fy_by_end)]

    interim = None
    if hy_by_end:
        hy_ends_desc = sorted(hy_by_end, reverse=True)
        latest = dict(hy_by_end[hy_ends_desc[0]])
        latest["prior"] = hy_by_end[hy_ends_desc[1]] if len(hy_ends_desc) >= 2 else None
        interim = latest

    source_docs = [
        {"doc_id": doc_id, "submit_at": s}
        for doc_id, s in sorted(docs.items(), key=lambda kv: kv[1], reverse=True)
    ]

    return {
        "basis": basis,
        "standard": standard,
        "periods": periods,
        "interim": interim,
        "source_docs": source_docs,
    }


def delete_financials(db: Database, symbol: str) -> None:
    """銘柄の財務数値と取り込み記録を削除する（銘柄削除時の掃除用。CASCADE があるので保険）。"""
    with db.write() as conn:
        conn.execute("DELETE FROM financials WHERE symbol = ?", (symbol,))
        conn.execute("DELETE FROM financial_docs WHERE symbol = ?", (symbol,))


# ---------- 取得ジョブと導線（P11-3。SPEC §2.9.1） ----------

# 対象は有価証券報告書（120）・半期報告書（160）と、それぞれの訂正（130・170）。
# 四半期報告書（140/150）は2024年に廃止済みで EDINET には過去分しか無く、対象外（SPEC §2.9.1・§1.3）
FINANCIAL_DOC_TYPES = ("120", "160", "130", "170")

DEFAULT_MAX_DOCS = 6  # 有報1件で5期分の履歴が入るので、これだけあれば通常は足りる

_MAX_CONSECUTIVE_FAILURES = 5  # この回数連続で失敗したらジョブを中止する（disclosures_job は3。書類単位のためやや緩める）


def _resolve_max_docs(value) -> int:
    """`params["max_docs"]` を正の int にする。0以下・数値でない値は既定にフォールバックする。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_DOCS
    return n if n > 0 else DEFAULT_MAX_DOCS


def _csv_flag_for(doc_id: str, cache: dict | None) -> int | None:
    """日次キャッシュの `results` から該当 `docID` の `csvFlag` を引く。

    キャッシュが無い・該当 docID が無い場合は None（不明。「とりあえず取りに行く」扱いにする。
    SPEC §2.9.1・キャッシュを消した環境でも機能が死なないようにするため）。
    """
    if cache is None:
        return None
    for item in cache.get("results") or []:
        if item.get("docID") == doc_id:
            return 1 if item.get("csvFlag") == "1" else 0
    return None


def pending_financial_docs(
    db: Database, symbol: str, *, base_dir: Path | None = None, limit: int | None = DEFAULT_MAX_DOCS
) -> list[dict]:
    """未取得の有報・半期報（訂正含む）を、提出日時の新しい順に `limit` 件まで選ぶ（SPEC §2.9.1）。

    - `role` は問わない（`disclosure_links` に紐づいてさえいればよい）
    - 取下げ（`withdrawal = 1`）は除く
    - `financial_docs` に記録済みの `doc_id`（成功・失敗を問わない）は除く。失敗した書類も再取得しない
    - `csv_flag` は追加の HTTP を出さず、`submit_at` の日付ぶんの日次キャッシュから引く。
      同じ日付のキャッシュは1回だけ読む（複数の書類が同じ日に提出されることがあるため）
    """
    placeholders = ", ".join("?" for _ in FINANCIAL_DOC_TYPES)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT DISTINCT d.doc_id, d.submit_at, d.period_end, d.doc_type_code
            FROM disclosures d
            JOIN disclosure_links l ON l.doc_id = d.doc_id
            WHERE l.symbol = ?
              AND d.doc_type_code IN ({placeholders})
              AND (d.withdrawal IS NULL OR d.withdrawal != 1)
            ORDER BY d.submit_at DESC
            """,
            (symbol, *FINANCIAL_DOC_TYPES),
        ).fetchall()

    imported = imported_doc_ids(db, symbol)
    pending = [dict(r) for r in rows if r["doc_id"] not in imported]
    if limit is not None:
        pending = pending[:limit]

    cache_by_date: dict[str, dict | None] = {}
    result: list[dict] = []
    for doc in pending:
        date = (doc["submit_at"] or "")[:10]
        if date not in cache_by_date:
            cache_by_date[date] = edinet.read_cache(date, base_dir=base_dir)
        result.append(
            {
                "doc_id": doc["doc_id"],
                "submit_at": doc["submit_at"],
                "period_end": doc["period_end"],
                "doc_type_code": doc["doc_type_code"],
                "csv_flag": _csv_flag_for(doc["doc_id"], cache_by_date[date]),
            }
        )
    return result


def _latest_period_end(rows: list[dict], fallback: str | None) -> str | None:
    """パース結果の行から最も新しい `period_end` を選ぶ（`financial_docs` の記録用）。

    行が無ければ `disclosures` 側の `period_end`（`fallback`）を使う。`period_end` は
    'YYYY-MM-DD' なので文字列の比較で新しさが決まる。
    """
    ends = [r["period_end"] for r in rows if r.get("period_end")]
    if not ends:
        return fallback
    return max(ends)


def financials_job(db: Database, settings, base_dir: Path | None = None) -> Callable[[JobContext, dict], dict]:
    """`jobs.register("financials", financials_job(db, settings))` に渡すジョブ関数を組み立てる（SPEC §2.9.1）。

    - `edinet_api_key` 未設定なら `UserFacingError`
    - `client` は銘柄・書類をまたいで1つだけ作り、レート制限（1秒間隔）を共有する
    - 銘柄ごとに `pending_financial_docs` で新しい順に選び、**処理は古い順**にする
      （訂正報告書が最後に効くので、この順で処理すれば自然に正しい値が残る）
    - `csv_flag == 0` の書類は HTTP を出さず `no_csv` として記録するだけ
    - HTTP 401/403/429 は即中止（`aborted = "forbidden"`）。`DocumentTooLarge` はその書類だけ失敗させて
      連続失敗に数えない。それ以外の例外は連続 `_MAX_CONSECUTIVE_FAILURES` 回で中止（`aborted = "failures"`）。
      成功（`no_csv` を除く HTTP 取得の成功）でカウンタを0に戻す
    - `Cancelled` はそのまま抜ける。各書類の保存は短いトランザクション（`save_financials`/`record_doc`）で
      その都度確定しているので、中断してもそれまでの保存は残る（CLAUDE.md 不変条件10）
    """

    def job(ctx: JobContext, params: dict) -> dict:
        api_key = settings.get_secret("edinet_api_key")
        if not api_key:
            raise UserFacingError("EDINET の API キーが設定されていません。設定タブで登録してください")

        symbol = params.get("symbol")
        symbols = params.get("symbols")
        if symbol:
            syms = [symbol]
        elif symbols:
            syms = list(symbols)
        else:
            syms = [row["symbol"] for row in db.list_stocks()]

        max_docs = _resolve_max_docs(params.get("max_docs"))

        client = edinet.make_client(settings)

        # 銘柄ごとに新しい順で選んだものを、古い順に並べ替えてから1本の処理列にする
        plan: list[tuple[str, dict]] = []
        for sym in syms:
            docs = pending_financial_docs(db, sym, base_dir=base_dir, limit=max_docs)
            for doc in sorted(docs, key=lambda d: d["submit_at"]):
                plan.append((sym, doc))

        total = len(plan)
        saved = 0
        skipped = 0
        no_csv = 0
        errors: list[str] = []
        aborted: str | None = None
        consecutive_failures = 0

        for i, (sym, doc) in enumerate(plan):
            ctx.progress(i, total, f"財務数値を取得中 {i + 1}/{total}（{sym}）")
            ctx.check()

            doc_id = doc["doc_id"]
            submit_at = doc["submit_at"]
            period_end = doc["period_end"]

            if doc["csv_flag"] == 0:
                record_doc(db, sym, doc_id, submit_at, period_end, "no_csv")
                no_csv += 1
                continue

            try:
                content = edinet.fetch_financial_csv(client, doc_id, api_key, cancel=ctx.cancel)
                parsed = edinet.parse_financial_csv(content)
            except Cancelled:
                raise
            except HttpError as exc:
                errors.append(f"{sym} {doc_id}: {exc}")
                if exc.status in (401, 403, 429):
                    aborted = "forbidden"
                    break
                record_doc(db, sym, doc_id, submit_at, period_end, f"error:{exc}")
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue
            except edinet.DocumentTooLarge as exc:
                errors.append(f"{sym} {doc_id}: {exc}")
                record_doc(db, sym, doc_id, submit_at, period_end, f"error:{exc}")
                continue  # 書類が大きすぎるだけなので連続失敗には数えない
            except Exception as exc:
                errors.append(f"{sym} {doc_id}: {exc}")
                record_doc(db, sym, doc_id, submit_at, period_end, f"error:{exc}")
                consecutive_failures += 1
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    aborted = "failures"
                    break
                continue

            consecutive_failures = 0  # HTTP 取得・パース自体は成功した

            rows = parsed.get("rows") or []
            doc_period_end = _latest_period_end(rows, period_end)
            if not rows:
                record_doc(db, sym, doc_id, submit_at, doc_period_end, "empty")
                continue

            save_doc = {
                "doc_id": doc_id,
                "submit_at": submit_at,
                "period_end": doc_period_end,
                "standard": parsed.get("standard"),
            }
            save_result = save_financials(db, sym, save_doc, rows)
            saved += save_result["saved"]
            skipped += save_result["skipped"]
            record_doc(db, sym, doc_id, submit_at, doc_period_end, "ok")

        ctx.progress(total, total, "財務数値の取得が完了")

        return {
            "symbols": len(syms),
            "docs": total,
            "saved": saved,
            "skipped": skipped,
            "no_csv": no_csv,
            "errors": errors,
            "aborted": aborted,
        }

    return job


def estimate_financials(
    db: Database, settings, symbol: str | None = None, *, base_dir: Path | None = None, max_docs: int = DEFAULT_MAX_DOCS
) -> dict:
    """取得ジョブの事前見積り（確認ダイアログ用。SPEC §2.9.1）。

    `app/ai/analyze.py` の `estimate` と同じ考え方で、API キー未設定などでも例外にせず
    `can_run: False` と `reason` を返す（呼び出し側はエラーではなく確認ダイアログの表示に使う）。

    与えられたシグネチャ（`db, symbol=None, *, base_dir=None, max_docs=...`）には API キーの有無を
    判定する材料が無いため、`app/ai/analyze.py` の `estimate(db, settings, symbol, ...)` に合わせて
    `settings` を第2引数に加えている（担当タスクの指示との差分。完了報告に記載）。

    `requests` は実際に HTTP を出す回数（`csv_flag == 0` の書類を除いた件数）。
    件数自体は API キーが無くても計算できる（`pending_financial_docs` はネットワークを使わない）ので、
    `can_run` に関わらず求めて返す。
    """
    api_key = settings.get_secret("edinet_api_key")
    can_run = bool(api_key)
    reason = None if can_run else "EDINET の API キーが設定されていません。設定タブで登録してください"

    syms = [symbol] if symbol else [row["symbol"] for row in db.list_stocks()]

    docs = 0
    requests = 0
    for sym in syms:
        pending = pending_financial_docs(db, sym, base_dir=base_dir, limit=max_docs)
        docs += len(pending)
        requests += sum(1 for d in pending if d["csv_flag"] != 0)

    return {
        "symbols": len(syms),
        "docs": docs,
        "requests": requests,
        "can_run": can_run,
        "reason": reason,
    }
