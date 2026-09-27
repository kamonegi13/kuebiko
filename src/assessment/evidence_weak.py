"""弱い証拠の後始末 (2026-09-27) — 割当の関門より前に規則で入った証拠を、同じ関門で採点し直す。

割当規則の精度は anchor 31% / nation 10% / token 4% (Opus 盲検 362 組、2026-09-24)。
09-25 から新しい割当には関門 (`stateful._apply_assign_gate`、ML または埋込 0.6) が掛かるが、
それ以前の証拠 (規則のみ) は台帳に残ったまま ACH・総括の根拠になっていた。

- 判定は**本番の関門と同じ関数** (`_gate_vectors` / `_gate_decider`) を使う。別の判定器を作らない
- 落ちた証拠は行を消さず `weak_at` を刻む (`SituationStore.mark_weak`)。評価の読み取りは
  `_NOT_WEAK` で外す。戻すときは `UPDATE situation_evidence SET weak_at = NULL`
- 対象は event の台帳の規則割当だけ。seed (開設時の記事) と standing の収穫は採点しない
- 埋込が無い / 作れない記事は判定不能として**残す** (関門の失敗で台帳を変えない、本番と同じ)
"""

from __future__ import annotations

from dataclasses import dataclass

from src.assessment.assignment import SituationKeys, build_article_keys, build_situation_keys
from src.assessment.situation_store import SituationStore
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

#: 採点し直す割当の種類 (規則)。seed / standing / standing_declarative は対象外
RULE_KINDS: tuple[str, ...] = ("anchor", "nation", "token")


@dataclass(frozen=True)
class WeakScan:
    """採点の結果。``weak`` = 関門で落ちる (situation_id, article_id)。"""

    checked: int
    weak: tuple[tuple[str, str], ...]
    judge: str


async def scan_weak_evidence(repo: RunHistoryRepository, store: SituationStore) -> WeakScan:
    """未印の規則割当を本番の関門で採点し、落ちる組を返す (書き込まない)。"""
    from src.assessment.stateful import _gate_decider, _gate_vectors

    rows = [r for r in store.load_situations(("active", "dormant")) if r.kind == "event"]
    kept = store.unmarked_evidence_by_rule([r.situation_id for r in rows], rules=RULE_KINDS)
    if not kept:
        return WeakScan(checked=0, weak=(), judge="none")
    by_sid = {r.situation_id: r for r in rows}
    sit_keys: dict[str, SituationKeys] = {}
    for sid in kept:
        rev = store.latest_revision(sid)
        sit_keys[sid] = build_situation_keys(by_sid[sid], claim_type=rev.claim_type if rev else "")
    aids = sorted({a for v in kept.values() for a in v})
    arts = repo.get_articles_by_ids(aids)
    ents = repo.entity_keys_for_articles(aids)
    art_keys = {
        a: build_article_keys(
            article_id=a,
            title=(arts[a].title or "") if a in arts else "",
            entity_keys=frozenset(ents.get(a, set())),
        )
        for a in aids
    }
    titles = {sid: by_sid[sid].title for sid in kept}
    sit_vecs, art_vecs = await _gate_vectors(kept, titles, repo)
    decide, judge = _gate_decider(
        kept, sit_vecs, art_vecs, repo=repo, art_keys=art_keys, sit_keys=sit_keys, store=store
    )
    weak = tuple(
        (sid, aid)
        for sid in sorted(kept)
        for aid in kept[sid]
        if art_vecs.get(aid) is not None
        and sit_vecs.get(sid) is not None
        and not decide(sid, aid)[0]
    )
    checked = sum(len(v) for v in kept.values())
    _log.info("evidence_weak_scan", checked=checked, weak=len(weak), judge=judge)
    return WeakScan(checked=checked, weak=weak, judge=judge)
