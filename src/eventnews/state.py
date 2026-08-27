"""事象ニュースの独立媒体数算出 + 状態機械 (docs/event_news_design.md §7 §8)。

すべて決定論。LLM は呼ばない。呼出順は runner (別モジュール) が持つ:
1. 参加判定 (grouping モジュール、本ファイルの対象外) で既存アイテムに新メンバーを迎える
2. ``compute_source_breakdown`` を参加前後の 2 回呼び、breakdown_before/after を得る
3. ``decide_arrival`` へ渡して 'updated' / 'reinforced' を決定する
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from src.cti.source_basis import SourceTier, classify_source_tier
from src.eventnews.models import (
    UPDATE_DRIVER_TYPES,
    MemberArticle,
    SourceBreakdown,
    StateDecision,
)

# best_tier の序列 (§8: official > research > news > social > state_media)。
# source_basis._TIER_RANK (confidence 算出用。social/state_media は同格) とは目的が違う —
# あちらは「信頼度の高さ」、こちらは「事象ニュースの表示上どの tier を代表として掲げるか」
# であり news が social/state_media より上位に来る。独立に持つ。
_BEST_TIER_RANK: dict[SourceTier, int] = {
    "official": 5,
    "research": 4,
    "news": 3,
    "social": 2,
    "state_media": 1,
    "unknown": 0,
}

# best_tier 上昇 (§7 条件③) の判定に使う「上位」ティア: news/social/state_media → research/official
_HIGH_AUTHORITY_TIERS: frozenset[SourceTier] = frozenset({"official", "research"})

# importance の序列 (article importance。src/cti/ci_posture.py の規約 high=2/medium=1/low=0 を踏襲)
_IMPORTANCE_RANK: dict[str, int] = {"high": 2, "medium": 1, "low": 0}

# 駆動 entity 種別 → reason 文字列 (§7)
_ENTITY_REASON: dict[str, str] = {
    "cve": "new_cve",
    "victim_org": "new_victim_org",
    "actor": "new_actor",
}


def _source_key(member: MemberArticle) -> str:
    """媒体同一性キー (feed_url → feed_title → host)。

    出典: ``src/cti/source_basis.py`` の skey 規約 (``compute_source_basis`` /
    ``_corroborating_sources`` が使う ``rec.feed_url or rec.feed_title or ...``) を踏襲する。
    source_basis はレコード単位の便宜で article_id を最終フォールバックに使うが、
    ここでは「媒体そのもの」の同一性が関心のため host をフォールバックにする (§8)。
    新しい正規化関数は発明しない。
    """
    return member.feed_url or member.feed_title or member.host


def compute_source_breakdown(members: Sequence[MemberArticle]) -> SourceBreakdown:
    """独立媒体数の 3 値算出 (§8)。

    独立媒体は ``_source_key`` (feed_url→feed_title→host) の distinct 数。同一媒体の
    複数記事は最高位 tier を代表として保持する (source_basis と同じ規約)。
    state_media / unclassified はその内訳:
      - state_media_count = tier == 'state_media' の媒体数
      - unclassified       = tier が 'news' か 'unknown' の媒体数 (catch-all)
    best_tier は official > research > news > social > state_media の序で最良のもの。
    未分類を 0 と見せない (NOT NULL DEFAULT 0 で型として保証、§8) — メンバーが無ければ
    independent=0 / state_media=0 / unclassified=0 / best_tier='unknown' を返す。
    """
    tier_by_key: dict[str, SourceTier] = {}
    for m in members:
        key = _source_key(m)
        if not key:
            continue
        tier = classify_source_tier(m.feed_title, m.feed_url, account_class=m.account_class)
        if key not in tier_by_key or _BEST_TIER_RANK[tier] > _BEST_TIER_RANK[tier_by_key[key]]:
            tier_by_key[key] = tier

    tiers = tier_by_key.values()
    state_media_count = sum(1 for t in tiers if t == "state_media")
    unclassified = sum(1 for t in tiers if t in ("news", "unknown"))
    best_tier: SourceTier = max(tiers, key=lambda t: _BEST_TIER_RANK[t]) if tiers else "unknown"
    return SourceBreakdown(
        independent=len(tier_by_key),
        state_media=state_media_count,
        unclassified=unclassified,
        best_tier=best_tier,
    )


def _driver_entities(member: MemberArticle) -> dict[str, frozenset[str]]:
    """member.entities から UPDATE_DRIVER_TYPES (cve/victim_org/actor) のみ型別に抽出する。

    JOIN_ENTITY_TYPES には含まれるが駆動 entity には含めない malware_family、および
    そもそも entities に入らない ioc_*/tool/ttp (§7 — LLM 揺れで常時 true になるため除外
    済) は、ここで自然に取り除かれる。
    """
    out: dict[str, set[str]] = {}
    for etype, value in member.entities:
        if etype not in UPDATE_DRIVER_TYPES:
            continue
        out.setdefault(etype, set()).add(value)
    return {k: frozenset(v) for k, v in out.items()}


def _change_kind(
    existing_driver_entities: Mapping[str, frozenset[str]], new_actors: frozenset[str]
) -> str:
    """既存 actor 集合が非空で新 actor 値が非交差なら 'correct'、それ以外は 'add'。

    CVE 置換等の数値矛盾検出は v1 では採用しない (§7 — 偽の訂正を生むため)。
    """
    existing_actors = existing_driver_entities.get("actor", frozenset())
    if existing_actors and new_actors and existing_actors.isdisjoint(new_actors):
        return "correct"
    return "add"


def decide_arrival(
    existing_driver_entities: Mapping[str, frozenset[str]],
    new_member: MemberArticle,
    breakdown_before: SourceBreakdown,
    breakdown_after: SourceBreakdown,
    importance_before: str,
) -> StateDecision:
    """新メンバー到着時の状態判定 (§7)。すべて決定論。

    4 条件のいずれかで 'updated'、いずれも無ければ 'reinforced':
      ① 駆動 entity (cve/victim_org/actor) の新規値
      ② 初の裏取り (independent 1 → 2。単独報の解消 = I&W 上の最重要遷移。
         2 → 3 以降の同 tier 追加は reinforced — 実測で全 join の 99% が
         「別媒体からの 2 件目以降」であり、無条件の増加駆動は updated を
         恒真にする [リプレイ実測 reinforced 1%、レビュー B D3 の予言どおり])
      ③ best_tier の上昇 (news/social/state_media → research/official)
      ④ importance の上昇

    ``existing_driver_entities`` は新メンバー参加前のアイテムが持つ駆動 entity
    (型別、正規化済み値の集合)。``breakdown_before``/``breakdown_after`` は
    ``compute_source_breakdown`` を新メンバー参加前/後のメンバー集合それぞれに適用した結果
    (呼出側が算出して渡す — この関数はメンバー集合の差分計算をしない)。
    """
    new_driver = _driver_entities(new_member)
    added: dict[str, frozenset[str]] = {}
    for etype, values in new_driver.items():
        existing = existing_driver_entities.get(etype, frozenset())
        new_values = values - existing
        if new_values:
            added[etype] = new_values

    media_increase = breakdown_before.independent == 1 and breakdown_after.independent >= 2
    tier_rise = (
        breakdown_before.best_tier not in _HIGH_AUTHORITY_TIERS
        and breakdown_after.best_tier in _HIGH_AUTHORITY_TIERS
    )
    importance_rise = _IMPORTANCE_RANK.get(new_member.importance, 0) > _IMPORTANCE_RANK.get(
        importance_before, 0
    )

    reasons: list[str] = [_ENTITY_REASON[t] for t in ("cve", "victim_org", "actor") if t in added]
    if media_increase:
        reasons.append("first_corroboration")
    if tier_rise:
        reasons.append("tier_rise")
    if importance_rise:
        reasons.append("importance_rise")

    new_facts: dict[str, object] = {"article_id": new_member.article_id}
    if added:
        new_facts["added_entities"] = {t: sorted(v) for t, v in added.items()}
    if media_increase:
        new_facts["media_delta"] = breakdown_after.independent - breakdown_before.independent
    if tier_rise:
        new_facts["tier_transition"] = f"{breakdown_before.best_tier}->{breakdown_after.best_tier}"
    if importance_rise:
        # 理由には数えていたが記録していなかったため、後から「何が変わったのか」を
        # 説明できなかった (2026-08-27 — 続報の経緯表示で判明)
        new_facts["importance_transition"] = f"{importance_before}->{new_member.importance}"

    if not reasons:
        return StateDecision(kind="reinforced", change_kind=None, reasons=(), new_facts=new_facts)

    change_kind = _change_kind(existing_driver_entities, new_driver.get("actor", frozenset()))
    return StateDecision(
        kind="updated",
        change_kind=change_kind,
        reasons=tuple(reasons),
        new_facts=new_facts,
    )
