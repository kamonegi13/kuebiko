"""事象単位ニュースの群化規則 (src/eventnews/grouping.py) のテスト。

2 信号要求 (cos だけ・entity だけでは結合しない) / entity 乗り換え連鎖の
不変条件による停止 / MEMBER_CAP / 複数マッチの tiebreak / dormant 厳条件 /
頻出ガード、を固定する (docs/event_news_design.md §5)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from src.assessment.evidence_verify import normalize_for_match
from src.eventnews.grouping import assign_article, build_join_entities
from src.eventnews.models import ENTITY_FREQ_CAP, MEMBER_CAP, ItemState, MemberArticle

_NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _member(
    article_id: str,
    entities: frozenset[tuple[str, str]],
    *,
    anchor_ts: datetime = _NOW,
) -> MemberArticle:
    return MemberArticle(
        article_id=article_id,
        title=f"title-{article_id}",
        url=f"https://example.com/{article_id}",
        feed_title="feed",
        feed_url="https://example.com/feed",
        host="example.com",
        importance="medium",
        category="threat",
        status="posted",
        anchor_ts=anchor_ts,
        summary="summary",
        body="body",
        entities=entities,
    )


def _item(
    item_id: str,
    *,
    first_reported_at: datetime = _NOW,
    last_reported_at: datetime = _NOW,
    status: str = "new",
) -> ItemState:
    return ItemState(
        item_id=item_id,
        first_reported_at=first_reported_at,
        last_reported_at=last_reported_at,
        status=status,
        importance="medium",
        current_version=1,
        member_ids=(),
    )


def _vec(cos: float) -> np.ndarray:
    """[1.0, 0.0] との内積が ``cos`` になる単位ベクトル。"""
    return np.array([cos, (1.0 - cos**2) ** 0.5], dtype=np.float32)


_BASE_VEC = np.array([1.0, 0.0], dtype=np.float32)


# ---------- 2 信号要求 ----------


def test_cos_alone_without_shared_entity_does_not_join() -> None:
    member = _member("m1", frozenset({("cve", "cve-2024-1111")}))
    item = _item("item-1")
    candidate = _member("cand", frozenset({("malware_family", "cobaltstrike")}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (member,)},
        member_vecs={"m1": _vec(0.95)},
        now=_NOW,
    )

    assert assignment.target_item_id is None


def test_shared_entity_alone_without_cos_does_not_join() -> None:
    shared = ("cve", "cve-2024-1111")
    member = _member("m1", frozenset({shared}))
    item = _item("item-1")
    candidate = _member("cand", frozenset({shared}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (member,)},
        member_vecs={"m1": _vec(0.50)},
        now=_NOW,
    )

    assert assignment.target_item_id is None


def test_cos_and_shared_entity_together_join() -> None:
    shared = ("cve", "cve-2024-1111")
    member = _member("m1", frozenset({shared}))
    item = _item("item-1")
    candidate = _member("cand", frozenset({shared}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (member,)},
        member_vecs={"m1": _vec(0.95)},
        now=_NOW,
    )

    assert assignment.target_item_id == "item-1"
    assert assignment.max_cos == pytest.approx(0.95, abs=1e-4)
    assert shared in assignment.shared_entities


# ---------- entity 乗り換え連鎖 (不変条件) ----------


def test_entity_chain_drift_blocked_by_invariant() -> None:
    common = ("malware_family", "shared-mal")
    # ⚠ side を actor にしない — 2026-08-31 以降「アクター名だけの共有」は辺の
    #    段階で落ちるので、invariant まで到達せずこのテストの主題が確かめられない
    #    (アクター名単独の遮断は test_eventnews_shared_names_relaxation.py が持つ)。
    side = ("malware_family", "side-mal")
    a = _member("a", frozenset({common}))
    b = _member("b", frozenset({common}))
    c = _member("c", frozenset({common, side}))
    item = _item("item-1")
    # d は c とだけ (side 経由で) つながり、a/b とは共通 entity が無い。
    d = _member("d", frozenset({side}))

    assignment = assign_article(
        d,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (a, b, c)},
        member_vecs={"a": _vec(0.95), "b": _vec(0.95), "c": _vec(0.95)},
        now=_NOW,
    )

    assert assignment.target_item_id is None
    assert "invariant" in assignment.rejected


def test_entity_chain_without_drift_joins() -> None:
    common = ("malware_family", "shared-mal")
    a = _member("a", frozenset({common}))
    b = _member("b", frozenset({common}))
    item = _item("item-1")
    d = _member("d", frozenset({common}))

    assignment = assign_article(
        d,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (a, b)},
        member_vecs={"a": _vec(0.95), "b": _vec(0.95)},
        now=_NOW,
    )

    assert assignment.target_item_id == "item-1"


# ---------- MEMBER_CAP ----------


def test_member_cap_rejects_join() -> None:
    common = ("cve", "cve-2024-9999")
    members = tuple(_member(f"m{i}", frozenset({common})) for i in range(MEMBER_CAP))
    item = _item("item-1")
    candidate = _member("newcomer", frozenset({common}))
    member_vecs = {m.article_id: _vec(0.95) for m in members}

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": members},
        member_vecs=member_vecs,
        now=_NOW,
    )

    assert assignment.target_item_id is None
    assert "member_cap" in assignment.rejected


def test_member_cap_not_reached_still_joins() -> None:
    common = ("cve", "cve-2024-9999")
    members = tuple(_member(f"m{i}", frozenset({common})) for i in range(MEMBER_CAP - 1))
    item = _item("item-1")
    candidate = _member("newcomer", frozenset({common}))
    member_vecs = {m.article_id: _vec(0.95) for m in members}

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": members},
        member_vecs=member_vecs,
        now=_NOW,
    )

    assert assignment.target_item_id == "item-1"


# ---------- 複数マッチの tiebreak ----------


def test_tiebreak_prefers_older_first_reported_at_on_equal_cos() -> None:
    common = ("cve", "cve-2024-1000")
    m1 = _member("m1", frozenset({common}))
    m2 = _member("m2", frozenset({common}))
    older = _item("older", first_reported_at=_NOW - timedelta(hours=10))
    newer = _item("newer", first_reported_at=_NOW - timedelta(hours=1))
    candidate = _member("cand", frozenset({common}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(older, newer),
        item_members={"older": (m1,), "newer": (m2,)},
        member_vecs={"m1": _vec(0.9), "m2": _vec(0.9)},
        now=_NOW,
    )

    assert assignment.target_item_id == "older"


def test_highest_cos_wins_over_tiebreak_order() -> None:
    common = ("cve", "cve-2024-1000")
    m1 = _member("m1", frozenset({common}))
    m2 = _member("m2", frozenset({common}))
    older_low_cos = _item("older", first_reported_at=_NOW - timedelta(hours=10))
    newer_high_cos = _item("newer", first_reported_at=_NOW - timedelta(hours=1))
    candidate = _member("cand", frozenset({common}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(older_low_cos, newer_high_cos),
        item_members={"older": (m1,), "newer": (m2,)},
        member_vecs={"m1": _vec(0.75), "m2": _vec(0.95)},
        now=_NOW,
    )

    assert assignment.target_item_id == "newer"


# ---------- dormant 厳条件 ----------


def test_dormant_item_rejects_normal_threshold_match() -> None:
    last_reported = _NOW - timedelta(days=20)
    common = ("cve", "cve-x")
    extra = ("actor", "actor-x")
    member = _member("m1", frozenset({common, extra}), anchor_ts=last_reported)
    item = _item(
        "item-1",
        first_reported_at=last_reported,
        last_reported_at=last_reported,
        status="dormant",
    )
    # 共有 entity は 1 つだけ (DORMANT_REJOIN_SHARED=2 に届かない)
    candidate = _member("cand", frozenset({common}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (member,)},
        member_vecs={"m1": _vec(0.75)},
        now=_NOW,
    )

    assert assignment.target_item_id is None
    assert "dormant_strict" in assignment.rejected


def test_dormant_item_accepts_strict_match() -> None:
    last_reported = _NOW - timedelta(days=20)
    common = ("cve", "cve-x")
    extra = ("actor", "actor-x")
    member = _member("m1", frozenset({common, extra}), anchor_ts=last_reported)
    item = _item(
        "item-1",
        first_reported_at=last_reported,
        last_reported_at=last_reported,
        status="dormant",
    )
    candidate = _member("cand", frozenset({common, extra}))

    assignment = assign_article(
        candidate,
        _BASE_VEC,
        items=(item,),
        item_members={"item-1": (member,)},
        member_vecs={"m1": _vec(0.85)},
        now=_NOW,
    )

    assert assignment.target_item_id == "item-1"


# ---------- 頻出ガード (build_join_entities) ----------


def test_build_join_entities_excludes_over_cap_values() -> None:
    """自由記述・再利用される名前 (victim_org / actor / malware_family) は cap で落とす。"""
    common = normalize_for_match("Common Corp.")
    rare = normalize_for_match("Rare Corp.")
    common_key = ("victim_org", common)
    rare_key = ("victim_org", rare)
    raw = [(f"a{i}", "victim_org", "Common Corp.") for i in range(ENTITY_FREQ_CAP + 1)]
    raw.append(("a0", "victim_org", "Rare Corp."))
    counts = {common_key: ENTITY_FREQ_CAP + 1, rare_key: 1}

    result = build_join_entities(raw, counts)

    assert common_key not in result["a0"]
    assert rare_key in result["a0"]


def test_build_join_entities_keeps_frequent_cve() -> None:
    """CVE は頻出でも結合信号に残す (大域一意な識別子なので頻度で薄まらない)。

    2026-08-25: cap を CVE にも掛けていたため、**大きく報じられた事案ほど群化に
    失敗する**逆転が起きていた。実測で 16 個の CVE が cap を超え、162 事象
    (うち 140 が単独事象) に散っていた。CVE-2026-68820 は 26 事象に分裂。
    共起だけで繋がるわけではなく cos >= COS_THRESHOLD が別途要る。
    """
    key = ("cve", "cve-2026-73570")
    raw = [(f"a{i}", "cve", "cve-2026-73570") for i in range(ENTITY_FREQ_CAP + 5)]
    counts = {key: ENTITY_FREQ_CAP + 5}

    result = build_join_entities(raw, counts)

    assert key in result["a0"]


def test_build_join_entities_normalizes_victim_org() -> None:
    raw = [("a1", "victim_org", "Example Corp.")]
    normalized = normalize_for_match("Example Corp.")
    counts = {("victim_org", normalized): 1}

    result = build_join_entities(raw, counts)

    assert result["a1"] == frozenset({("victim_org", normalized)})


def test_build_join_entities_excludes_non_join_types() -> None:
    # ⚠ tool は 2026-08-31 に結合信号へ昇格した。除外の例には使えない
    raw = [("a1", "actor_provisional", "maybe-actor"), ("a1", "ttp", "T1059")]
    counts: dict[tuple[str, str], int] = {}

    result = build_join_entities(raw, counts)

    assert result.get("a1", frozenset()) == frozenset()


# ---------- CVE を主題として共有するペアの閾値 (required_cos) ----------


def _cves(*ids: str) -> frozenset[tuple[str, str]]:
    return frozenset(("cve", i) for i in ids)


def test_focal_cve_pair_uses_the_relaxed_threshold() -> None:
    """同じ CVE を主題にする記事どうしは cos を緩める。

    日本語の短い注意喚起と英語記事では埋込が離れる (実測 CVE-2026-73570 のペアで
    cos=0.512)。頻出ガードを直して CVE が結合信号に戻っても、AND のもう一方
    (cos>=0.70) が通らず分裂が残っていた。
    """
    from src.eventnews.grouping import required_cos
    from src.eventnews.models import FOCAL_CVE_COS

    a = _cves("cve-2026-73570")
    b = _cves("cve-2026-73570", "cve-2026-1")
    assert required_cos(a, b, sorted(a & b)) == FOCAL_CVE_COS


def test_bulk_advisory_keeps_the_strict_threshold() -> None:
    """一括アドバイザリ側は緩めない。

    ANSSI の 1 記事は最大 278 個の CVE を列挙する。列挙を主題扱いすると、
    たまたま同じ CVE に触れただけの無関係な事案が接着する。
    """
    from src.eventnews.grouping import required_cos
    from src.eventnews.models import COS_THRESHOLD, FOCAL_CVE_MAX

    focal = _cves("cve-2026-73570")
    bulk = _cves(*[f"cve-2026-{i}" for i in range(FOCAL_CVE_MAX + 2)], "cve-2026-73570")
    assert required_cos(focal, bulk, sorted(focal & bulk)) == COS_THRESHOLD
    assert required_cos(bulk, focal, sorted(focal & bulk)) == COS_THRESHOLD


def test_non_cve_sharing_keeps_the_strict_threshold() -> None:
    """緩めるのは CVE だけ。actor/malware/victim_org は再利用される名前なので厳しいまま。"""
    from src.eventnews.grouping import required_cos
    from src.eventnews.models import COS_THRESHOLD

    a = frozenset({("actor", "apt28")})
    assert required_cos(a, a, sorted(a)) == COS_THRESHOLD


def test_relaxed_threshold_still_requires_a_shared_entity() -> None:
    """cos を緩めても「共有 entity >= 1」は外さない (2 信号のうち片方は必ず要る)。"""
    from src.eventnews.grouping import required_cos
    from src.eventnews.models import COS_THRESHOLD

    assert required_cos(_cves("cve-a"), _cves("cve-b"), []) == COS_THRESHOLD
