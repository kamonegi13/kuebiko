"""台帳の上流の修正 3 旗 (LEDGER_SEED_STRICT / DUP_RESCUE / ASSIGN_STRONG_ANCHOR) のテスト。"""

from __future__ import annotations

import pytest

from src.assessment.assign_strong_anchor import should_drop_assignment, strong_anchor_blocks
from src.assessment.assignment import ArticleKeys, SituationKeys
from src.assessment.dup_rescue import RescueCandidate, decide_dup_rescue, rescue_anchors
from src.assessment.ledger_flags import (
    assign_strong_anchor_mode,
    dup_rescue_mode,
    seed_strict_mode,
)
from src.assessment.situation_store import SituationRow
from src.cti.roundup_detector import is_roundup_article
from src.synthesis.grounded.clustering import (
    expand_claim_sources,
    is_proper_token,
    strict_token_match,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "off"), ("0", "off"), ("shadow", "shadow"), ("1", "on"), ("bogus", "off")],
)
def test_flag_modes(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str) -> None:
    for env, fn in (
        ("LEDGER_SEED_STRICT", seed_strict_mode),
        ("LEDGER_DUP_RESCUE", dup_rescue_mode),
        ("LEDGER_ASSIGN_STRONG_ANCHOR", assign_strong_anchor_mode),
    ):
        if raw is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, raw)
        assert fn() == expected


class TestRoundupSsot:
    def test_title_kind_category(self) -> None:
        assert is_roundup_article("週刊セキュリティニュース")
        assert is_roundup_article("普通の題", kind="roundup")
        assert is_roundup_article("普通の題", category="recap")
        assert not is_roundup_article("A社で不正アクセス", kind="incident", category="breach")


class TestStrictTokens:
    def test_generic_only_overlap_rejected(self) -> None:
        assert not strict_token_match({"不正アクセス", "情報"})

    def test_proper_token_accepted(self) -> None:
        assert strict_token_match({"不正アクセス", "アララ"})
        assert is_proper_token("アララ")
        assert not is_proper_token("ab")

    def test_two_specific_accepted(self) -> None:
        assert strict_token_match({"物流", "倉庫"})

    def test_single_overlap_rejected(self) -> None:
        assert not strict_token_match({"アララ"})


