"""配信直前の重複判定の規則変更 3 案 (I-2 / I-3 / I-4) の影の記録 (11-19 監査 D3)。

重複で抑止した記事について、案が入っていれば抑止が外れたかをログ ``dedup_rule_shadow`` に
残すだけ。**判定は一切変えない** (旗 ``DEDUP_RULE_SHADOW``、既定 0 = 計算しない)。
ログは id・一致の層・規則名だけで、題・本文は出さない。例外は握る (型名のみ)。

- I-2 ``cve_focal``: CVE の一致 (cve 層) で、どちらかが一括の勧告 (CVE が FOCAL_CVE_MAX 超)、
  または種別が advisory ⇔ exploitation/derivative の組なら外れる
- I-3 ``victim_focal``: 被害組織の一致 (victim_org 層) で、どちらかの被害組織が 3 以上・
  種別が advisory/roundup/stats・伏字の被害者なら外れる
- I-4 ``high_override``: 新しい記事が high で根が high でないなら外れる (記録のみ。
  同じ run の根の対応が直るまで二重配信になるため shadow のまま)
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from src.eventnews.event_kind import focus_kind_of
from src.eventnews.grouping import is_masked_victim
from src.eventnews.models import FOCAL_CVE_MAX, ROUNDUP_KIND
from src.logging_config import get_logger

_log = get_logger(__name__)

DEDUP_RULE_SHADOW_ENV = "DEDUP_RULE_SHADOW"

LAYER_CVE = "cve"
LAYER_VICTIM_ORG = "victim_org"
LAYER_OTHER = "other"

RULE_CVE_FOCAL = "I-2:cve_focal"
RULE_VICTIM_FOCAL = "I-3:victim_focal"
RULE_HIGH_OVERRIDE = "I-4:high_override"

#: I-3: 被害組織がこの数以上ある記事 (流出一覧・サプライチェーン) は照合の根拠にしない
VICTIM_MANY_MIN = 3
_VICTIM_NON_SUBJECT_KINDS = frozenset({"advisory", ROUNDUP_KIND, "stats"})
_FIX_KIND = "advisory"
_EXPLOIT_KINDS = frozenset({"exploitation", "derivative"})
_CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)
_HIGH = "high"

#: 一致の層の判別 (``DedupGateResult.failure_reason`` の接頭辞)
_LAYER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("cve dedup", LAYER_CVE),
    ("victim_org dedup", LAYER_VICTIM_ORG),
)


@dataclass(frozen=True)
class DedupMatchFacts:
    """抑止が起きた一致の事実。不明な欄は None / 空。"""

    layer: str
    new_kind: str | None = None
    root_kind: str | None = None
    new_cve_count: int = 0
    root_cve_count: int | None = None
    new_victims: tuple[str, ...] = ()
    root_victims: tuple[str, ...] | None = None
    new_importance: str | None = None
    root_importance: str | None = None


@dataclass(frozen=True)
class ShadowVerdict:
    """抑止を外す規則の名前と理由。``lifted_by`` が空なら案は何も外さない。"""

    lifted_by: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def layer_of_reason(failure_reason: str) -> str:
    for prefix, layer in _LAYER_PREFIXES:
        if failure_reason.startswith(prefix):
            return layer
    return LAYER_OTHER


def _is_fix_vs_exploit(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ka, kb = focus_kind_of(a), focus_kind_of(b)
    return (ka == _FIX_KIND and kb in _EXPLOIT_KINDS) or (kb == _FIX_KIND and ka in _EXPLOIT_KINDS)


def _cve_focal_reason(f: DedupMatchFacts) -> str | None:
    if f.layer != LAYER_CVE:
        return None
    if f.new_cve_count > FOCAL_CVE_MAX:
        return "new_bulk_advisory"
    if f.root_cve_count is not None and f.root_cve_count > FOCAL_CVE_MAX:
        return "root_bulk_advisory"
    if _is_fix_vs_exploit(f.new_kind, f.root_kind):
        return "fix_vs_exploit"
    return None


def _victim_focal_reason(f: DedupMatchFacts) -> str | None:
    if f.layer != LAYER_VICTIM_ORG:
        return None
    if len(f.new_victims) >= VICTIM_MANY_MIN:
        return "new_many_victims"
    if f.root_victims is not None and len(f.root_victims) >= VICTIM_MANY_MIN:
        return "root_many_victims"
    if f.new_kind in _VICTIM_NON_SUBJECT_KINDS or f.root_kind in _VICTIM_NON_SUBJECT_KINDS:
        return "non_subject_kind"
    if any(is_masked_victim(v) for v in f.new_victims):
        return "masked_victim"
    return None


def _high_override_reason(f: DedupMatchFacts) -> str | None:
    if f.new_importance == _HIGH and f.root_importance != _HIGH:
        return "high_over_non_high_root"
    return None


def evaluate_shadow_rules(facts: DedupMatchFacts) -> ShadowVerdict:
    """3 案のうち、この一致の抑止を外すものを返す (純粋関数)。"""
    checks = (
        (RULE_CVE_FOCAL, _cve_focal_reason(facts)),
        (RULE_VICTIM_FOCAL, _victim_focal_reason(facts)),
        (RULE_HIGH_OVERRIDE, _high_override_reason(facts)),
    )
    hit = [(rule, why) for rule, why in checks if why is not None]
    return ShadowVerdict(
        lifted_by=tuple(r for r, _ in hit),
        reasons=tuple(f"{r}={w}" for r, w in hit),
    )


def dedup_rule_shadow_enabled() -> bool:
    return os.environ.get(DEDUP_RULE_SHADOW_ENV, "0").strip() == "1"


def _importance_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower() or None


def _count_cves(texts: Sequence[str]) -> int:
    return len({m.upper() for t in texts for m in _CVE_RE.findall(t)})


def _victims_of(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(str(v) for v in raw if str(v).strip())


def build_facts(
    repo: Any, *, msg: Any, art_id: str, prior_article_id: str | None, failure_reason: str
) -> DedupMatchFacts:
    """DB から根の事実を集める (照会は kinds 1・entities 1・articles 1 の最大 3 本)。

    新しい記事の種別は直前の ``involves_roundup`` が ``article_kinds`` へ保存済みのものを読む。
    CVE・被害組織は msg から (まだ DB に無いため)。
    """
    layer = layer_of_reason(failure_reason)
    ids = [art_id] + ([prior_article_id] if prior_article_id else [])
    kinds = repo.get_article_kinds(ids)
    root_cves: int | None = None
    root_victims: tuple[str, ...] | None = None
    root_importance: str | None = None
    if prior_article_id:
        ents = repo.get_article_entities(prior_article_id)
        root_cves = len({v for t, v in ents if t == "cve"})
        root_victims = tuple(v for t, v in ents if t == "victim_org")
        rec = repo.get_articles_by_ids([prior_article_id]).get(prior_article_id)
        root_importance = _importance_str(getattr(rec, "importance", None))
    metadata = getattr(msg, "metadata", None) or {}
    cve_texts = [*(getattr(msg, "iocs", None) or []), getattr(msg, "title", "") or ""]
    return DedupMatchFacts(
        layer=layer,
        new_kind=kinds.get(art_id),
        root_kind=kinds.get(prior_article_id) if prior_article_id else None,
        new_cve_count=_count_cves(cve_texts),
        root_cve_count=root_cves,
        new_victims=_victims_of(metadata.get("victim_orgs")),
        root_victims=root_victims,
        new_importance=_importance_str(getattr(msg, "importance", None)),
        root_importance=root_importance,
    )


def record_dedup_rule_shadow(
    repo: Any, *, msg: Any, art_id: str, prior_article_id: str | None, failure_reason: str
) -> None:
    """抑止 1 件の影の記録。旗 0 なら何もしない。例外は握り、判定には影響しない。"""
    if repo is None or not dedup_rule_shadow_enabled():
        return
    try:
        facts = build_facts(
            repo,
            msg=msg,
            art_id=art_id,
            prior_article_id=prior_article_id,
            failure_reason=failure_reason,
        )
        verdict = evaluate_shadow_rules(facts)
        _log.info(
            "dedup_rule_shadow",
            article_id=art_id,
            prior_article_id=prior_article_id,
            layer=facts.layer,
            lifted_by=list(verdict.lifted_by),
            reasons=list(verdict.reasons),
            new_kind=facts.new_kind,
            root_kind=facts.root_kind,
        )
    except Exception as e:  # noqa: BLE001 — 影の記録は判定に影響させない
        _log.warning("dedup_rule_shadow_failed", error_type=type(e).__name__)


__all__ = [
    "DedupMatchFacts",
    "ShadowVerdict",
    "build_facts",
    "dedup_rule_shadow_enabled",
    "evaluate_shadow_rules",
    "layer_of_reason",
    "record_dedup_rule_shadow",
]
