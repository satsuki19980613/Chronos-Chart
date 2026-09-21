"""AI 分析レポート用のインライン SVG 生成（SPEC §2.9 / P12-1）。

レポート（`data/reports/*.html`）は**外部参照を一切持たない単一 HTML** であり、
サンドボックス付き iframe（`sandbox=""`）にも埋め込まれる。そのため図はすべて
サーバー側（本モジュール）で組み立てたインライン SVG 文字列とし、`<script>` や外部 URL
（CDN・画像参照など）は一切出力しない。新しい依存パッケージも追加しない（標準ライブラリのみ）。

配色は色覚多様性に配慮した Okabe–Ito パレットを使う。色は CSS カスタムプロパティ
（`var(--chart-1, #E69F00)` など）経由で出すことで、テンプレート側の `<style>` が
ダークモード（`prefers-color-scheme`）や印刷（`@media print`）に合わせて上書きできるようにする。
軸・目盛り・文字は `currentColor` にして、埋め込み先の文字色に従わせる。

すべての公開関数は **`<svg>...</svg>` の文字列を返す純粋関数**で、ファイル書き込みや
DB アクセスは行わない。値が全部 `None`・空配列・カテゴリ1個などの退化ケースでも
例外を投げず、`placeholder()` 相当の代替表示か、可能な範囲の図を返す。
"""

from __future__ import annotations

import html
import math
from typing import Any

# ---------------------------------------------------------------------------
# 配色・共通定数
# ---------------------------------------------------------------------------

# Okabe–Ito パレット（色覚多様性に配慮）。テンプレート担当の呼び出しコードはキー名で色を指定する
PALETTE: dict[str, str] = {
    "orange": "#E69F00",
    "skyblue": "#56B4E9",
    "green": "#009E73",
    "yellow": "#F0E442",
    "blue": "#0072B2",
    "vermilion": "#D55E00",
    "purple": "#CC79A7",
    "black": "#000000",
}
# CSS カスタムプロパティ名（--chart-1 ...）に対応させる順序
_PALETTE_ORDER = list(PALETTE.keys())

# すべての <svg> に付ける class 名（テンプレート側の <style> がこれを対象にする）
CHART_CLASS = "cc-chart"

# SVG の名前空間。外部への参照ではなく宣言なので、「外部参照ゼロ」の検査からは除外する
SVG_NS = "http://www.w3.org/2000/svg"

# イベントマーカーの種類ごとの色・形（買い=緑の上向き三角、売り=朱の下向き三角、開示=青の丸）
_MARKER_STYLE: dict[str, dict[str, str]] = {
    "buy": {"color": "green", "shape": "up"},
    "sell": {"color": "vermilion", "shape": "down"},
    "disclosure": {"color": "blue", "shape": "circle"},
}


# ---------------------------------------------------------------------------
# 数値・文字列の整形ヘルパー
# ---------------------------------------------------------------------------


def _is_missing(value: Any) -> bool:
    """`None` または NaN なら真。欠測値の判定をここに集約する。"""
    if value is None:
        return True
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def scale_unit(values: Any) -> tuple[float, str]:
    """金額の桁を丸めるための (除数, 単位ラベル) を返す。

    値の絶対値の最大が 1兆・1億・100万のどれ以上かで「兆円」「億円」「百万円」を選び、
    それ未満は「円」（除数1）にする。値が全部 `None`・空なら `(1.0, "")`（単位不明）。
    ネストしたリスト（複数系列）を渡しても1階層だけ平坦化して扱う。
    """
    flat: list[float] = []
    for v in values or []:
        if isinstance(v, (list, tuple)):
            flat.extend(x for x in v if not _is_missing(x))
        elif not _is_missing(v):
            flat.append(float(v))
    if not flat:
        return (1.0, "")
    max_abs = max(abs(v) for v in flat)
    if max_abs >= 1e12:
        return (1e12, "兆円")
    if max_abs >= 1e8:
        return (1e8, "億円")
    if max_abs >= 1e6:
        return (1e6, "百万円")
    return (1.0, "円")


def format_number(value: Any, *, digits: int = 0) -> str:
    """桁区切りの数値表示。`None`（NaN 含む）は "—"。"""
    if _is_missing(value):
        return "—"
    return f"{float(value):,.{digits}f}"


