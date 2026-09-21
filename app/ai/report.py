"""AI 分析レポートの HTML 生成と保存（SPEC §2.7.6・§3 `ai_reports`）。

**CLAUDE.md 不変条件1（最重要）**: 需給データ（空売り残高・貸借取引残高）をレポートに載せない。
このモジュールが受け取るのは `app.ai.prompt.PromptInput`（= AI に実際に送った内容そのもの）と
`app.ai.schema.AnalysisReport`（= AI の出力）だけなので、需給が混入する経路が無い。
`service.dashboard()` の payload（需給の表示データを含む）はここでは一切参照しない。

HTML は Jinja2 の単一テンプレート（`templates/report.html.j2`）から生成する。外部 CSS/JS/画像は
参照せず、生成された文字列だけで完結する1ファイルにする。AI の出力（`summary` 等）をそのまま
テンプレートに埋め込むため、**autoescape は必須**（`<script>` 混入対策）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from .. import config
from ..errors import UserFacingError
from .prompt import PromptInput
from .schema import AnalysisReport

TEMPLATE_NAME = "report.html.j2"

# web/js/app.js の STATUS 表記（bull/bear/neutral/na → 強気/弱気/中立/—）に合わせる
_STATUS_LABELS = {"bull": "強気", "bear": "弱気", "neutral": "中立", "na": "—"}
# 判定に応じた CSS クラス名。未知の値は neutral 相当の見た目にする
_STATUS_CLASSES = {"bull": "bull", "bear": "bear", "neutral": "neutral", "na": "neutral"}
_DIRECTION_LABELS = {"buy": "買い", "sell": "売り"}
_VERDICT_LABELS = {"bullish": "強気", "bearish": "弱気", "neutral": "中立"}
_CONFIDENCE_LABELS = {"low": "低", "medium": "中", "high": "高"}
# 開示と登録銘柄の関係（SPEC §2.4.3 の role）。画面と同じく日本語で見せる
_ROLE_LABELS = {"filer": "提出者", "issuer": "発行者（保有された側）", "subject": "対象会社"}


def _role_label(role: str) -> str:
    """`filer` や `subject/filer` のような role 文字列を日本語にする。"""
    if not role:
        return ""
    return "・".join(_ROLE_LABELS.get(part, part) for part in role.split("/"))


def _format_value(value: Any) -> str:
    """指標値の表示。浮動小数はそのままだと桁が長すぎるので丸める。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)

# report_path でファイル名に使えない文字（Windows のパス予約文字含む）をアンダースコアに置き換える
_UNSAFE_SYMBOL_CHARS = re.compile(r"[^0-9A-Za-z_-]")


def _environment(templates_dir: Path | None = None) -> Environment:
    """テンプレートの探索環境を作る。既定は `app.config.BASE_DIR / "templates"`。

    `templates_dir` はテストから差し替えるための引数（SPEC §2.7.6 の実装メモ）。
    AI の出力をそのまま埋め込むテンプレートなので autoescape は常に有効にする。
    """
    directory = templates_dir if templates_dir is not None else config.BASE_DIR / "templates"
    return Environment(loader=FileSystemLoader(str(directory)), autoescape=True)


def _status_row(card: Any) -> dict:
    status = card["status"]
    value = card["value"]
    return {
        "label": card["label"],
        "status_label": _STATUS_LABELS.get(status, status),
        "status_class": _STATUS_CLASSES.get(status, "neutral"),
        "value_display": _format_value(value),
        "note": card["note"] or "",
    }


def _signal_row(sig: Any) -> dict:
    direction = sig["direction"]
    return {
        "date": sig["date"],
        "direction_label": _DIRECTION_LABELS.get(direction, direction),
        "label": sig["label"],
        "short": sig["short"],
    }


