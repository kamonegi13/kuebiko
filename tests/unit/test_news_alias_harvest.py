"""ニュース由来 alias 収穫 (F3) のテスト — 保守的抽出と提案 dedup が核心。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.news_alias_harvest import extract_alias_candidates, propose_news_aliases
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="volt_typhoon",
                canonical="Volt Typhoon",
                aliases=("Vanguard Panda",),
            ),
            ActorAlias(id="qilin", canonical="Qilin", aliases=("Agenda",)),
        ),
    )


# ---------- extract_alias_candidates ----------


def test_extracts_aka_in_parentheses() -> None:
    text = "The group Volt Typhoon (aka Redfly) targeted power grids."
    out = extract_alias_candidates(text, _registry())
    assert [(c.actor_id, c.alias) for c in out] == [("volt_typhoon", "Redfly")]
    assert "Redfly" in out[0].excerpt


def test_extracts_also_known_as_with_list(caplog: pytest.LogCaptureFixture) -> None:
    text = "Volt Typhoon, also known as Redfly, Bronze Storm and UNC3236, is active."
    out = extract_alias_candidates(text, _registry())
    aliases = {c.alias for c in out}
    assert aliases == {"Redfly", "Bronze Storm", "UNC3236"}


def test_extracts_japanese_betsumei() -> None:
    text = "中国系 Volt Typhoon(別名: Redfly)が重要インフラを標的化。"
    out = extract_alias_candidates(text, _registry())
    assert [(c.actor_id, c.alias) for c in out] == [("volt_typhoon", "Redfly")]


def test_known_names_are_not_harvested() -> None:
    # 既知 alias (Vanguard Panda) と他アクターの名前 (Agenda) は収穫しない
    text = "Volt Typhoon (aka Vanguard Panda, Agenda, Redfly) was observed."
    out = extract_alias_candidates(text, _registry())
    assert [c.alias for c in out] == ["Redfly"]


def test_generic_words_are_filtered() -> None:
    # 一般語 SSoT (元素名 Zinc 等) は 2026-07-21 型の汚染源 — 収穫段階で遮断
    text = "Volt Typhoon (also known as Zinc) resurfaced."
    out = extract_alias_candidates(text, _registry())
    assert out == []


def test_no_preceding_actor_no_harvest() -> None:
    # 直前 80 字に辞書アクターがいなければ「誰の別名か」不明 → 収穫しない
    text = "A new threat group, also known as Redfly, emerged."
    assert extract_alias_candidates(text, _registry()) == []


def test_merged_stub_not_attributed() -> None:
    reg = ActorAliasRegistry(
        actors=(
            ActorAlias(id="a", canonical="Alpha"),
            ActorAlias(id="b", canonical="OldBeta", status="merged", merged_into="a"),
        ),
    )
    text = "OldBeta (aka Redfly) is active."
    assert extract_alias_candidates(text, reg) == []


def test_lowercase_or_short_candidates_rejected() -> None:
    text = "Volt Typhoon (aka the group, ab) was seen."
    assert extract_alias_candidates(text, _registry()) == []


# ---------- propose_news_aliases ----------


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "na.db")


def _add(repo: RunHistoryRepository, aid: str, *, title: str, body: str) -> None:
    created = datetime.now(UTC) - timedelta(days=2)
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=title,
            url=f"https://x.example/{aid}",
            status="posted",
            created_at=created,
        ),
    )
    repo.update_article_body(aid, body)


def test_propose_creates_pending_proposal(repo: RunHistoryRepository) -> None:
    _add(
        repo,
        "a1",
        title="Volt Typhoon expands targeting",
        body="Volt Typhoon (also known as Redfly) compromised utilities.",
    )
    stats = propose_news_aliases(repo, registry=_registry())
    assert stats["proposed"] == 1
    props = repo.list_actor_update_proposals(status="pending")
    na = [p for p in props if p.proposal_type == "news_alias"]
    assert len(na) == 1
    payload = json.loads(na[0].payload)
    assert payload["actor_id"] == "volt_typhoon"
    assert payload["alias"] == "Redfly"
    assert payload["_evidence"]["article_count"] == 1


def test_propose_dedups_across_runs(repo: RunHistoryRepository) -> None:
    _add(
        repo,
        "a1",
        title="t",
        body="Volt Typhoon (aka Redfly) attacked again.",
    )
    assert propose_news_aliases(repo, registry=_registry())["proposed"] == 1
    stats2 = propose_news_aliases(repo, registry=_registry())
    assert stats2["proposed"] == 0
    assert stats2["skipped_dup"] == 1


def test_propose_no_marker_articles(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", title="t", body="Volt Typhoon attacked without any alias note.")
    stats = propose_news_aliases(repo, registry=_registry())
    assert stats["proposed"] == 0


def test_intervening_unknown_subject_blocks_attribution() -> None:
    """実データ再現: 辞書アクターとマーカーの間に未知の大文字主体 → 帰属しない。

    「MOIS-linked OilRig subgroup Lyceum (aka Hexane)」の Hexane は辞書未収録の
    Lyceum の別名であり、遠くの既知アクターに付けてはならない。
    """
    reg = ActorAliasRegistry(
        actors=(ActorAlias(id="oilrig", canonical="OilRig"),),
    )
    text = "attributed to the OilRig subgroup Lyceum (also known as Hexane) today."
    assert extract_alias_candidates(text, reg) == []
    # 直結なら従来どおり収穫できる
    direct = extract_alias_candidates("OilRig (also known as Hexane) is active.", reg)
    assert [c.alias for c in direct] == ["Hexane"]


def test_malware_list_does_not_attribute_to_distant_actor() -> None:
    """実データ再現: マルウェア列挙内の aka を、列挙中の alias 一致アクターに誤帰属しない。"""
    reg = ActorAliasRegistry(
        actors=(ActorAlias(id="turla", canonical="Turla", aliases=("Snake",)),),
    )
    text = "- Snake Keylogger - ValleyRAT (also known as Winos4.0) - XWorm"
    assert extract_alias_candidates(text, reg) == []
