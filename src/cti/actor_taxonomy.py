"""アクターの種類を STIX 2.1 の語彙 (threat-actor-type-ov) で導く (2026-09-27)。

辞書の ``family`` 欄は命名系統 (typhoon / panda / bear …、国コード付き) と活動の種類
(ransom_group / spider = 犯罪、hacktivist、state_organ) が同じ欄に混在している。種類は既存の欄
(kind・family・nation・sponsor・sponsor_org) からここ 1 か所で導く — 275 件に欄を書き足さない。

- 国家機関・請負 (kind=organization / contractor) は脅威アクターではなく STIX の identity
  (stix_exporter の扱いと同じ) → None
- 語彙: https://docs.oasis-open.org/cti/stix/v2.1/ の threat-actor-type-ov
"""

from __future__ import annotations

from typing import Protocol

#: 犯罪系の family (辞書の families 節で nation が "cc")
CRIMINAL_FAMILIES: frozenset[str] = frozenset({"ransom_group", "spider"})


def _state_naming_families() -> frozenset[str]:
    """国家系の命名系統 = 辞書の families 節で nation が ISO 国コード (cc / vv 以外) のもの。

    一覧を書き写さず辞書から読む (families 節が SSoT)。
    """
    from src.cti.actor_normalizer import load_actor_families

    return frozenset(
        fid
        for fid, info in load_actor_families().items()
        if len(str(info.get("nation", ""))) == 2 and info.get("nation") not in ("cc", "vv")
    )


class _TypedActor(Protocol):
    @property
    def kind(self) -> str: ...
    @property
    def family(self) -> str | None: ...
    @property
    def nation(self) -> str | None: ...
    @property
    def sponsor(self) -> str | None: ...
    @property
    def sponsor_org(self) -> str | None: ...


def threat_actor_type(actor: _TypedActor) -> str | None:
    """STIX threat-actor-type-ov の値。国家機関・請負は None (identity)。"""
    if actor.kind in ("organization", "contractor"):
        return None
    family = actor.family or ""
    if family in CRIMINAL_FAMILIES:
        return "crime-syndicate"
    if family == "hacktivist":
        return "activist"
    if family == "state_organ" or family in _state_naming_families():
        return "nation-state"
    if actor.sponsor_org or actor.sponsor or actor.nation:
        return "nation-state"
    return "unknown"