def format_percent(value: Any, *, digits: int = 1) -> str:
    """比率（0.343 のような小数）を "34.3%" のようなパーセント表示にする。"""
    if _is_missing(value):
        return "—"
    return f"{float(value) * 100:,.{digits}f}%"


def delta_mark(current: Any, previous: Any) -> dict:
    """前期・前日比などの増減を、記号（↑↓→）付きの表示情報にする。

    **▲▼ は使わない。** 日本語の財務資料では「▲1,000」がマイナスを表すため、
    増加の印に ▲ を付けると符号が逆に読まれる。

    色だけに意味を負わせないよう、`text` に必ず記号を含める。戻り値の `class` は
    呼び出し側（テンプレートの CSS）が色分けに使うためのフックで、ここでは色そのものは決めない。
    """
    if _is_missing(current) or _is_missing(previous):
        return {"text": "—", "direction": "na", "class": "is-na"}

    current = float(current)
    previous = float(previous)
    diff = current - previous

    if previous != 0:
        pct: float | None = diff / abs(previous)
    elif diff == 0:
        pct = 0.0
    else:
        pct = None  # 前期がゼロで比較不能（率は出せないが増減の向きだけ分かる）

    if pct is None:
        if diff > 0:
            return {"text": "↑—", "direction": "up", "class": "is-up"}
        return {"text": "↓—", "direction": "down", "class": "is-down"}
    if pct > 0:
        return {"text": f"↑+{format_percent(pct)}", "direction": "up", "class": "is-up"}
    if pct < 0:
        return {"text": f"↓-{format_percent(abs(pct))}", "direction": "down", "class": "is-down"}
    return {"text": f"→{format_percent(0.0)}", "direction": "flat", "class": "is-flat"}


def _esc(value: Any) -> str:
    """SVG のテキスト・属性に埋め込む前にエスケープする。"""
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------------------
# SVG 組み立ての共通部品
# ---------------------------------------------------------------------------


def _svg_header(
    width: float,
    height: float,
    *,
    aria_title: str,
    aria_desc: str = "",
    extra_class: str = "",
) -> tuple[str, str]:
    """`<svg ...>` の開始タグと `<title>`/`<desc>` を組み立てる。

    `role="img"` を付け、読み上げ環境や図が表示できない環境向けの代替テキストにする。
    `desc` を省略した場合は `title` を流用する（空にしない）。
    """
    classes = CHART_CLASS if not extra_class else f"{CHART_CLASS} {extra_class}"
    desc_text = aria_desc if aria_desc else aria_title
    # xmlns は付ける。HTML に inline で埋め込む分には無くても解釈されるが、
    # レポートの HTML を別のツールに読ませたときに SVG として扱われないことがある。
    # これは名前空間の宣言であって外部への参照ではない（何も取りに行かない）。
    open_tag = (
        f'<svg xmlns="{SVG_NS}" viewBox="0 0 {width:g} {height:g}" width="100%" '
        f'preserveAspectRatio="xMidYMid meet" role="img" class="{classes}">'
    )
    head = f"<title>{_esc(aria_title)}</title><desc>{_esc(desc_text)}</desc>"
    return open_tag, head


def placeholder(message: str, *, width: int = 680, height: int = 120) -> str:
    """データが無いときに図の代わりに出す、枠とメッセージだけの SVG。"""
    open_tag, head = _svg_header(
        width, height, aria_title="データなし", aria_desc=str(message), extra_class="cc-chart--placeholder"
    )
    body = (
        f'<rect x="1" y="1" width="{width - 2:g}" height="{height - 2:g}" fill="none" '
        f'stroke="currentColor" stroke-opacity="0.3" stroke-dasharray="4 4"/>'
        f'<text x="{width / 2:g}" y="{height / 2:g}" text-anchor="middle" '
        f'dominant-baseline="middle" fill="currentColor" font-size="13">{_esc(message)}</text>'
    )
    return f"{open_tag}{head}{body}</svg>"


