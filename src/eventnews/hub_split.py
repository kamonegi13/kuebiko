"""勧告のハブへの吸収を止める関門 (2026-11-11、``EVENTNEWS_MERGE_HUB_SPLIT``)。

AT-1 v2 (統合の適合率 0.846、目標 0.95) の誤りの中心は、**勧告 (修正の告知) を seed にした
大きな事象 (ハブ) への吸収**だった。悪用の報道・別主体の修正・PoC・教材がハブに吸い込まれ、
いったん混ざると、後から来る悪用の報道も「ハブの中の悪用の記事」と似ているので結ばれる。
焦点の多数決の種別 (``identity.focus._majority_kind``) は混ざった後の記事で決まるので、
この連鎖を止められない。

定義 (docs/event_identity_definition.md): 修正 ⇔ 悪用・派生は**別の事象で関係 (系統) で結ぶ**。
事象の同一性は seed の事案で決まる。そこで、ハブの側は**seed (焦点の錨の記事) の種別**で見る。

規則 (純粋関数 ``hub_split_reason``):

- ハブ = 統合の前の記事数が ``HUB_SPLIT_MIN_ARTICLES`` 以上で、seed の種別が advisory の事象
- 相手の seed の種別が advisory 以外 (悪用・被害・統計・その他) → 結ばない (``kind:<種別>``)
- 相手も advisory でも、辺 1 本の救済で結ぶ組 → 結ばない (``rescued``)。別主体の修正・
  別ベンダーの告知が 1 本の辺で吸い込まれる型 (AT-1 v2 の誤り)
- 相手の種別が未分類 (空) は止めない (材料が無いときは止めない、焦点の照合と同じ方針)

当て直し (AT-1 v2 の 100 組 = この関門を考えた標本なので**見込み**): 止める 16 組 = different 6・
same 6 (6 組とも定義では別の事象 = 悪用の報道をハブの中の悪用の記事と比べて same とした組)・
境界 2・割れ 2。適合率の点推定 0.846 → 0.899。直近 30 日の統合 344 件のうち 42 件を止める
(うち 26 件が 1 つのハブ)。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.eventnews.event_kind import focus_kind_of
from src.eventnews.identity.focus import FocusArticle, compute_focus
from src.eventnews.models import MemberArticle
from src.logging_config import get_logger

_log = get_logger(__name__)

#: 判断の記録の klass (event_merge_vetoes / 判断記録の flags_json)
HUB_SPLIT_CLASS = "hub_split"
#: ハブとみなす統合の前の記事数の下限 (``merge.HUB_MIN_ARTICLES`` と同じ値。merge から
#: import すると循環するので値を写す — 片方だけ変えないこと、テストが同値を固定する)
HUB_SPLIT_MIN_ARTICLES = 8
#: ハブの seed の種別 (修正・勧告)
HUB_SEED_KIND = "advisory"
#: 止める理由の接頭辞
REASON_KIND_PREFIX = "kind:"
REASON_RESCUED = "rescued"


@dataclass(frozen=True)
class ItemProfile:
    """関門が見る事象の要約 (統合の前の状態)。"""

    article_count: int
    seed_kind: str  # focus_kind_of 済み。未分類は ""


def seed_kind(members: Sequence[MemberArticle]) -> str:
    """事象の seed の種別。焦点の錨の記事 (無ければ最初のまとめ以外の記事) の種別。

    未分類 (種別が空) は "" を返す (``focus_kind_of`` は空を other にするが、ここでは
    「材料が無い」と「その他」を分ける)。
    """
    focus = compute_focus(
        [
            FocusArticle(
                article_id=m.article_id, names=m.entities, kind=m.kind, is_roundup=m.is_roundup
            )
            for m in members
        ]
    )
    by_id = {m.article_id: m for m in members}
    seed = by_id.get(focus.anchor_article)
    if seed is None:
        seed = next((m for m in members if not m.is_roundup), None)
    if seed is None or not seed.kind.strip():
        return ""
    return focus_kind_of(seed.kind)


def profile_of(members: Sequence[MemberArticle]) -> ItemProfile:
    return ItemProfile(article_count=len(members), seed_kind=seed_kind(members))


def _is_hub(p: ItemProfile) -> bool:
    return p.article_count >= HUB_SPLIT_MIN_ARTICLES and p.seed_kind == HUB_SEED_KIND


def _reason_against_hub(other: ItemProfile, rescued: bool) -> str:
    if not other.seed_kind:
        return ""
    if other.seed_kind != HUB_SEED_KIND:
        return REASON_KIND_PREFIX + other.seed_kind
    return REASON_RESCUED if rescued else ""


def hub_split_reason(a: ItemProfile, b: ItemProfile, *, rescued: bool) -> str:
    """組を止める理由 (止めないなら "")。両側がハブなら両方向を見る。"""
    for hub, other in ((a, b), (b, a)):
        if not _is_hub(hub):
            continue
        reason = _reason_against_hub(other, rescued)
        if reason:
            return reason
    return ""


HubSplitCheck = Callable[[str, str, bool], str]


def build_hub_split_check(
    members_of: Callable[[str], Sequence[MemberArticle]],
) -> HubSplitCheck:
    """``plan_merges(hub_split=...)`` に渡す照合。事象の要約は 1 回の計画の中で使い回す。"""
    cache: dict[str, ItemProfile] = {}

    def profile(item_id: str) -> ItemProfile:
        if item_id not in cache:
            cache[item_id] = profile_of(members_of(item_id))
        return cache[item_id]

    def check(item_a: str, item_b: str, rescued: bool) -> str:
        try:
            return hub_split_reason(profile(item_a), profile(item_b), rescued=rescued)
        except Exception as e:  # noqa: BLE001 — 照合の失敗で統合を止めない (焦点の照合と同じ)
            _log.warning("eventnews_hub_split_failed", a=item_a, b=item_b, error=str(e)[:160])
            return ""

    return check
