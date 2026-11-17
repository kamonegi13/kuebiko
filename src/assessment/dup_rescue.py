"""同じ事案の二重開設を、開設の直前に既存の情勢へ合流させる (``LEDGER_DUP_RESCUE``)。

docs/ledger_upstream_fixes_2026_11_17.md §2。新しい塊の強い手がかり (CVE・被害組織) が 2 つ以上、
active/dormant の event 情勢のそれと一致し、**かつ** 評価済み・非 weak の根拠が事象の根
(``coalesce(merged_into, item_id)``) を塊の記事と共有する情勢があれば、新規に開かずそこへ合流する。

旗: 0 = 何もしない / shadow = 合流したはずの先を ``detect_bundles`` (variant=dup_rescue_shadow) に
記録するだけ / 1 = 合流させる。判定は純粋関数 (``decide_dup_rescue``)、DB は ``load_*`` に閉じる。
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.assessment.detect_bundle_log import BundleRecord, make_claim_id
from src.assessment.ledger_flags import dup_rescue_mode
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.assignment import SituationKeys
    from src.assessment.situation_store import SituationStore

_log = get_logger(__name__)

#: 合流の手がかりに数える強い手がかりの型
RESCUE_ANCHOR_TYPES: frozenset[str] = frozenset({"cve", "victim_org"})
#: 一致が必要な強い手がかりの数
MIN_SHARED_ANCHORS = 2
_SQL_CHUNK = 400
_ROOT_EXPR = "COALESCE(NULLIF(i.merged_into, ''), m.item_id)"


@dataclass(frozen=True)
class RescueCandidate:
    situation_id: str
    anchors: frozenset[str]  # "type:value" (RESCUE_ANCHOR_TYPES のみ)
    roots: frozenset[str]  # 評価済み・非 weak の根拠が属する事象の根


@dataclass(frozen=True)
class RescueVerdict:
    situation_id: str
    shared_anchors: tuple[str, ...]
    shared_roots: tuple[str, ...]

    @property
    def reason(self) -> str:
        return (
            f"dup_rescue: 強い手がかり {len(self.shared_anchors)} 件・事象の根 "
            f"{len(self.shared_roots)} 件が一致"
        )


def rescue_anchors(keys: Iterable[str]) -> frozenset[str]:
    """ "type:value" キーのうち合流の手がかりに数える型だけ。"""
    return frozenset(k for k in keys if k.split(":", 1)[0] in RESCUE_ANCHOR_TYPES)


def decide_dup_rescue(
    cluster_anchors: Collection[str],
    cluster_roots: Collection[str],
    candidates: Sequence[RescueCandidate],
) -> RescueVerdict | None:
    """合流先を 1 つ選ぶ (純粋関数)。手がかりの一致数 → 根の一致数の降順、同点は id 昇順。"""
    anchors = rescue_anchors(cluster_anchors)
    roots = frozenset(cluster_roots)
    if len(anchors) < MIN_SHARED_ANCHORS or not roots:
        return None
    best: tuple[int, int, str, RescueVerdict] | None = None
    for c in candidates:
        shared_a = anchors & c.anchors
        shared_r = roots & c.roots
        if len(shared_a) < MIN_SHARED_ANCHORS or not shared_r:
            continue
        verdict = RescueVerdict(c.situation_id, tuple(sorted(shared_a)), tuple(sorted(shared_r)))
        key = (-len(shared_a), -len(shared_r), c.situation_id)
        if best is None or key < best[:3]:
            best = (*key, verdict)
    return None if best is None else best[3]


def load_cluster_roots(store: SituationStore, article_ids: Sequence[str]) -> frozenset[str]:
    """記事群が属する事象の根 (``coalesce(merged_into, item_id)``)。"""
    ids = list(dict.fromkeys(article_ids))
    roots: set[str] = set()
    with store._repo._connect() as conn:  # noqa: SLF001 — store と同じ接続 seam
        for i in range(0, len(ids), _SQL_CHUNK):
            chunk = ids[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT DISTINCT {_ROOT_EXPR} AS root FROM event_item_members m"  # noqa: S608
                f" JOIN event_items i ON i.id = m.item_id WHERE m.article_id IN ({ph})",
                chunk,
            ).fetchall()
            roots.update(str(r["root"]) for r in rows)
    return frozenset(roots)


def load_situation_roots(
    store: SituationStore, situation_ids: Sequence[str]
) -> dict[str, frozenset[str]]:
    """情勢 → 評価済み・非 weak の根拠が属する事象の根。"""
    out: dict[str, set[str]] = {}
    sids = list(dict.fromkeys(situation_ids))
    with store._repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(sids), _SQL_CHUNK):
            chunk = sids[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT e.situation_id AS sid, {_ROOT_EXPR} AS root FROM situation_evidence e"  # noqa: S608
                " JOIN event_item_members m ON m.article_id = e.article_id"
                " JOIN event_items i ON i.id = m.item_id"
                f" WHERE e.situation_id IN ({ph}) AND e.assessed_at IS NOT NULL"
                " AND e.weak_at IS NULL",
                chunk,
            ).fetchall()
            for r in rows:
                out.setdefault(str(r["sid"]), set()).add(str(r["root"]))
    return {s: frozenset(v) for s, v in out.items()}


def _find_verdict(
    store: SituationStore,
    sit_keys: Sequence[SituationKeys],
    entities_by_id: Mapping[str, set[str]],
    article_ids: Sequence[str],
) -> RescueVerdict | None:
    cluster_anchors = rescue_anchors(k for a in article_ids for k in entities_by_id.get(a, set()))
    if len(cluster_anchors) < MIN_SHARED_ANCHORS:
        return None
    pre = [
        (k.row.situation_id, rescue_anchors(k.row.anchors))
        for k in sit_keys
        if len(rescue_anchors(k.row.anchors) & cluster_anchors) >= MIN_SHARED_ANCHORS
    ]
    if not pre:
        return None
    roots_by_sid = load_situation_roots(store, [sid for sid, _ in pre])
    cands = [
        RescueCandidate(sid, anchors, roots_by_sid.get(sid, frozenset())) for sid, anchors in pre
    ]
    return decide_dup_rescue(cluster_anchors, load_cluster_roots(store, article_ids), cands)


def try_rescue(
    *,
    store: SituationStore,
    sit_keys: Sequence[SituationKeys],
    entities_by_id: Mapping[str, set[str]],
    article_ids: Sequence[str],
    claim: str,
    run_at: str | None = None,
) -> RescueVerdict | None:
    """旗に従って合流先を返す。off / shadow は None (shadow は記録だけ)。開設は止めない。"""
    mode = dup_rescue_mode()
    if mode == "off" or not article_ids:
        return None
    try:
        verdict = _find_verdict(store, sit_keys, entities_by_id, article_ids)
    except Exception as e:  # noqa: BLE001 — 救済の失敗で開設を止めない
        _log.warning("dup_rescue_failed", error=type(e).__name__)
        return None
    if verdict is None:
        return None
    _log.info(
        "ledger_dup_rescue",
        mode=mode,
        situation_id=verdict.situation_id,
        anchors=len(verdict.shared_anchors),
        roots=len(verdict.shared_roots),
    )
    if mode == "on":
        return verdict
    _record_shadow(store, verdict, article_ids, claim, run_at or datetime.now(UTC).isoformat())
    return None


def _record_shadow(
    store: SituationStore,
    verdict: RescueVerdict,
    article_ids: Sequence[str],
    claim: str,
    run_at: str,
) -> None:
    try:
        store.record_detect_bundles(
            [
                BundleRecord(
                    run_at=run_at,
                    variant="dup_rescue_shadow",
                    claim_id=make_claim_id(run_at, "dup_rescue_shadow", article_ids, claim),
                    origin="dup_rescue",
                    claim=claim,
                    domain="",
                    llm_article_ids=(),
                    final_article_ids=tuple(article_ids),
                    outcome="merged",
                    situation_id=verdict.situation_id,
                    reason=f"shadow: {verdict.reason}",
                )
            ]
        )
    except Exception as e:  # noqa: BLE001 — 記録の失敗は本流に影響させない
        _log.warning("dup_rescue_record_failed", error=type(e).__name__)
