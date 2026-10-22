"""常設の問いの根拠の候補の範囲 (2026-10-22、調査 data/eval/pir_answer_evidence_2026_10_22.md)。

問いの対象に合わない記事が候補に入り、答えの重みになっていた (直近 30 日で根拠の約 4 割が
非国家アクター・非サイバー記事)。決定論の関門で候補の段で止める:

- (a) 国家の活動を問う問い (対象が敵性国家): 国家系アクターに結び付く記事だけ。
  辞書の nation=ru を持つ ransom_group 等の非国家だけが根拠の記事は外す。
- (b) 破壊・妨害の問い: 範囲は「サイバーの事案」。geopolitical / policy / other は根拠にしない。
  日本・世界の重要インフラの別 (直接 / 間接) は宣言条件の腕が決めており、ここでは落とさない。
- (c) 金銭目的 (financial) の趨勢の問い (犯罪組織の脅威): 範囲は「サイバーの事案」かつ
  非国家の犯罪アクター (ransom_group / spider) に結び付く記事。国家系・ハクティビスト・
  アクター無しの記事は根拠にしない (2026-10-22)。

旗 ``STANDING_EVIDENCE_SCOPE`` (既定 1、0 で旧挙動)。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.cti.importance_v2 import NON_CYBER_CATEGORIES
from src.cti.threat_actor_doctrine import is_state_nation

_FLAG_ENV = "STANDING_EVIDENCE_SCOPE"
_DISRUPTION = "disruption"
_FINANCIAL = "financial"
#: 犯罪組織とみなす辞書 family。NON_STATE_FAMILIES から hacktivist (金銭目的でない) を除く。
CRIMINAL_FAMILIES: frozenset[str] = frozenset({"ransom_group", "spider"})


@dataclass(frozen=True)
class QuestionScope:
    """問い 1 件の候補の範囲。"""

    state_only: bool  # 国家系アクターに結び付く記事だけ
    cyber_only: bool  # サイバーの事案 (NON_CYBER_CATEGORIES 以外) だけ
    criminal_only: bool = False  # 非国家の犯罪アクターに結び付く記事だけ


UNSCOPED = QuestionScope(state_only=False, cyber_only=False)
STATE_SCOPE = QuestionScope(state_only=True, cyber_only=False)


def scope_enabled() -> bool:
    """``STANDING_EVIDENCE_SCOPE`` (既定 on、0 で旧挙動)。"""
    return os.environ.get(_FLAG_ENV, "1").strip() != "0"


def scope_for_question(question: Mapping[str, Any]) -> QuestionScope:
    """昇格した問い (slots を持つ) の範囲。旗が 0 なら制限なし。"""
    if not scope_enabled():
        return UNSCOPED
    slots = question.get("slots") or {}
    subject = str(slots.get("subject") or "").lower()
    threat = str(slots.get("threat") or "")
    return QuestionScope(
        state_only=is_state_nation(subject),
        cyber_only=threat in (_DISRUPTION, _FINANCIAL),
        criminal_only=threat == _FINANCIAL,
    )


def scope_for_seed() -> QuestionScope:
    """seed (敵性 4 カ国の事前配置) の範囲。旗が 0 なら制限なし。"""
    return STATE_SCOPE if scope_enabled() else UNSCOPED


def category_in_scope(scope: QuestionScope, category: str | None) -> bool:
    """記事の category が範囲内か。未知 (空) は判定できないので残す。"""
    if not scope.cyber_only:
        return True
    return (category or "") not in NON_CYBER_CATEGORIES


@lru_cache(maxsize=1)
def _criminal_actor_keys() -> frozenset[str]:
    """辞書の犯罪組織 (CRIMINAL_FAMILIES) の id / canonical / alias (小文字)。"""
    from src.cti.actor_normalizer import load_actor_aliases

    keys: set[str] = set()
    for actor in load_actor_aliases().actors:
        if (actor.family or "") in CRIMINAL_FAMILIES:
            keys |= {actor.id.lower(), actor.canonical.lower(), *(a.lower() for a in actor.aliases)}
    return frozenset(keys)


def criminal_actor_in_scope(scope: QuestionScope, entity_keys: frozenset[str]) -> bool:
    """記事の entity に犯罪組織があるか (``actor:<値>`` 形式)。criminal_only でなければ常に残す。"""
    if not scope.criminal_only:
        return True
    criminal = _criminal_actor_keys()
    return any(
        etype == "actor" and value.lower() in criminal
        for etype, _, value in (k.partition(":") for k in entity_keys)
    )
