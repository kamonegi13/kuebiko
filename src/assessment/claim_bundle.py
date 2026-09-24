"""detect の束ね (別々の事案を 1 つの claim にまとめる) を見つける (2026-09-24)。

5 日の replay で 4 モデルすべてが、被害組織も事象も別の記事を「相次いだ」の 1 claim に束ねた。
束ねた情勢は ACH の対象が曖昧になり、以後の割当の磁石にもなる (台帳の割当の実測で、束ねの情勢に
無関係な証拠が 28 件入っていた)。記事どうしを **同じ事象 (群化) に入っている** か **強い鍵
(CVE・被害組織・マルウェア・アクター・作戦名) を共有** で繋ぎ、塊が 2 つ以上なら束ねとみなす。
国や一般語では繋がない (台帳の割当と同じく、国の共有は同一性ではない)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from src.assessment.assignment import STRONG_ANCHOR_TYPES
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.synthesis.grounded.incremental import DetectResult

_log = get_logger(__name__)


#: 要約の類似度がこれ以上なら同じ事象とみなして繋ぐ (実測: 同じ事象 0.87 / 別事案の束ね 最大 0.68)
SAME_EVENT_COS = 0.75


def split_clusters(
    article_ids: Sequence[str],
    *,
    strong_by_aid: Mapping[str, set[str] | frozenset[str]],
    items_by_aid: Mapping[str, set[str] | frozenset[str]],
    vec_by_aid: Mapping[str, Any] | None = None,
) -> list[list[str]]:
    """記事を「同じ事象 / 強い鍵の共有 / 要約がほぼ同じ」で繋いだ塊 (大きい順、塊の中は入力順)。

    ``vec_by_aid`` は正規化済みの要約埋込 (無い記事は類似度で繋がない)。
    """
    ids = list(dict.fromkeys(article_ids))
    parent = {a: a for a in ids}

    def root(a: str) -> str:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    owner: dict[str, str] = {}
    bare: list[str] = []  # 判断材料の無い記事 (固有名詞も事象も無い) — 分ける根拠にしない
    for a in ids:
        keys = {f"item:{i}" for i in items_by_aid.get(a, ())}
        keys |= {k for k in strong_by_aid.get(a, ()) if k.partition(":")[0] in STRONG_ANCHOR_TYPES}
        if not keys:
            bare.append(a)
        for k in keys:
            if k in owner:
                parent[root(a)] = root(owner[k])
            else:
                owner[k] = a
    vecs = vec_by_aid or {}
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if a in vecs and b in vecs and float(vecs[a] @ vecs[b]) >= SAME_EVENT_COS:
                parent[root(a)] = root(b)
                bare = [x for x in bare if x not in (a, b)]
    groups: dict[str, list[str]] = {}
    for a in ids:
        if a not in bare:
            groups.setdefault(root(a), []).append(a)
    order = {a: i for i, a in enumerate(ids)}
    ranked = sorted(groups.values(), key=lambda g: (-len(g), order[g[0]]))
    if not ranked:
        return [list(bare)] if bare else []
    # 判断材料の無い記事は最大の塊に含める (入力順を保つ)
    ranked[0] = sorted([*ranked[0], *bare], key=lambda a: order[a])
    return ranked


def is_bundle(clusters: Sequence[Sequence[str]]) -> bool:
    return len(clusters) >= 2


#: 1 run で書き直しにかける塊の上限 (LLM 呼出の上限。超えた塊は開設しない = 次の run へ)
MAX_REWRITES = 8


async def unbundle_claims(
    detected: DetectResult,
    *,
    strong_by_aid: Mapping[str, set[str] | frozenset[str]],
    items_by_aid: Mapping[str, set[str] | frozenset[str]],
    rewrite: Callable[[list[str]], Awaitable[DetectResult]],
    vec_by_aid: Mapping[str, Any] | None = None,
) -> DetectResult:
    """束ねた claim を開設せず、塊ごとに claim を書き直させる (**塊ごとに別の呼出**)。

    1 回で全部を渡すと書き直しでも束ねうる (プロンプトは「同一事象はまとめよ」) — 塊ごとに
    呼べば塊をまたいで束ねられない。書き直しが塊の外の記事を足しても取り除き、それでも束ねて
    いれば開設しない。失敗した塊も開設しない (記事は未割当に残り、次の run で候補になる)。
    """
    from src.synthesis.grounded.incremental import DetectedClaim, DetectResult

    kept: list[DetectedClaim] = []
    rejected = list(detected.rejected)
    rewrites = 0
    for c in detected.open:
        clusters = split_clusters(
            c.article_ids, strong_by_aid=strong_by_aid, items_by_aid=items_by_aid
        )
        if not is_bundle(clusters):
            kept.append(c)
            continue
        _log.info("detect_bundle_split", claim=c.claim[:80], clusters=[len(g) for g in clusters])
        for cluster in clusters:
            if rewrites >= MAX_REWRITES:
                _log.warning("detect_bundle_rewrite_capped", left=len(cluster))
                continue
            rewrites += 1
            try:
                res = await rewrite(list(cluster))
            except Exception as e:  # noqa: BLE001 — 書き直せない塊は開設しない (次の run へ)
                _log.warning("detect_bundle_rewrite_failed", error=str(e)[:120])
                continue
            rejected.extend(res.rejected)
            for rc in res.open:
                ids = tuple(a for a in rc.article_ids if a in cluster)
                sub = split_clusters(
                    ids,
                    strong_by_aid=strong_by_aid,
                    items_by_aid=items_by_aid,
                    vec_by_aid=vec_by_aid,
                )
                if ids and not is_bundle(sub):
                    kept.append(DetectedClaim(claim=rc.claim, domain=rc.domain, article_ids=ids))
    return DetectResult(open=tuple(kept), rejected=tuple(rejected), overflow=detected.overflow)
