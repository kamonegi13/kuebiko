"""台帳の上流の修正 3 旗 (LEDGER_SEED_STRICT / DUP_RESCUE / ASSIGN_STRONG_ANCHOR) のテスト。"""

from __future__ import annotations

import pytest

from src.assessment.assign_strong_anchor import should_drop_assignment, strong_anchor_blocks
from src.assessment.assignment import ArticleKeys, SituationKeys
from src.assessment.dup_rescue import RescueCandidate, decide_dup_rescue
from src.assessment.ledger_flags import (
    assign_strong_anchor_mode,
    dup_rescue_mode,
    seed_strict_mode,
)
from src.assessment.name_relevance import article_names, is_relevant, matched_names, normalize_name
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
    def test_needs_two_names_and_shared_root(self) -> None:
        cands = [RescueCandidate("s1", "アララ社 不正アクセス cve20261", frozenset({"r1"}))]
        names = {"アララ社", "cve20261"}
        assert decide_dup_rescue(names, {"r1"}, cands) is not None
        assert decide_dup_rescue(names, {"r2"}, cands) is None  # 根が違う
        assert decide_dup_rescue({"アララ社"}, {"r1"}, cands) is None  # 名前 1 つ
        assert decide_dup_rescue({"zzz999", "yyy888"}, {"r1"}, cands) is None  # 題に出ない

    def test_best_candidate_and_tiebreak(self) -> None:
        names = {"aaa", "bbb", "ccc"}
        cands = [
            RescueCandidate("s2", "aaa bbb", frozenset({"r"})),
            RescueCandidate("s3", "aaa bbb ccc", frozenset({"r"})),
            RescueCandidate("s1", "aaa bbb", frozenset({"r"})),
        ]
        v = decide_dup_rescue(names, {"r"}, cands)
        assert v is not None and v.situation_id == "s3"
        v2 = decide_dup_rescue(names, {"r"}, [cands[0], cands[2]])
        assert v2 is not None and v2.situation_id == "s1"

    def test_try_rescue_off_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typing import Any, cast

        from src.assessment.dup_rescue import try_rescue

        monkeypatch.setenv("LEDGER_DUP_RESCUE", "0")
        assert (
            try_rescue(
                store=cast(Any, None),
                sit_keys=[],
                entities_by_id={},
                titles_by_id={},
                article_ids=["a"],
                claim="",
            )
            is None
        )


class TestNameRelevance:
    def test_kanji_org_and_product_match(self) -> None:
        names = article_names("日本学術振興会で不正アクセス", {"victim_org:日本学術振興会"})
        assert is_relevant(names, "日本学術振興会 のサーバに侵入")

    def test_corp_suffix_and_width_normalized(self) -> None:
        assert normalize_name("Acme Corp. （株）") == normalize_name("ACME")
        names = article_names("", {"victim_org:Acme Inc."})
        assert is_relevant(names, "ＡＣＭＥ で漏えい")

    def test_generic_and_short_names_ignored(self) -> None:
        names = article_names("不正アクセス 情報 漏洩", {"victim_org:ab", "actor:Japan"})
        assert names == frozenset()

    def test_matched_names_drops_substrings(self) -> None:
        assert matched_names({"apt41", "apt"}, "apt41 campaign") == ("apt41",)


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

    def test_relevant_by_name_not_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEDGER_ASSIGN_STRONG_ANCHOR", "1")
        art = ArticleKeys(
            article_id="a1",
            title="WordPress プラグインの脆弱性",
            strong=frozenset(),
            nations=frozenset(),
            tokens=frozenset(),
        )
        row = _sit("cyber_incident").row
        sit = SituationKeys(
            row=SituationRow(**{**row.__dict__, "title": "WordPress の侵害"}),
            strong=frozenset(),
            nations=frozenset(),
            tokens=frozenset(),
        )
        assert not strong_anchor_blocks(
            (sit, "token"), art, entity_keys=["affected_product:WordPress"]
        )
        assert strong_anchor_blocks((_sit("cyber_incident"), "token"), art)


def test_seed_relevance_before_roundup_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_SEED_STRICT", "1")
    pool: list[dict[str, object]] = [
        {"article_id": "s", "title": "アララ で不正アクセス"},
        {"article_id": "m", "title": "週刊ダイジェスト アララ 大阪市"},
    ]
    ents = {
        "s": {"victim_org:アララ"},
        "m": {"victim_org:アララ", "victim_org:大阪市", "victim_org:新潟市"},
    }
    out = expand_claim_sources(
        claim_text="アララで不正アクセス",
        seed_ids=("s",),
        pool=pool,
        entities_by_id=ents,
        max_sources=5,
    )
    assert "m" in out  # 固有名の一致を、まとめ・多数の被害組織の除外より先に見る
