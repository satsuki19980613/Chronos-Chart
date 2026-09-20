"""disclosures → チャートイベント変換（SPEC §2.5.3・§2.6）。

`app.disclosures.list_for_symbol()` の戻り値を、チャートのマーカーとイベント欄が使える形に整形する。
**DB に触らない純粋な変換関数だけを置く**（テストしやすさのため。CLAUDE.md 不変条件7）。
マーカーの日付を保存はせず、呼ばれるたびに `prices.date` から決め直す。
"""

from __future__ import annotations

import bisect
from datetime import datetime

from .disclosures import TENDER_OFFER_RANGE

# SPEC §2.4.6 の分類表・§2.5.3 のマーカー文字。完全一致をまず引き、当たらなければ
# TOB の範囲（240〜320）を判定する。235/236（内部統制）は範囲の外だが、
# 「完全一致を先に引く」順序自体をテストで固定するため表に残す
_EXACT_LABELS = {
    "120": "有報",
    "130": "有報",
    "140": "四半期",
    "150": "四半期",
    "160": "半期",
    "170": "半期",
    "350": "大量保有",
    "360": "大量保有",
    "220": "自己株",
    "230": "自己株",
    "180": "臨報",
    "190": "臨報",
    "030": "届出",
    "040": "届出",
    "235": "内部統制",
    "236": "内部統制",
}

# 同一日に複数の開示があるときのマーカーの色・形の優先順（SPEC §2.5.3）
_CATEGORY_PRIORITY = {"supply": 0, "report": 1, "other": 2}


def label_for(doc_type_code: str | None) -> str:
    """`docTypeCode` を短いラベルにする（SPEC §2.4.6 の分類表・§2.5.3 のマーカー文字）。

    分類（report/supply/other）は自分で導出せず `disclosures.classify()` を使う。ここでは
    表示用の短い文字列だけを決める。空・None・数字でない値・未知のコードはすべて `その他`。
    """
    code = str(doc_type_code).strip() if doc_type_code is not None else ""
    if code in _EXACT_LABELS:
        return _EXACT_LABELS[code]
    try:
        n = int(code)
    except ValueError:
        return "その他"
    if TENDER_OFFER_RANGE[0] <= n <= TENDER_OFFER_RANGE[1]:
        return "TOB"
    return "その他"


def marker_date(dates: list[str], submit_at: str) -> str | None:
    """`submit_at` を、足のある日付に寄せる（SPEC §2.5.3）。

    `submit_at` の日付部分以降で、`dates`（`prices.date` の昇順リスト）に存在する最初の日を返す。
    祝日カレンダーは持たず、`dates` への二分探索だけで決める。

    - 該当する足がまだ無い（提出日が最後の足より後）→ None
    - **提出日が最初の足より前**（チャートの期間より古い開示）→ None。
      先頭の足に寄せると、その日に起きていない出来事が最左のローソクに付いて誤読されるため
      （本タスクでの決定。SPEC 追記済み）
    - `submit_at` が空・形式が壊れている → None（例外にしない）
    """
    if not dates or not submit_at:
        return None
    date_part = submit_at[:10]
    try:
        datetime.strptime(date_part, "%Y-%m-%d")
    except ValueError:
        return None
    if date_part < dates[0]:
        return None
    idx = bisect.bisect_left(dates, date_part)
    if idx >= len(dates):
        return None
    return dates[idx]


def _pick_category(categories: list[str]) -> str:
    """マーカーの色・形を `supply > report > other` の優先順で決める（SPEC §2.5.3）。"""
    return min(categories, key=lambda c: _CATEGORY_PRIORITY.get(c, len(_CATEGORY_PRIORITY)))


def build(disclosure_list: dict, dates: list[str]) -> dict:
    """`disclosures.list_for_symbol()` の戻り値をチャート用のイベントデータにする（SPEC §2.5.3・§2.6）。

    `items` は `list_for_symbol` の並び（submit_at 降順・同着は doc_id 昇順）をそのまま保つ。
    `markers` は `marker_date` が同じ開示を1つにまとめ、date の昇順で返す。
    取下げ（`withdrawal` が None でも 0 でもない）は `items` には残すがマーカーには出さない。
    その日の開示がすべて取下げならマーカー自体を作らない。
    """
    items: list[dict] = []
    # marker_date（足のある日付）ごとに、まだマーカーに出せる（取下げでない）開示を集める
    groups: dict[str, dict] = {}

    for d in disclosure_list.get("items", []):
        m_date = marker_date(dates, d.get("submit_at"))
        label = label_for(d.get("doc_type_code"))
        item = {
            "doc_id": d.get("doc_id"),
            "submit_at": d.get("submit_at"),
            "marker_date": m_date,
            "category": d.get("category"),
            "label": label,
            "doc_type_code": d.get("doc_type_code"),
            "description": d.get("description"),
            "filer_name": d.get("filer_name"),
            "roles": d.get("roles", []),
            "withdrawal": d.get("withdrawal"),
        }
        items.append(item)

        if m_date is None:
            continue
        withdrawn = item["withdrawal"] is not None and item["withdrawal"] != 0
        if withdrawn:
            continue
        group = groups.setdefault(m_date, {"categories": [], "labels": [], "doc_ids": []})
        group["categories"].append(item["category"])
        group["labels"].append(label)
        group["doc_ids"].append(item["doc_id"])

    markers = []
    for date in sorted(groups):
        group = groups[date]
        doc_ids = sorted(group["doc_ids"])
        count = len(doc_ids)
        text = group["labels"][0] if count == 1 else f"開示{count}件"
        markers.append(
            {
                "id": f"ev:{date}",
                "date": date,
                "category": _pick_category(group["categories"]),
                "text": text,
                "count": count,
                "doc_ids": doc_ids,
            }
        )

    return {
        "items": items,
        "markers": markers,
        "counts": disclosure_list.get("counts", {"report": 0, "supply": 0, "other": 0, "withdrawn": 0, "total": 0}),
        "fetched_days": disclosure_list.get("fetched_days", 0),
    }