def _expand(mode: str | None, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    if mode is None:
        monkeypatch.delenv("LEDGER_SEED_STRICT", raising=False)
    else:
        monkeypatch.setenv("LEDGER_SEED_STRICT", mode)
    pool: list[dict[str, object]] = [
        {"article_id": "s", "title": "甲社で不正アクセス 情報漏洩の可能性"},
        {"article_id": "x", "title": "乙社で不正アクセス 情報漏洩を公表"},
        {"article_id": "r", "title": "週刊ダイジェスト 甲社で不正アクセス 情報漏洩"},
    ]
    return expand_claim_sources(
        claim_text="甲社で不正アクセス 情報漏洩",
        seed_ids=("s",),
        pool=pool,
        entities_by_id={},
        max_sources=8,
    )


def test_seed_strict_flag_equivalence(monkeypatch: pytest.MonkeyPatch) -> None:
    off = _expand(None, monkeypatch)
    assert off == _expand("0", monkeypatch)
    assert off == _expand("shadow", monkeypatch)  # shadow は結果不変
    assert "x" in off
    strict = _expand("1", monkeypatch)
    assert strict == ["s"]


def test_seed_strict_excludes_many_victim_orgs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_SEED_STRICT", "1")
    pool: list[dict[str, object]] = [
        {"article_id": "s", "title": "a"},
        {"article_id": "m", "title": "b"},
    ]
    ents = {"s": {"cve:CVE-1"}, "m": {"cve:CVE-1", "victim_org:a", "victim_org:b", "victim_org:c"}}
    out = expand_claim_sources(
        claim_text="x", seed_ids=("s",), pool=pool, entities_by_id=ents, max_sources=5
    )
    assert out == ["s"]


class TestDupRescue:
    def test_needs_two_anchors_and_shared_root(self) -> None:
        cands = [RescueCandidate("s1", frozenset({"cve:C1", "victim_org:a"}), frozenset({"r1"}))]
        anchors = {"cve:C1", "victim_org:a"}
        assert decide_dup_rescue(anchors, {"r1"}, cands) is not None
        assert decide_dup_rescue(anchors, {"r2"}, cands) is None  # 根が違う
        assert decide_dup_rescue({"cve:C1"}, {"r1"}, cands) is None  # 手がかり 1 つ
        assert decide_dup_rescue({"actor:x", "malware_family:y"}, {"r1"}, cands) is None

    def test_best_candidate_and_tiebreak(self) -> None:
        a = frozenset({"cve:C1", "victim_org:a", "victim_org:b"})
        cands = [
            RescueCandidate("s2", frozenset({"cve:C1", "victim_org:a"}), frozenset({"r"})),
            RescueCandidate("s3", a, frozenset({"r"})),
            RescueCandidate("s1", frozenset({"cve:C1", "victim_org:a"}), frozenset({"r"})),
        ]
        v = decide_dup_rescue(a, {"r"}, cands)
        assert v is not None and v.situation_id == "s3"
        v2 = decide_dup_rescue({"cve:C1", "victim_org:a"}, {"r"}, cands[:1] + cands[2:])
        assert v2 is not None and v2.situation_id == "s1"

    def test_rescue_anchors_filters_types(self) -> None:
        assert rescue_anchors(["cve:C", "actor:x", "victim_org:o"]) == {"cve:C", "victim_org:o"}

    def test_try_rescue_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typing import Any, cast

        from src.assessment.dup_rescue import try_rescue

        monkeypatch.setenv("LEDGER_DUP_RESCUE", "0")
        assert (
            try_rescue(
                store=cast(Any, None), sit_keys=[], entities_by_id={}, article_ids=["a"], claim=""
            )
            is None
        )


def _sit(domain: str, strong: frozenset[str] = frozenset()) -> SituationKeys:
    row = SituationRow(
        situation_id="s1",
        title="t",
        domain=domain,
        status="active",
        anchors=strong,
        pir_ids=(),
        opened_at="2026-11-01",
        last_evidence_at="2026-11-01",
    )
    return SituationKeys(
        row=row, strong=strong, nations=frozenset(), tokens=frozenset(), claim_type=""
    )


def _art(strong: frozenset[str] = frozenset()) -> ArticleKeys:
    return ArticleKeys(
        article_id="a1",
        title="t",
        strong=strong,
        nations=frozenset(),
        tokens=frozenset(),
    )


class TestStrongAnchor:
    def test_decision(self) -> None:
        assert should_drop_assignment(rule="nation", domain="cyber_incident", shared_strong=())
        assert should_drop_assignment(rule="token", domain="cyber_incident", shared_strong=())
        assert not should_drop_assignment(rule="anchor", domain="cyber_incident", shared_strong=())
        assert not should_drop_assignment(rule="nation", domain="geopolitical", shared_strong=())
        assert not should_drop_assignment(rule="nation", domain="military", shared_strong=())
        assert not should_drop_assignment(
            rule="token", domain="cyber_incident", shared_strong=("cve:C",)
        )

    def test_flag_modes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        matched = (_sit("cyber_incident"), "nation")
        monkeypatch.setenv("LEDGER_ASSIGN_STRONG_ANCHOR", "0")
        assert not strong_anchor_blocks(matched, _art())
        monkeypatch.setenv("LEDGER_ASSIGN_STRONG_ANCHOR", "shadow")
        assert not strong_anchor_blocks(matched, _art())
        monkeypatch.setenv("LEDGER_ASSIGN_STRONG_ANCHOR", "1")
        assert strong_anchor_blocks(matched, _art())
        assert not strong_anchor_blocks((_sit("geopolitical"), "nation"), _art())
        assert not strong_anchor_blocks(None, _art())
