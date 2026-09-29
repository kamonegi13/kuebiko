"""Spotlight の「線でたどった関連事象」の節 — GraphRAG の取得 (2026-09-29)。

候補記事が属する事象から、事象どうしの線 (``eventnews.relations`` — 本番の関係表示と同じ導出)
をたどり、**候補の外** の関連事象を見出し・日付・共有した指標・出典の記事番号つきで渡す。

実測 (scripts/graphrag_spotlight_compare.py、凍結 15 窓・Opus・盲検): 線つきが 12:3 で勝ち、
根拠の観点は悪化しなかった (8:5)。1 窓の候補 30 件はほぼ別々の事象で、候補どうしの線は
ほとんど無い — 効くのは候補の外へたどる取得の形。

- 線は**確かめる対象**であって主張の根拠ではない。節の末尾に、線に無い関係を推測しない旨を置く
- 同じ出来事の線 (続報・関連・包含) を先に、同じアクターを後に、それぞれ新しい順。上限つき
- 関係表は「今」から引くので、窓の終わりより後に報じられた事象は除く (未来の混入)

既定は off (``SPOTLIGHT_GRAPH_CONTEXT=1`` で on)。ローカルモデルが線を扱えるか確かめてから開く。
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

_FLAG_ENV = "SPOTLIGHT_GRAPH_CONTEXT"
#: 節に載せる線の上限 (プロンプトの肥大を抑える)
MAX_LINES = 25
#: 関係を引く期間 (日)。窓の前の経緯まで届くよう Spotlight の窓 (7 日) より長く
RELATION_DAYS = 120

_REL_LABELS = {
    "follow_up": "続報",
    "incident": "同じ出来事の関連",
    "side": "同じ出来事の別の側面",
    "campaign": "同一キャンペーン",
    "contains": "包含 (まとめ記事)",
    "same_actor": "同じアクター",
}
_BASIS = {"victim": "被害組織", "cve": "CVE", "cap": "道具", "actor": "攻撃者"}
_NOTE = (
    "上の線は、事象が共有する指標 (被害組織・CVE・道具・攻撃者) から機械的に導いたもの"
    "である。書くときは線を根拠として引用してよいが、線に無い事象どうしの関係は推測しない。"
)
_CANDIDATE_HEADERS = ("## この SIR にマッチした", "## この PIR にマッチした")


def graph_context_enabled() -> bool:
    return os.environ.get(_FLAG_ENV, "0") == "1"


def _basis_label(basis: str, actor_name: Callable[[str], str]) -> str:
    kind, _, value = basis.partition(":")
    if kind == "actor":
        value = actor_name(value)
    return f"{_BASIS.get(kind, kind)}: {value}"


def _refs(numbers: Sequence[int]) -> str:
    return "".join(f"[{n}]" for n in sorted(set(numbers)))


def render_graph_context(
    article_ids: Sequence[str],
    *,
    members: Mapping[str, Sequence[str]],
    relations: Mapping[str, Sequence[Any]],
    first_reported: Mapping[str, datetime],
    headlines: Mapping[str, str],
    window_end: datetime,
    actor_name: Callable[[str], str],
    max_lines: int = MAX_LINES,
) -> str:
    """線の節のテキスト (純粋関数)。線が無ければ空文字。

    ``article_ids`` はプロンプトの候補一覧の順 (番号 = 位置 + 1)。
    """
    events: dict[str, list[int]] = defaultdict(list)
    for n, aid in enumerate(article_ids, start=1):
        for ev in members.get(aid, ()):
            events[ev].append(n)
    seen: set[tuple[str, str, str]] = set()
    found: list[tuple[int, float, str]] = []
    for ev in events:
        for r in relations.get(ev, ()):
            other = r.b if r.a == ev else r.a
            first = first_reported.get(other)
            if other == ev or first is None or first >= window_end:
                continue
            key = (min(ev, other), max(ev, other), r.rel_type)
            if key in seen:
                continue
            seen.add(key)
            if other in events:
                target = "記事 " + _refs(events[other])
            elif headlines.get(other):
                target = f"候補外の事象「{headlines[other]}」({first.date().isoformat()})"
            else:
                continue
            basis = "、".join(_basis_label(b, actor_name) for b in r.basis) or "要約の類似"
            label = _REL_LABELS.get(r.rel_type, r.rel_type)
            line = f"- 記事 {_refs(events[ev])} ↔ {target}: {label} (共有: {basis})"
            found.append((1 if r.rel_type == "same_actor" else 0, -first.timestamp(), line))
    if not found:
        return ""
    found.sort(key=lambda t: (t[0], t[1]))
    lines = [t[2] for t in found[:max_lines]]
    return (
        f"## 線でたどった関連事象 ({len(lines)} 本 / 全 {len(found)} 本)\n"
        + "\n".join(lines)
        + f"\n\n{_NOTE}\n\n"
    )


def insert_after_candidates(prompt: str, block: str) -> str:
    """候補一覧の直後 (次の節の見出しの前) に節を差し込む。見出しが無ければ末尾へ。"""
    if not block:
        return prompt
    head = max(prompt.find(h) for h in _CANDIDATE_HEADERS)
    nxt = prompt.find("\n## ", head + 5) if head >= 0 else -1
    if nxt < 0:
        return prompt + "\n\n" + block
    return prompt[: nxt + 1] + block + prompt[nxt + 1 :]


def build_graph_context(repo: Any, article_ids: Sequence[str], window_end: datetime) -> str:
    """DB から線・事象を読んで節を組む (失敗は呼び手が握る)。"""
    from src.cti.actor_normalizer import load_actor_aliases
    from src.eventnews.relations import relations_by_event

    if not article_ids:
        return ""
    relations = relations_by_event(repo, days=RELATION_DAYS)
    marks = ",".join("?" for _ in article_ids)
    with repo._connect() as conn:  # noqa: SLF001 — 読み取りのみ
        member_rows = conn.execute(
            "SELECT m.article_id, m.item_id FROM event_item_members m "
            "JOIN event_items i ON i.id = m.item_id "
            f"WHERE m.article_id IN ({marks}) AND i.merged_into IS NULL",
            tuple(article_ids),
        ).fetchall()
    members: dict[str, list[str]] = defaultdict(list)
    for r in member_rows:
        members[str(r[0])].append(str(r[1]))
    others = {
        (rel.b if rel.a == ev else rel.a)
        for evs in members.values()
        for ev in evs
        for rel in relations.get(ev, ())
    }
    first: dict[str, datetime] = {}
    if others:
        omarks = ",".join("?" for _ in others)
        with repo._connect() as conn:  # noqa: SLF001
            for row in conn.execute(
                f"SELECT id, first_reported_at FROM event_items WHERE id IN ({omarks})",
                tuple(others),
            ).fetchall():
                first[str(row[0])] = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
    versions = repo.latest_event_versions(list(others)) if others else {}
    headlines = {k: str(v.headline) for k, v in versions.items() if v.headline}
    aliases = load_actor_aliases()

    def actor_name(actor_id: str) -> str:
        actor = aliases.by_id(actor_id)
        return actor.canonical if actor is not None else actor_id

    return render_graph_context(
        article_ids,
        members=members,
        relations=relations,
        first_reported=first,
        headlines=headlines,
        window_end=window_end,
        actor_name=actor_name,
    )


__all__ = [
    "MAX_LINES",
    "build_graph_context",
    "graph_context_enabled",
    "insert_after_candidates",
    "render_graph_context",
]