def _color_var(key: str | None) -> str:
    """パレットのキー（例: "orange"）を CSS カスタムプロパティ参照に変換する。

    テンプレート側の `<style>` で `--chart-1` などを上書きすれば、ダークモードや
    印刷用に配色を差し替えられる。`#rrggbb` を直接渡された場合はそのまま使い、
    未知の値は `currentColor` にフォールバックする。
    """
    if not key:
        return "currentColor"
    if key in PALETTE:
        idx = _PALETTE_ORDER.index(key) + 1
        return f"var(--chart-{idx}, {PALETTE[key]})"
    if isinstance(key, str) and key.startswith("#"):
        return key
    return "currentColor"


def _series_color(series_item: dict, index: int) -> str:
    """系列辞書の `color` を優先し、無ければパレットを順番に割り当てる。"""
    key = series_item.get("color")
    if key:
        return _color_var(key)
    return _color_var(_PALETTE_ORDER[index % len(_PALETTE_ORDER)])


def _numeric_domain(
    values: Any, *, include_zero: bool = False, pad_ratio: float = 0.08
) -> tuple[float, float]:
    """折れ線・棒グラフの値域 (lo, hi) を決める。

    `include_zero=True` の棒グラフでは 0 を必ず含めたうえで、0 が置かれる側には
    余白を足さない（0 の位置がぶれないようにするため）。値が無ければ `(0.0, 1.0)`。
    """
    nums = [float(v) for v in (values or []) if not _is_missing(v)]
    if not nums:
        return (0.0, 1.0)
    lo, hi = min(nums), max(nums)
    if include_zero:
        lo = min(lo, 0.0)
        hi = max(hi, 0.0)
    if lo == hi:
        span = abs(lo) if lo != 0 else 1.0
        lo -= span * 0.1 + 0.5
        hi += span * 0.1 + 0.5
        if include_zero:
            lo = min(lo, 0.0)
            hi = max(hi, 0.0)
        return (lo, hi)
    pad = (hi - lo) * pad_ratio
    if include_zero:
        if hi > 0:
            hi += pad
        if lo < 0:
            lo -= pad
    else:
        lo -= pad
        hi += pad
    return (lo, hi)


def _y_pixel(value: float, lo: float, hi: float, top: float, height_px: float) -> float:
    """値域 [lo, hi] を、上端が最大値になるよう縦方向のピクセル座標に変換する。"""
    span = (hi - lo) or 1.0
    t = (value - lo) / span
    return top + (1.0 - t) * height_px


def _plot_area(
    width: float, height: float, *, left: float = 48, right: float = 12, top: float = 20, bottom: float = 28
) -> dict[str, float]:
    """軸ラベル用の余白を差し引いた描画領域を返す。"""
    return {
        "left": left,
        "right": max(width - right, left + 1),
        "top": top,
        "bottom": max(height - bottom, top + 1),
        "width": max(width - left - right, 1.0),
        "height": max(height - top - bottom, 1.0),
    }


def _thin_indices(n: int, target: int) -> list[int]:
    """0..n-1 から、両端を含めておよそ `target` 個を等間隔で選ぶ（x軸ラベルの間引き用）。"""
    if n <= 0:
        return []
    if n <= target:
        return list(range(n))
    if target <= 1:
        return [0]
    step = (n - 1) / (target - 1)
    return sorted({round(i * step) for i in range(target)})


def _axis_date_label(value: str) -> str:
    """"YYYY-MM-DD" を "M/D" にする。想定外の形式はそのまま返す。"""
    parts = str(value).split("-")
    if len(parts) == 3:
        try:
            return f"{int(parts[1])}/{int(parts[2])}"
        except ValueError:
            return str(value)
    return str(value)


def _line_paths(points: list[tuple[float, float | None]]) -> list[str]:
    """(x, y|None) の並びから、`None` をまたがない複数の `M..L..` パス文字列を作る。

    値が1点だけの区間は線を引けないので捨てる（欠測をまたいで結ばない仕様の徹底）。
    """
    paths: list[str] = []
    current: list[tuple[float, float]] = []
    for x, y in points:
        if y is None:
            if len(current) >= 2:
                paths.append(_to_path(current))
            current = []
        else:
            current.append((x, y))
    if len(current) >= 2:
        paths.append(_to_path(current))
    return paths


def _to_path(points: list[tuple[float, float]]) -> str:
    return "M " + " L ".join(f"{x:.2f},{y:.2f}" for x, y in points)


