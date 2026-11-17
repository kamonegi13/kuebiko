"""同じ事案の二重開設を、開設の直前に既存の情勢へ合流させる (``LEDGER_DUP_RESCUE``)。

docs/ledger_upstream_fixes_2026_11_17.md §2・§8 (2 回目)。
**既存の情勢に積み上がった anchor は使わない**
(汚れた根拠から積み上がり、無関係な情勢どうしで重なったため)。合流の条件は両方:

(a) 新しい塊の固有の名前 (記事の entity + 題の固有語、``name_relevance``) のうち 2 種類以上が、
    active/dormant の event 情勢の題・最新の判定文に出る。
(b) その情勢の根拠 (評価済み・非 weak・polarity='supports') と塊の記事が事象の根
    (``coalesce(merged_into, item_id)``) を共有する。seed がまとめ記事の事象の根は除く。

旗: 0 = 何もしない / shadow = 合流したはずの先を ``detect_bundles`` (variant=dup_rescue_shadow) に
記録するだけ / 1 = 合流させる。判定は純粋関数 (``decide_dup_rescue``)、DB は ``load_*`` に閉じる。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.assessment.detect_bundle_log import BundleRecord, make_claim_id
from src.assessment.ledger_flags import dup_rescue_mode
from src.assessment.name_relevance import article_names, matched_names
from src.cti.roundup_detector import is_roundup_article
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.assessment.assignment import SituationKeys
    from src.assessment.situation_store import SituationStore

_log = get_logger(__name__)

#: 塊の固有の名前が状況の題・判定文に出る必要のある種類の数
MIN_SHARED_NAMES = 2
#: 根拠として数える polarity (対象の情勢側)
SUPPORT_POLARITY = "supports"
_SQL_CHUNK = 400
_ROOT_EXPR = "COALESCE(NULLIF(i.merged_into, ''), m.item_id)"


@dataclass(frozen=True)
class RescueCandidate:
    situation_id: str
    text: str  # 情勢の題 + 最新の判定文
    # supports・評価済み・非 weak の根拠が属する事象の根 (まとめ記事の根は除く)
    roots: frozenset[str]


@dataclass(frozen=True)
class RescueVerdict:
    situation_id: str
    shared_names: tuple[str, ...]
    shared_roots: tuple[str, ...]

    @property
    def reason(self) -> str:
        return (
            f"dup_rescue: 固有の名前 {len(self.shared_names)} 件・事象の根 "
            f"{len(self.shared_roots)} 件が一致"
        )


def decide_dup_rescue(
    cluster_names: Collection[str],
    cluster_roots: Collection[str],
    candidates: Sequence[RescueCandidate],
) -> RescueVerdict | None:
    """合流先を 1 つ選ぶ (純粋関数)。名前の一致数 → 根の一致数の降順、同点は id 昇順。"""
    roots = frozenset(cluster_roots)
    if len(cluster_names) < MIN_SHARED_NAMES or not roots:
        return None
    best: tuple[int, int, str, RescueVerdict] | None = None
    for c in candidates:
        shared_r = roots & c.roots
        if not shared_r:
            continue
        shared_n = matched_names(cluster_names, c.text)
        if len(shared_n) < MIN_SHARED_NAMES:
            continue
        verdict = RescueVerdict(c.situation_id, shared_n, tuple(sorted(shared_r)))
        key = (-len(shared_n), -len(shared_r), c.situation_id)
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


def load_supported_roots(
    store: SituationStore, situation_ids: Sequence[str]
) -> dict[str, frozenset[str]]:
    """情勢 → polarity='supports'・評価済み・非 weak の根拠が属する事象の根。"""
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
                " AND e.weak_at IS NULL AND e.polarity = ?",
                [*chunk, SUPPORT_POLARITY],
            ).fetchall()
            for r in rows:
                out.setdefault(str(r["sid"]), set()).add(str(r["root"]))
    return {s: frozenset(v) for s, v in out.items()}


def roundup_roots(store: SituationStore, roots: Collection[str]) -> frozenset[str]:
    """seed (その事象の最古の構成記事) がまとめ記事の事象の根。"""
    rts = list(dict.fromkeys(roots))
    first: dict[str, tuple[str, str]] = {}  # root -> (joined_at, article_id)
    with store._repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(rts), _SQL_CHUNK):
            chunk = rts[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                f"SELECT {_ROOT_EXPR} AS root, m.article_id AS aid, m.joined_at AS at"  # noqa: S608
                " FROM event_item_members m JOIN event_items i ON i.id = m.item_id"
                f" WHERE {_ROOT_EXPR} IN ({ph})",
                chunk,
            ).fetchall():
                key = (str(r["at"]), str(r["aid"]))
                if str(r["root"]) not in first or key < first[str(r["root"])]:
                    first[str(r["root"])] = key
        seeds = sorted({a for _, a in first.values()})
        titles: dict[str, tuple[str, str]] = {}
        for i in range(0, len(seeds), _SQL_CHUNK):
            chunk = seeds[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                "SELECT article_id AS a, MIN(title) AS t, MIN(category) AS c FROM articles"  # noqa: S608
                f" WHERE article_id IN ({ph}) GROUP BY article_id",
                chunk,
            ).fetchall():
                titles[str(r["a"])] = (str(r["t"] or ""), str(r["c"] or ""))
        kinds: dict[str, str] = {}
        for i in range(0, len(seeds), _SQL_CHUNK):
            chunk = seeds[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            for r in conn.execute(
                "SELECT article_id AS a, kind AS k FROM article_kinds"  # noqa: S608
                f" WHERE article_id IN ({ph})",
                chunk,
            ).fetchall():
                kinds[str(r["a"])] = str(r["k"])
    return frozenset(
        root
        for root, (_, aid) in first.items()
        if is_roundup_article(
            titles.get(aid, ("", ""))[0], kind=kinds.get(aid), category=titles.get(aid, ("", ""))[1]
        )
    )


def load_situation_texts(store: SituationStore, situation_ids: Sequence[str]) -> dict[str, str]:
    """情勢 → 題 + 最新の判定文 (最新 revision の claim)。"""
    out: dict[str, str] = {}
    sids = list(dict.fromkeys(situation_ids))
    with store._repo._connect() as conn:  # noqa: SLF001
        for i in range(0, len(sids), _SQL_CHUNK):
            chunk = sids[i : i + _SQL_CHUNK]
            ph = ",".join("?" for _ in chunk)
            rows = conn.execute(
                "SELECT s.situation_id AS sid, s.title AS t, ("  # noqa: S608
                "SELECT r.claim FROM situation_revisions r WHERE r.situation_id = s.situation_id"
                " ORDER BY r.rev DESC LIMIT 1) AS c"
                f" FROM situations s WHERE s.situation_id IN ({ph})",
                chunk,
            ).fetchall()
            for r in rows:
                out[str(r["sid"])] = f"{r['t'] or ''} {r['c'] or ''}"
    return out


def cluster_names(
    article_ids: Sequence[str],
    entities_by_id: Mapping[str, set[str]],
    titles_by_id: Mapping[str, str],
) -> frozenset[str]:
    """塊の記事の固有の名前の和集合。"""
    out: set[str] = set()
    for a in article_ids:
        out |= article_names(titles_by_id.get(a, ""), entities_by_id.get(a, set()))
    return frozenset(out)


def _find_verdict(
    store: SituationStore,
    sit_keys: Sequence[SituationKeys],
    entities_by_id: Mapping[str, set[str]],
    titles_by_id: Mapping[str, str],
    article_ids: Sequence[str],
) -> RescueVerdict | None:
    names = cluster_names(article_ids, entities_by_id, titles_by_id)
    if len(names) < MIN_SHARED_NAMES:
        return None
    sids = [k.row.situation_id for k in sit_keys]
    texts = load_situation_texts(store, sids)
    pre = [sid for sid in sids if len(matched_names(names, texts.get(sid, ""))) >= MIN_SHARED_NAMES]
    if not pre:
        return None
    sit_roots = load_supported_roots(store, pre)
    cluster_roots = load_cluster_roots(store, article_ids)
    all_roots = cluster_roots & frozenset().union(*sit_roots.values()) if sit_roots else frozenset()
    bad = roundup_roots(store, all_roots)
    cands = [RescueCandidate(sid, texts[sid], sit_roots.get(sid, frozenset()) - bad) for sid in pre]
    return decide_dup_rescue(names, cluster_roots - bad, cands)


def try_rescue(
    *,
    store: SituationStore,
    sit_keys: Sequence[SituationKeys],
    entities_by_id: Mapping[str, set[str]],
    titles_by_id: Mapping[str, str],
    article_ids: Sequence[str],
    claim: str,
    run_at: str | None = None,
) -> RescueVerdict | None:
    """旗に従って合流先を返す。off / shadow は None (shadow は記録だけ)。開設は止めない。"""
    mode = dup_rescue_mode()
    if mode == "off" or not article_ids:
        return None
    try:
        verdict = _find_verdict(store, sit_keys, entities_by_id, titles_by_id, article_ids)
    except Exception as e:  # noqa: BLE001 — 救済の失敗で開設を止めない
        _log.warning("dup_rescue_failed", error=type(e).__name__)
        return None
    if verdict is None:
        return None
    _log.info(
        "ledger_dup_rescue",
        mode=mode,
        situation_id=verdict.situation_id,
        names=len(verdict.shared_names),
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
