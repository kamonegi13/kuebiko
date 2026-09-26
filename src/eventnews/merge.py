"""既にできた事象どうしを突き合わせて統合する (2026-09-21)。

毎時の群化は **新しい記事を既存の事象へ入れる**だけで、**既にできた事象どうし**を
突き合わせない。実測の参加信号は **seed (新規作成) 4,617 に対し既存への参加 146 (3%)**。

⭐ 比較の**単位**が違う:

| | 比較する単位 |
|---|---|
| 毎時の群化 | 1 記事 × 1 事象 |
| **これ** | **記事 × 記事** (事象の枠を外した総当たり) |

事象 A の 3 本目と事象 B の 2 本目が似ている、という関係に毎時は到達できない。
長期化する事案ほど割れる = **追跡価値が高いものほど不利**だった (実例: さくら
インターネット事業者の大規模漏えいの続報が 25-26 日差で別事象、Citrix の「修正」と「悪用確認」が
18 日差で分断、PaperCut の 48 記事が 19 事象)。

⭐ **時間差の上限は設けない**。実測 (全期間・候補ペア 62,884):

| 時間差の上限 | 畳む事象 |
|---|---|
| 7 日 (旧 retro_merge) | 599 |
| 30 日 | 1,015 |
| **無制限** | **1,038** |

無制限でも 30 日と実質同じ = 時間が離れた記事はそもそもエッジ判定を通らない。
**判定自体が十分に厳しく、時間の上限は安全弁として機能していない**ので、恣意的な
定数を 1 つ減らす。計算は 3 秒 (ML のみ)。

⚠ 旧 `scripts/retro_merge_events.py` は **`len(member_ids)==1` の単独事象しか見ない**
ため、大群どうし (PaperCut の 11/8/7 件) は永久に統合されなかった。ここでは全事象を見る。

⚠ 一括勧告 (ハブ) の抑止は既存の仕組みに任せる — `edge_is_allowed` と ML の
`kind_advisory_vs_incident`、そして毎時側の定足数ガード。ここで自前の条件を書かない
(2026-08-31 に遡及側が自前の条件を持っていてガードが片方だけ効かなかった)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from src.eventnews.grouping import edge_is_allowed
from src.eventnews.models import MemberArticle

#: 事象どうしを結ぶのに要る辺の本数。**1 本では結ばない**。
#: ⭐ 一括勧告 (ハブ) は「多数の無関係な事案と CVE を共有する」ため、相手ごとに
#:   1 本ずつ細い辺を張る。毎時側の `_quorum_blocks` (辺 1 本では参加させない、
#:   2026-09-03) と同じ思想を統合側へ移植する。
#: ⚠ 記事の 88% が未分類なので `event_kind` による除外は当てにならない
#:   (VMware の 38 記事のうち 28 件が未分類)。**構造で見る**。
MIN_EDGES_BETWEEN_ITEMS = 2


@dataclass(frozen=True)
class MergeGroup:
    """統合する事象の組。``target`` が統合先 (最初に立った事象)。"""

    target: str
    absorbed: tuple[str, ...]


def candidate_pairs_by_entity(
    entities: Mapping[str, frozenset[tuple[str, str]]],
    *,
    vectors: Mapping[str, np.ndarray],
    hub_cap: int = 400,
) -> set[tuple[str, str]]:
    """共有 entity で索引を張って候補対を作る (純粋関数)。

    ⚠ ``hub_cap`` を超える汎用 entity は飛ばす。「ランサムウェア」のような語は
    数千記事に付き、総当たりが爆発するうえ意味のある信号にならない。
    """
    by_ent: dict[tuple[str, str], list[str]] = {}
    for aid, es in entities.items():
        for e in es:
            by_ent.setdefault(e, []).append(aid)
    pairs: set[tuple[str, str]] = set()
    for aids in by_ent.values():
        if len(aids) > hub_cap:
            continue
        for i in range(len(aids)):
            for j in range(i + 1, len(aids)):
                a, b = sorted((aids[i], aids[j]))
                if a in vectors and b in vectors:
                    pairs.add((a, b))
    return pairs


def _root(parent: dict[str, str], x: str) -> str:
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def plan_merges(
    *,
    item_of: Mapping[str, str],
    first_seen: Mapping[str, object],
    entities: Mapping[str, frozenset[tuple[str, str]]],
    vectors: Mapping[str, np.ndarray],
    approved: Sequence[tuple[str, str]] | None = None,
    hub_cap: int = 400,
    min_edges: int = MIN_EDGES_BETWEEN_ITEMS,
    single_edge_ok: set[tuple[str, str]] | None = None,
) -> list[MergeGroup]:
    """統合する群を決める (純粋関数)。

    Args:
        item_of: article_id → item_id
        first_seen: item_id → 最初の報道時刻 (統合先の決定に使う)
        entities: article_id → 参加判定用 entity
        vectors: article_id → 正規化済み埋込
        approved: ML が承認した記事対。**None なら決定論のみ** (ML 不在時の縮退)
        min_edges: 2 つの事象を結ぶのに要る辺の本数。1 だとハブが多数を吸い込む
            (実測: 決定論のみで VMware の一括勧告が 82 事象・277 記事を吸収)
        single_edge_ok: 1 本でも結んでよい記事対 (ML 承認かつまとめ系の特徴が立たない)。
            **記事 1 件の事象** がちょうど 1 つの事象へ辺を持つときだけ使う (2026-09-27)。
            相手が 1 件の事象なら辺は構造上 1 本しか張れず、本数の規則では永久に取り残される
            (盲検: まとめ系を除くと 40/41 正しい)。2 つ以上の事象へ辺を持つ単独記事は救わない
            (2 群を橋渡しして連鎖させない)
    """
    pairs = candidate_pairs_by_entity(entities, vectors=vectors, hub_cap=hub_cap)
    allow = set(approved) if approved is not None else None
    # ⭐ 事象の組ごとに辺を数える。**1 本では結ばない** (ハブ対策)。
    edge_count: dict[tuple[str, str], int] = {}
    edge_articles: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for a, b in pairs:
        ia, ib = item_of.get(a), item_of.get(b)
        if ia is None or ib is None or ia == ib:
            continue
        if allow is not None and (a, b) not in allow and (b, a) not in allow:
            continue
        shared = tuple(sorted(entities[a] & entities[b]))
        cos = float(np.dot(vectors[a], vectors[b]))
        if not edge_is_allowed(entities[a], entities[b], shared, cos):
            continue
        key = (ia, ib) if ia < ib else (ib, ia)
        edge_count[key] = edge_count.get(key, 0) + 1
        edge_articles.setdefault(key, []).append((a, b))

    rescued = _singleton_rescues(item_of, edge_articles, single_edge_ok or set())
    parent: dict[str, str] = {}
    for iid in set(item_of.values()):
        parent[iid] = iid
    for (ia, ib), n in edge_count.items():
        if n < min_edges and (ia, ib) not in rescued:
            continue
        ra, rb = _root(parent, ia), _root(parent, ib)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for iid in parent:
        groups.setdefault(_root(parent, iid), []).append(iid)
    out: list[MergeGroup] = []
    for ids in groups.values():
        if len(ids) < 2:
            continue
        # ⭐ 統合先は **最初に立った事象** — URL がそこに残る
        ordered = sorted(ids, key=lambda i: str(first_seen.get(i, "")))
        out.append(MergeGroup(target=ordered[0], absorbed=tuple(ordered[1:])))
    return out


def _singleton_rescues(
    item_of: Mapping[str, str],
    edge_articles: Mapping[tuple[str, str], list[tuple[str, str]]],
    single_edge_ok: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    """本数が足りなくても結ぶ事象の組 (記事 1 件の事象の救済、pure)。"""
    if not single_edge_ok:
        return set()
    size: dict[str, int] = {}
    for iid in item_of.values():
        size[iid] = size.get(iid, 0) + 1
    ok = {tuple(sorted(p)) for p in single_edge_ok}
    # 単独事象ごとに、強い辺で繋がる相手の事象を集める
    partners: dict[str, set[str]] = {}
    for (ia, ib), arts in edge_articles.items():
        if not any(tuple(sorted(p)) in ok for p in arts):
            continue
        for single, other in ((ia, ib), (ib, ia)):
            if size.get(single) == 1:
                partners.setdefault(single, set()).add(other)
    out: set[tuple[str, str]] = set()
    for single, others in partners.items():
        if len(others) != 1:
            continue  # 2 つ以上へ繋がる単独記事は橋渡しになるので救わない
        other = next(iter(others))
        out.add((single, other) if single < other else (other, single))
    return out


def edge_inputs(members: Mapping[str, MemberArticle]) -> list[tuple[str, str, str]]:
    """``build_join_entities`` へ渡す (article_id, entity_type, value) の並び。"""
    from src.eventnews.models import JOIN_ENTITY_TYPES

    return [
        (aid, et, val)
        for aid, m in members.items()
        for et, val in m.entities
        if et in JOIN_ENTITY_TYPES
    ]