def _triangle_up(cx: float, cy: float, size: float, color: str) -> str:
    pts = f"{cx:.2f},{cy - size:.2f} {cx - size:.2f},{cy + size:.2f} {cx + size:.2f},{cy + size:.2f}"
    return f'<polygon points="{pts}" fill="{color}"/>'


def _triangle_down(cx: float, cy: float, size: float, color: str) -> str:
    pts = f"{cx:.2f},{cy + size:.2f} {cx - size:.2f},{cy - size:.2f} {cx + size:.2f},{cy - size:.2f}"
    return f'<polygon points="{pts}" fill="{color}"/>'


# ---------------------------------------------------------------------------
# 表の行末に置く小さな折れ線
# ---------------------------------------------------------------------------


def sparkline(values: Any, *, width: int = 84, height: int = 22, aria: str = "") -> str:
    """表の行末に置く小さな折れ線。値が1個以下なら空文字を返す（placeholder は使わない）。"""
    values = list(values or [])
    if len(values) <= 1:
        return ""

    margin = 3.0
    lo, hi = _numeric_domain(values, include_zero=False)
    n = len(values)

    def px(i: int) -> float:
        return margin + i / (n - 1) * (width - 2 * margin)

    def py(v: float) -> float:
        return _y_pixel(v, lo, hi, margin, height - 2 * margin)

    points: list[tuple[float, float | None]] = [
        (px(i), None if _is_missing(v) else py(float(v))) for i, v in enumerate(values)
    ]

    parts: list[str] = []
    for d in _line_paths(points):
        parts.append(
            f'<path d="{d}" fill="none" stroke="{_color_var("blue")}" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )
    last_point = next((p for p in reversed(points) if p[1] is not None), None)
    if last_point is not None:
        parts.append(f'<circle cx="{last_point[0]:.2f}" cy="{last_point[1]:.2f}" r="1.8" fill="{_color_var("blue")}"/>')

    open_tag, head = _svg_header(
        width, height, aria_title="スパークライン", aria_desc=aria, extra_class="cc-chart--sparkline"
    )
    return f"{open_tag}{head}{''.join(parts)}</svg>"


# ---------------------------------------------------------------------------
# 時系列の折れ線
# ---------------------------------------------------------------------------


def line_chart(
    dates: Any,
    series: Any,
    *,
    markers: Any = (),
    width: int = 680,
    height: int = 240,
    unit: str = "",
    aria: str = "",
) -> str:
    """時系列の折れ線グラフ。`values` の `None` は線を切る（欠測をまたいで結ばない）。

    `markers` は買い（下端に上向き三角）・売り（上端に下向き三角）・開示（最上部に丸）を
    `dates` 上の該当日に重ねる。`dates` に無い日付のマーカーは無視する。
    """
    dates = [str(d) for d in (dates or [])]
    series = list(series or [])
    all_values = [v for s in series for v in (s.get("values") or [])]
    has_data = bool(dates) and bool(series) and any(not _is_missing(v) for v in all_values)
    if not has_data:
        return placeholder("データなし", width=width, height=height)

    has_legend = len(series) > 1
    top_margin = 22 + (14 if has_legend else 0)
    frame = _plot_area(width, height, top=top_margin)
    n = len(dates)
    lo, hi = _numeric_domain(all_values, include_zero=False)

    def px(i: int) -> float:
        if n <= 1:
            return frame["left"] + frame["width"] / 2
        return frame["left"] + i / (n - 1) * frame["width"]

    def py(v: float) -> float:
        return _y_pixel(v, lo, hi, frame["top"], frame["height"])

    parts: list[str] = []

    # 横方向グリッド線と y 軸ラベル（4分割）
    grid_n = 4
    for g in range(grid_n + 1):
        gv = lo + (hi - lo) * g / grid_n
        gy = py(gv)
        parts.append(
            f'<line x1="{frame["left"]:.2f}" y1="{gy:.2f}" x2="{frame["right"]:.2f}" y2="{gy:.2f}" '
            f'stroke="currentColor" stroke-opacity="0.12"/>'
        )
        label = format_number(gv) + (unit if unit else "")
        parts.append(
            f'<text x="{frame["left"] - 6:.2f}" y="{gy + 3:.2f}" font-size="10" text-anchor="end" '
            f'fill="currentColor">{_esc(label)}</text>'
        )

    # x 軸ラベル（間引いて 4〜6 個）
    for i in _thin_indices(n, 5):
        parts.append(
            f'<text x="{px(i):.2f}" y="{height - 6:g}" font-size="10" text-anchor="middle" '
            f'fill="currentColor">{_esc(_axis_date_label(dates[i]))}</text>'
        )

    # 凡例（系列が2つ以上のときだけ）
    if has_legend:
        lx = frame["left"]
        ly = 12.0
        for i, s in enumerate(series):
            color = _series_color(s, i)
            label = str(s.get("label", ""))
            parts.append(f'<line x1="{lx:.2f}" y1="{ly:.2f}" x2="{lx + 14:.2f}" y2="{ly:.2f}" stroke="{color}" stroke-width="3"/>')
            parts.append(f'<text x="{lx + 18:.2f}" y="{ly + 3:.2f}" font-size="10" fill="currentColor">{_esc(label)}</text>')
            lx += 18 + len(label) * 7 + 14

    # 各系列の折れ線
    for i, s in enumerate(series):
        values = (s.get("values") or [])[:n]
        color = _series_color(s, i)
        points = [(px(j), None if _is_missing(v) else py(float(v))) for j, v in enumerate(values)]
        for d in _line_paths(points):
            parts.append(
                f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
            )

    # イベントマーカー
    date_index = {d: i for i, d in enumerate(dates)}
    for m in markers or []:
        idx = date_index.get(str(m.get("date", "")))
        style = _MARKER_STYLE.get(m.get("kind"))
        if idx is None or style is None:
            continue
        x = px(idx)
        color = _color_var(style["color"])
        title = _esc(str(m.get("label", "")))
        title_tag = f"<title>{title}</title>" if title else ""
        if style["shape"] == "up":
            shape = _triangle_up(x, frame["bottom"] - 5, 5, color)
        elif style["shape"] == "down":
            shape = _triangle_down(x, frame["top"] + 5, 5, color)
        else:
            shape = f'<circle cx="{x:.2f}" cy="6" r="3.5" fill="{color}"/>'
        parts.append(f"<g>{title_tag}{shape}</g>")

    open_tag, head = _svg_header(
        width, height, aria_title="時系列の折れ線グラフ", aria_desc=aria, extra_class="cc-chart--line"
    )
    return f"{open_tag}{head}{''.join(parts)}</svg>"


# ---------------------------------------------------------------------------
# 業績推移（棒＋第2軸の折れ線）／キャッシュフロー（棒のみ）
# ---------------------------------------------------------------------------


def _bars_svg(
    categories: list[str],
    bar_series: list[dict],
    line: dict | None,
    *,
    width: float,
    height: float,
    aria: str,
    kind: str,
    aria_title: str,
) -> str:
    n = len(categories)
    all_bar_values = [v for s in bar_series for v in (s.get("values") or [])]
    has_bar_data = bool(bar_series) and any(not _is_missing(v) for v in all_bar_values)
    if n == 0 or not has_bar_data:
        return placeholder("データなし", width=width, height=height)

    line_values = (line or {}).get("values") or []
    has_line = bool(line) and any(not _is_missing(v) for v in line_values)
    right_margin = 46 if has_line else 12
    legend_needed = len(bar_series) > 1 or has_line
    top_margin = 20 + (14 if legend_needed else 0)
    frame = _plot_area(width, height, right=right_margin, top=top_margin)

    # 棒の数値軸は必ず 0 を含める（切り詰めない）
    lo, hi = _numeric_domain(all_bar_values, include_zero=True)

    group_w = frame["width"] / n
    m = max(len(bar_series), 1)
    bar_gap = group_w * 0.2
    bar_w = (group_w - bar_gap) / m

    def gx(i: int) -> float:
        return frame["left"] + i * group_w

    def y_val(v: float) -> float:
        return _y_pixel(v, lo, hi, frame["top"], frame["height"])

    zero_y = y_val(0.0)

    parts: list[str] = []

    grid_n = 4
    for g in range(grid_n + 1):
        gv = lo + (hi - lo) * g / grid_n
        gy = y_val(gv)
        parts.append(
            f'<line x1="{frame["left"]:.2f}" y1="{gy:.2f}" x2="{frame["right"]:.2f}" y2="{gy:.2f}" '
            f'stroke="currentColor" stroke-opacity="0.12"/>'
        )
        parts.append(
            f'<text x="{frame["left"] - 6:.2f}" y="{gy + 3:.2f}" font-size="10" text-anchor="end" '
            f'fill="currentColor">{_esc(format_number(gv))}</text>'
        )
    # 0 ライン（負の値がある棒グラフでは特に重要なので、はっきり描く）
    parts.append(
        f'<line x1="{frame["left"]:.2f}" y1="{zero_y:.2f}" x2="{frame["right"]:.2f}" y2="{zero_y:.2f}" '
        f'stroke="currentColor" stroke-opacity="0.5" stroke-width="1.2"/>'
    )

    for si, s in enumerate(bar_series):
        color = _series_color(s, si)
        values = s.get("values") or []
        for ci in range(n):
            v = values[ci] if ci < len(values) else None
            if _is_missing(v):
                continue
            bx = gx(ci) + bar_gap / 2 + si * bar_w
            by = y_val(float(v))
            y_top = min(by, zero_y)
            bh = max(abs(by - zero_y), 0.5)
            parts.append(
                f'<rect x="{bx:.2f}" y="{y_top:.2f}" width="{max(bar_w - 2, 1):.2f}" '
                f'height="{bh:.2f}" fill="{color}"/>'
            )

    for ci, cat in enumerate(categories):
        parts.append(
            f'<text x="{gx(ci) + group_w / 2:.2f}" y="{height - 6:g}" font-size="10" '
            f'text-anchor="middle" fill="currentColor">{_esc(cat)}</text>'
        )

    if has_line:
        llo, lhi = _numeric_domain(line_values, include_zero=False)

        def ly_val(v: float) -> float:
            return _y_pixel(v, llo, lhi, frame["top"], frame["height"])

        lcolor = _color_var((line or {}).get("color") or "blue")
        points: list[tuple[float, float | None]] = []
        for ci in range(n):
            v = line_values[ci] if ci < len(line_values) else None
            x = gx(ci) + group_w / 2
            points.append((x, None if _is_missing(v) else ly_val(float(v))))
        for d in _line_paths(points):
            parts.append(f'<path d="{d}" fill="none" stroke="{lcolor}" stroke-width="2"/>')
        for x, y in points:
            if y is not None:
                parts.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.5" fill="{lcolor}"/>')
        unit = (line or {}).get("unit", "")
        for g in range(grid_n + 1):
            gv = llo + (lhi - llo) * g / grid_n
            gy = ly_val(gv)
            parts.append(
                f'<text x="{frame["right"] + 6:.2f}" y="{gy + 3:.2f}" font-size="10" '
                f'text-anchor="start" fill="currentColor">{_esc(format_number(gv, digits=1) + unit)}</text>'
            )

    if legend_needed:
        lx = frame["left"]
        ly_pos = 12.0
        legend_items = [(s.get("label", ""), _series_color(s, i)) for i, s in enumerate(bar_series)]
        if has_line:
            legend_items.append(((line or {}).get("label", ""), _color_var((line or {}).get("color") or "blue")))
        for label, color in legend_items:
            label = str(label)
            parts.append(f'<rect x="{lx:.2f}" y="{ly_pos - 8:.2f}" width="10" height="10" fill="{color}"/>')
            parts.append(f'<text x="{lx + 14:.2f}" y="{ly_pos + 1:.2f}" font-size="10" fill="currentColor">{_esc(label)}</text>')
            lx += 14 + len(label) * 7 + 14

    open_tag, head = _svg_header(width, height, aria_title=aria_title, aria_desc=aria, extra_class=f"cc-chart--{kind}")
    return f"{open_tag}{head}{''.join(parts)}</svg>"


def bars_with_line(
    categories: Any,
    bars: Any,
    line: dict | None = None,
    *,
    width: int = 680,
    height: int = 260,
    aria: str = "",
) -> str:
    """業績推移の棒グラフ（＋任意で第2軸の折れ線）。棒の数値軸は必ず 0 始まりにする。"""
    return _bars_svg(
        [str(c) for c in (categories or [])],
        list(bars or []),
        line,
        width=width,
        height=height,
        aria=aria,
        kind="bars-line",
        aria_title="業績推移の棒グラフ",
    )


def grouped_bars(
    categories: Any, series: Any, *, width: int = 680, height: int = 220, aria: str = ""
) -> str:
    """キャッシュフロー用の棒グラフ。負の値が主役なので 0 ラインを必ず描く。"""
    return _bars_svg(
        [str(c) for c in (categories or [])],
        list(series or []),
        None,
        width=width,
        height=height,
        aria=aria,
        kind="grouped-bars",
        aria_title="キャッシュフローの棒グラフ",
    )


# ---------------------------------------------------------------------------
# レンジの中の現在値（ブレット）
# ---------------------------------------------------------------------------


def bullet(
    low: Any,
    high: Any,
    current: Any,
    *,
    label: str = "",
    width: int = 320,
    height: int = 44,
    aria: str = "",
) -> str:
    """過去数期のレンジ（帯）の中で現在値がどこにあるかを示す横棒。"""
    if _is_missing(low) or _is_missing(high) or _is_missing(current):
        return placeholder(label or "データなし", width=width, height=height)

    low_v, high_v = float(low), float(high)
    current_v = float(current)
    if low_v > high_v:
        low_v, high_v = high_v, low_v

    dom_lo = min(low_v, current_v)
    dom_hi = max(high_v, current_v)
    if dom_lo == dom_hi:
        dom_lo -= 1.0
        dom_hi += 1.0
    pad = (dom_hi - dom_lo) * 0.08
    dom_lo -= pad
    dom_hi += pad

    left, right = 8.0, width - 8.0
    track_w = max(right - left, 1.0)

    def x(v: float) -> float:
        return left + (v - dom_lo) / (dom_hi - dom_lo) * track_w

    has_label = bool(label)
    label_h = height * 0.32 if has_label else 0.0
    bar_y = label_h + height * 0.14
    bar_h = height * 0.28

    parts: list[str] = []
    if has_label:
        parts.append(f'<text x="{left:g}" y="{label_h - 2:.2f}" font-size="11" fill="currentColor">{_esc(label)}</text>')

    parts.append(
        f'<rect x="{left:g}" y="{bar_y:.2f}" width="{track_w:.2f}" height="{bar_h:.2f}" '
        f'fill="currentColor" fill-opacity="0.08"/>'
    )
    rx0, rx1 = x(low_v), x(high_v)
    parts.append(
        f'<rect x="{rx0:.2f}" y="{bar_y:.2f}" width="{max(rx1 - rx0, 1):.2f}" height="{bar_h:.2f}" '
        f'fill="{_color_var("skyblue")}"/>'
    )

    cx = x(current_v)
    marker_color = _color_var("vermilion")
    parts.append(
        f'<line x1="{cx:.2f}" y1="{bar_y - 3:.2f}" x2="{cx:.2f}" y2="{bar_y + bar_h + 3:.2f}" '
        f'stroke="{marker_color}" stroke-width="2"/>'
    )

    bottom_text_y = bar_y + bar_h + height * 0.3
    top_text_y = max(bar_y - height * 0.12, 10.0)
    parts.append(
        f'<text x="{left:g}" y="{bottom_text_y:.2f}" font-size="10" text-anchor="start" '
        f'fill="currentColor">{_esc(format_number(low_v))}</text>'
    )
    parts.append(
        f'<text x="{right:g}" y="{bottom_text_y:.2f}" font-size="10" text-anchor="end" '
        f'fill="currentColor">{_esc(format_number(high_v))}</text>'
    )
    parts.append(
        f'<text x="{cx:.2f}" y="{top_text_y:.2f}" font-size="10" text-anchor="middle" '
        f'fill="currentColor">{_esc(format_number(current_v))}</text>'
    )

    aria_desc = aria or (
        f"{label + ' ' if label else ''}現在値 {format_number(current_v)}"
        f"（レンジ {format_number(low_v)}〜{format_number(high_v)}）"
    )
    open_tag, head = _svg_header(
        width, height, aria_title=label or "レンジ内の現在値", aria_desc=aria_desc, extra_class="cc-chart--bullet"
    )
    return f"{open_tag}{head}{''.join(parts)}</svg>"
