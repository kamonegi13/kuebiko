"""続報で「何が加わったか」を、生成時の記録から復元する。

事象ニュースは記事が合流するたびに本文を**全面的に書き直す**ので、読み手には
「前からあった内容」と「今回増えた内容」の区別が付かない (2026-08-27 利用者指摘)。

⭐ 版と版の本文を突き合わせて差分を出す方法は**採らない**。実測すると、同じ事実を
述べた 2 つの版で 12 行中 11 行が「新規」と判定された — 生成は毎回ゼロから書き直され、
モデルやプロンプトが変われば表現も総取り替えになるため、文面の一致率は内容の
異同を測らない。**測れないものを差分として提示すれば、それは捏造になる。**

代わりに使うのは、合流を判定した時点で決定論的に記録された ``new_facts_json``
(``state.decide_arrival`` の出力)。どの記事が合流し、どの entity が新しく加わったかが
そのまま入っている。表示は「加わった要素」の列挙に留め、本文には触らない
(生成本文にメタデータを載せない、という既存方針と同じ)。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.eventnews.grouping import join_entity_key
from src.eventnews.models import UPDATE_DRIVER_TYPES

#: 加わりうる entity 型の日本語名。生 enum を画面に出さない (UI 文言規約)。
#: 網羅は ``UPDATE_DRIVER_TYPES`` に対してテストで固定する。
DRIVER_TYPE_LABELS: Mapping[str, str] = {
    "cve": "脆弱性",
    "victim_org": "被害組織",
    "actor": "脅威アクター",
}


def _payload(new_facts_json: str | None) -> dict[str, Any]:
    if not new_facts_json:
        return {}
    try:
        parsed = json.loads(new_facts_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def contributing_article_id(new_facts_json: str | None) -> str | None:
    """その版を作らせた記事の id (無ければ None)。"""
    aid = _payload(new_facts_json).get("article_id")
    return str(aid) if aid else None


def resolve_additions(
    new_facts_json: str | None,
    entity_rows: Iterable[tuple[str, str]],
) -> list[dict[str, Any]]:
    """加わった entity を **原文の表記** に戻して型別に返す。

    ``added_entities`` が持つのは正規化キー (``federalreserveboard``) なので、
    そのままでは読めない。合流した記事の entity 行を同じ ``join_entity_key`` で
    畳んで突き合わせ、原文の表記 (``Federal Reserve Board``) を復元する
    — 別の正規化を書き起こすと SSoT が二重化するため、必ず同じ関数を通す。

    復元できなかったキーは**落とす** (読めない文字列を画面に出さない)。
    """
    added = _payload(new_facts_json).get("added_entities")
    if not isinstance(added, dict):
        return []
    surface: dict[tuple[str, str], str] = {}
    for entity_type, value in entity_rows:
        surface.setdefault(join_entity_key(entity_type, value), value)
    out: list[dict[str, Any]] = []
    for entity_type in UPDATE_DRIVER_TYPES:
        keys = added.get(entity_type)
        if not isinstance(keys, list):
            continue
        resolved = [
            surface[key]
            for key in (join_entity_key(entity_type, str(v)) for v in keys)
            if key in surface
        ]
        if resolved:
            out.append(
                {
                    "type": entity_type,
                    "label": DRIVER_TYPE_LABELS[entity_type],
                    "values": sorted(set(resolved)),
                }
            )
    return out


def corroboration_note(new_facts_json: str | None) -> str:
    """entity 以外の「加わり方」を 1 文にする (裏取り・出典の格上げ)。

    ``media_delta`` は独立媒体が増えたこと、``tier_transition`` は一次情報源
    (公的機関・ベンダ) に届いたことを指す。どちらも事実そのものではなく
    **確度の変化**なので、事実の列挙とは別に出す。
    """
    payload = _payload(new_facts_json)
    notes: list[str] = []
    delta = payload.get("media_delta")
    if isinstance(delta, int) and delta > 0:
        notes.append(f"独立した媒体が {delta} 件増えた")
    transition = payload.get("tier_transition")
    if isinstance(transition, str) and "->" in transition:
        notes.append("一次情報源による発表が加わった")
    if isinstance(payload.get("importance_transition"), str):
        notes.append("重要度が引き上げられた")
    return "、".join(notes)
