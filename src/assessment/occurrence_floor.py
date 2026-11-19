"""``LEDGER_OCCURRENCE_FLOOR`` (``0|shadow|1``、設計 §5): 発生の確度を帰属の確度と分けて持つ。

確度は ACH の見立て (誰が・何の目的で) の確度で、事案が起きたことの確度ではない。見立てが
「未確認・虚偽」のとき被害組織の公表は neutral になり強帰属の引き上げも効かず、公表を根拠に
持つ新規の 77% が低確度から始まっていた。帰属の確度 (従来の値) は変えず、事案の型が単発事象で
被害組織の公表・政府の確認があれば、発生の確度 (moderate。high は付けない) を
``confidence_basis`` の印として別に記録する。

- コードの既定 0 = 出力は従来と同一 / shadow = ログ ``ledger_occurrence_floor_shadow`` のみ /
  1 = 印を ``confidence_basis`` に足し (``KeyJudgment.occurrence_confirmed`` が立つ)、
  ``salience.epistemic_weight`` が max(帰属の確度, 発生の確度) で測る。
"""

from __future__ import annotations

import os
from dataclasses import replace
from typing import Any, Literal, Protocol

from src.logging_config import get_logger
from src.synthesis.grounded.estimate import (
    OCCURRENCE_BASES,
    EvidenceItem,
    KeyJudgment,
    occurrence_floor,
)

_log = get_logger(__name__)
FLAG_ENV = "LEDGER_OCCURRENCE_FLOOR"
Mode = Literal["0", "shadow", "1"]


def occurrence_floor_mode() -> Mode:
    v = os.environ.get(FLAG_ENV, "0").strip().lower()
    return v if v in ("shadow", "1") else "0"  # type: ignore[return-value]


class _Repo(Protocol):
    def get_article_grounding(self, article_id: str) -> dict[str, Any] | None: ...

    def entity_keys_for_articles(self, ids: list[str]) -> dict[str, set[str]]: ...


class _Store(Protocol):
    def evidence_bases(self, situation_id: str) -> list[tuple[str, str, str, str]]: ...


def _disclosures(j: KeyJudgment, store: _Store | None) -> tuple[EvidenceItem, ...]:
    """今回の版の証拠 + 状況の既存の証拠 (公表・確認だけ)。版ごとに印が付いたり外れたりしない。"""
    seen = {e.article_id for e in j.evidence}
    extra: list[EvidenceItem] = []
    if store is not None and not j.id.startswith("new-"):
        for aid, basis, polarity, tier in store.evidence_bases(j.id):
            if aid not in seen and basis in OCCURRENCE_BASES:
                extra.append(EvidenceItem(aid, tier, basis, "", polarity))  # type: ignore[arg-type]
    return (*j.evidence, *extra)


def _relevant_ids(evidence: tuple[EvidenceItem, ...], claim: str, repo: _Repo) -> set[str]:
    """見出し・entity の固有の名前が claim と共通する公表・確認の記事 (name_relevance)。"""
    from src.assessment.attacker_claim import is_attacker_claim
    from src.assessment.name_relevance import article_names, is_relevant

    aids = [e.article_id for e in evidence if e.attribution_basis in OCCURRENCE_BASES]
    keys = repo.entity_keys_for_articles(aids) if aids else {}
    out: set[str] = set()
    for aid in aids:
        g = repo.get_article_grounding(aid)
        title = str(g.get("title", "")) if g else ""
        if is_attacker_claim(title, str(g.get("feed_title", "")) if g else ""):
            continue  # 攻撃者側の主張は発生の確認に数えない
        if is_relevant(article_names(title, keys.get(aid, set())), claim):
            out.add(aid)
    return out


def apply_occurrence_floor(
    j: KeyJudgment, *, repo: _Repo | None = None, store: _Store | None = None
) -> KeyJudgment:
    """旗に従い発生の印を足した KeyJudgment (旗 0・対象外・失敗は同じ object。確度は変えない)。

    ``repo`` を渡すと見出しの固有の名前が claim と共通する公表だけを数え、``store`` を渡すと
    状況の既存の証拠も見る。発生の確認が見出しと無関係な記事 (別事案の公表) に由来しないようにする。
    """
    mode = occurrence_floor_mode()
    if mode == "0":
        return j
    try:
        evidence = _disclosures(j, store)
        relevant = _relevant_ids(evidence, j.claim, repo) if repo is not None else None
        floor = occurrence_floor(j.claim_type, evidence, relevant=relevant)
    except Exception as e:  # noqa: BLE001 — 発生の確度の不具合で台帳の更新を落とさない
        _log.warning("ledger_revision_gate_failed", step="occurrence", error=type(e).__name__)
        return j
    if floor is None:
        return j
    occurrence, mark = floor
    _log.info(
        "ledger_occurrence_floor_shadow",
        situation=j.id,
        confidence=j.confidence,
        occurrence_confidence=occurrence,
        basis=mark,
        mode=mode,
    )
    if mode != "1":
        return j
    basis = f"{j.confidence_basis} / {mark}" if j.confidence_basis else mark
    return replace(j, confidence_basis=basis)
