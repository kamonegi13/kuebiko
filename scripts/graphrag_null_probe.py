"""GraphRAG の専用の線を型ごとの対照 (並べ替え) と比べる。

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app -v $PWD/src:/app/src:ro \
        -v $PWD/scripts:/app/scripts:ro -v $PWD/data:/app/data kuebiko \
        python scripts/graphrag_null_probe.py
"""

from __future__ import annotations

from src.cti.actor_normalizer import load_actor_aliases
from src.cti.threat_actor_doctrine import is_state_actor
from src.eventnews.relation_features import actor_helpers, load_event_features
from src.eventnews.relations import DerivedRelation, EventFeatures, derive_graphrag_extra_relations
from src.graph.nullmodel import null_results
from src.storage.run_history import RunHistoryRepository

ROUNDS = 200
DAYS = 60


def main() -> None:
    repo = RunHistoryRepository()
    reg = load_actor_aliases()
    nation_of, _ = actor_helpers()

    def is_state(actor_id: str) -> bool:
        a = reg.by_id(actor_id)
        return a is not None and is_state_actor(a.nation, a.family)

    events = sorted(load_event_features(repo, days=DAYS), key=lambda e: e.first)

    def derive(evs: list[EventFeatures]) -> list[DerivedRelation]:
        return derive_graphrag_extra_relations(evs, nation_of=nation_of, is_state_actor_id=is_state)

    print(f"期間 {DAYS} 日 / 事象 {len(events)} 件 / 入れ替え {ROUNDS} 回")
    for t, r in null_results(events, derive, rounds=ROUNDS).items():
        lo, hi = r.null_interval()
        l_lo, l_hi = r.lift_interval()
        fmt = lambda x: "-" if x is None else f"{x:.2f}"  # noqa: E731
        lift = r.lift
        print(
            f"{t}: 実測 {r.real} / 対照平均 {r.mean:.1f} (95% {lo:.0f}-{hi:.0f})"
            f" / lift {fmt(lift)} (95% {fmt(l_lo)}-{fmt(l_hi)})"
            f" / 偶然並み {r.is_chance_level()} / 対照が不変 {r.is_degenerate}"
        )


if __name__ == "__main__":
    main()