def render_report(
    data: PromptInput,
    report: AnalysisReport,
    *,
    model: str,
    generated_at: str | None = None,
) -> str:
    """`PromptInput`（送信データ）と `AnalysisReport`（AI の出力）からレポート HTML を組み立てる。

    表示順は SPEC §2.7.6 のとおり「総括・判定 → 各観点 → リスク → 注目点 → 指標表・シグナル →
    開示一覧 → 免責」。`generated_at` を省略すると `data.generated_at`（プロンプト生成時刻）を使う。

    テンプレートは既定で `app.config.BASE_DIR / "templates"` から探す。テストなど別の場所から
    読ませたい場合は `_environment(templates_dir=...)` を直接使うこと（本関数のシグネチャは
    P6-6 が呼ぶ形のまま固定する）。
    """
    env = _environment()
    template = env.get_template(TEMPLATE_NAME)
    context = {
        "symbol": data.symbol,
        "name": data.name,
        "exchange": data.exchange,
        "currency": data.currency,
        "days": data.days,
        "generated_at": generated_at if generated_at is not None else data.generated_at,
        "model": model,
        "report": report,
        "verdict_label": _VERDICT_LABELS.get(report.verdict, report.verdict),
        "confidence_label": _CONFIDENCE_LABELS.get(report.confidence, report.confidence),
        "latest_rows": [_status_row(card) for card in data.latest],
        "signal_rows": [_signal_row(sig) for sig in data.signals],
        "disclosures": [
            {
                "submit_at": d.submit_at,
                "label": d.label,
                "role": _role_label(d.role),
                "description": d.description,
                "reason": d.reason,
                "withdrawn": d.withdrawn,
            }
            for d in data.disclosures
        ],
    }
    return template.render(**context)


def report_path(reports_dir: Path | str, symbol: str, now: datetime | None = None) -> Path:
    """保存先パスを決める（SPEC §2.7.6: `data/reports/report_<symbol>_<YYYYmmdd_HHMMSS>.html`）。

    symbol に含まれる `.`（例: `7203.T`）などファイル名に使いにくい文字はアンダースコアに置き換える。
    同じ秒に複数回呼ばれ、既にそのパスにファイルが存在する場合は `_2` `_3` ... を付けて衝突を避ける
    （`save_report` は返されたパスへ即座に書き込むので、実際の衝突判定はディスク上の存在確認でよい）。
    """
    now_ = now if now is not None else datetime.now()
    reports_dir = Path(reports_dir)
    safe_symbol = _UNSAFE_SYMBOL_CHARS.sub("_", symbol)
    stamp = now_.strftime("%Y%m%d_%H%M%S")
    candidate = reports_dir / f"report_{safe_symbol}_{stamp}.html"
    n = 2
    while candidate.exists():
        candidate = reports_dir / f"report_{safe_symbol}_{stamp}_{n}.html"
        n += 1
    return candidate


def save_report(
    db,
    reports_dir: Path | str,
    symbol: str,
    html: str,
    *,
    model: str,
    in_tokens: int | None = None,
    out_tokens: int | None = None,
    now: datetime | None = None,
) -> dict:
    """HTML を UTF-8 で書き出し、`ai_reports` に1行記録する。"""
    now_ = now if now is not None else datetime.now()
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = report_path(reports_dir, symbol, now_)
    path.write_text(html, encoding="utf-8")

    created_at = now_.strftime("%Y-%m-%d %H:%M:%S")
    with db.write() as conn:
        cur = conn.execute(
            "INSERT INTO ai_reports (symbol, created_at, model, path, in_tokens, out_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, created_at, model, str(path), in_tokens, out_tokens),
        )
        report_id = cur.lastrowid
    return {
        "id": report_id,
        "symbol": symbol,
        "created_at": created_at,
        "model": model,
        "path": str(path),
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
    }


def _with_exists(row: dict) -> dict:
    row = dict(row)
    row["exists"] = Path(row["path"]).exists()
    return row


def list_reports(db, *, limit: int | None = None) -> list[dict]:
    """`ai_reports` を新しい順（`created_at` 降順、同時刻は `id` 降順）で返す。

    ファイルが既に消えていても行自体は返し、`exists: False` を付ける（SPEC の一覧表示要件）。
    """
    query = "SELECT id, symbol, created_at, model, path, in_tokens, out_tokens FROM ai_reports " \
        "ORDER BY created_at DESC, id DESC"
    params: tuple = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)
    with db.connect() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_with_exists(dict(row)) for row in rows]


def get_report(db, report_id: int) -> dict:
    """1件だけ取得する。無ければ `UserFacingError`。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, symbol, created_at, model, path, in_tokens, out_tokens "
            "FROM ai_reports WHERE id = ?",
            (report_id,),
        ).fetchone()
    if row is None:
        raise UserFacingError(f"レポートが見つかりません（id={report_id}）")
    return _with_exists(dict(row))


def delete_missing(db) -> int:
    """ファイルが実在しなくなった `ai_reports` の行を削除する。戻り値は削除した件数。"""
    with db.write() as conn:
        rows = conn.execute("SELECT id, path FROM ai_reports").fetchall()
        missing_ids = [row["id"] for row in rows if not Path(row["path"]).exists()]
        if missing_ids:
            conn.executemany("DELETE FROM ai_reports WHERE id = ?", [(i,) for i in missing_ids])
    return len(missing_ids)
