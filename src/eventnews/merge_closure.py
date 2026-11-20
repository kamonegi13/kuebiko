"""統合の群に対する後段の関門 (2026-11-20、11-19 監査 D3 I-5・I-6)。

``plan_merges`` は組ごとに関門を当ててから union-find で推移的に結ぶ。そのため:

- **I-6 ハブへの救済**: 辺 1 本の救済は「単独記事 ⇔ 小さな事象」の設計なのに、救済した組の
  相手がほかの辺で勧告のハブ (8 記事以上) に入っていると、論説などがハブに入る
  (決定 401: Siemens の小さな事象を経由して PHP2Shell のハブへ)。
- **I-5 推移閉包の conflict**: A–B・B–C を結ぶと A–C の焦点の conflict が見られず、
  別々の被害者のリーク掲載が 1 つの事象に入る (決定 359〜362)。

どちらも純粋関数。``plan_merges`` が旗に応じて呼ぶ。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from src.eventnews.merge import (  # merge は本モジュールを関数内で読む (循環を避ける)
    VETO_BLOCKING_CLASS,
    LinkVerdict,
    MergeLink,
    VetoedLink,
)

#: 救済の相手が属してはいけない群の大きさ (記事数)。hub_split の HUB_MIN_ARTICLES と同値
HUB_RESCUE_MIN_ARTICLES = 8
CLOSURE_DETAIL_PREFIX = "closure: "


class _Veto(Protocol):
    def __call__(self, item_a: str, item_b: str) -> LinkVerdict | None: ...


def _find(parent: dict[str, str], x: str) -> str:
    while parent.setdefault(x, x) != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def rescues_into_hub(
    links: Sequence[MergeLink], item_sizes: Mapping[str, int], threshold: int
) -> list[tuple[str, str]]:
    """救済の組のうち、救済以外の辺でできる群が ``threshold`` 記事以上の側に繋がるもの。"""
    parent: dict[str, str] = {}
    for link in links:
        if not link.rescued:
            parent[_find(parent, link.item_a)] = _find(parent, link.item_b)
    size: dict[str, int] = {}
    for item, n in item_sizes.items():
        root = _find(parent, item)
        size[root] = size.get(root, 0) + n
    blocked: list[tuple[str, str]] = []
    for link in links:
        if not link.rescued:
            continue
        sides = (_find(parent, link.item_a), _find(parent, link.item_b))
        if max(size.get(r, 0) for r in sides) >= threshold:
            pair = sorted((link.item_a, link.item_b), key=lambda i: item_sizes.get(i, 0))
            blocked.append((pair[0], pair[1]))
    return blocked


def _components(ids: Sequence[str], links: Sequence[MergeLink]) -> list[list[str]]:
    keep = set(ids)
    parent: dict[str, str] = {i: i for i in ids}
    for link in links:
        if link.item_a in keep and link.item_b in keep:
            parent[_find(parent, link.item_a)] = _find(parent, link.item_b)
    comps: dict[str, list[str]] = {}
    for i in ids:
        comps.setdefault(_find(parent, i), []).append(i)
    return list(comps.values())


def split_closure_conflicts(
    ids: Sequence[str],
    links: Sequence[MergeLink],
    first_seen: Mapping[str, object],
    veto: _Veto | Callable[[str, str], LinkVerdict | None],
    *,
    enforce: bool,
    out: list[VetoedLink] | None,
) -> list[list[str]]:
    """群の統合先と各吸収側を照合し直し、conflict の吸収側を外して群を分け直す。

    外した事象は今回は統合しない (次の回に組ごとの照合からやり直す)。残りは繋がりで分け直し、
    統合先を含まない塊も同じ照合を受ける。``enforce`` が False なら記録だけして群は変えない。
    """
    ordered = sorted(ids, key=lambda i: str(first_seen.get(i, "")))
    target, rest = ordered[0], ordered[1:]
    conflicts = []
    for item in rest:
        verdict = veto(target, item)
        if verdict is not None and verdict.klass == VETO_BLOCKING_CLASS:
            conflicts.append(item)
            if out is not None:
                detail = CLOSURE_DETAIL_PREFIX + verdict.detail
                out.append(VetoedLink(target, item, VETO_BLOCKING_CLASS, detail, 0, enforce))
    if not conflicts or not enforce:
        return [list(ids)]
    remaining = [i for i in ordered if i not in set(conflicts)]
    result: list[list[str]] = []
    for comp in _components(remaining, links):
        if len(comp) < 2:
            continue
        if target in comp:
            result.append(comp)
        else:
            result.extend(
                split_closure_conflicts(comp, links, first_seen, veto, enforce=enforce, out=out)
            )
    return result


__all__ = [
    "CLOSURE_DETAIL_PREFIX",
    "HUB_RESCUE_MIN_ARTICLES",
    "rescues_into_hub",
    "split_closure_conflicts",
]
