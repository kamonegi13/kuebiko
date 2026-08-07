"""承認時再帰属 (D3 三パス: title 全期間 / LLM 出力全期間 / body 窓) のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository
from src.ui.services.actor_reattribution import reattribute_actor

_ZERO_STATS = {
    "title_candidates": 0,
    "llm_candidates": 0,
    "body_candidates": 0,
    "subjects_added": 0,
    "mentions_added": 0,
    "months_redistilled": 0,
}


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "reattr.db")


@pytest.fixture(autouse=True)
def _no_kev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.ui.services.actor_history_distill._kev_cve_set", lambda: frozenset())


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(id="nightspire", canonical="NightSpire", aliases=("Night Spire",)),
            ActorAlias(id="qilin", canonical="Qilin"),
        ),
    )


def _add(
    repo: RunHistoryRepository,
    aid: str,
    *,
    title: str,
    body: str | None = None,
    created_at: datetime | None = None,
    category: str = "incident",
    subject: str | None = None,
    subject_source: str | None = None,
    llm_raw: str | None = None,
    llm_conf: str | None = None,
) -> None:
    created = created_at or datetime.now(UTC) - timedelta(days=3)
    rid = repo.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=title,
            url=f"https://x.example/{aid}",
            status="posted",
            category=category,
            subject_actor_ids=subject,
            subject_actor_source=subject_source,
            llm_primary_actor_raw=llm_raw,
            llm_primary_confidence=llm_conf,
            created_at=created,
        ),
    )
    if body is not None:
        repo.update_article_body(aid, body)


# ---------- パス 1: title 全期間 ----------


def test_title_match_adds_subject_and_redistills(repo: RunHistoryRepository) -> None:
    _add(
        repo,
        "a1",
        title="NightSpire claims new healthcare victim",
        body="The NightSpire ransomware group listed a hospital.",
    )
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["subjects_added"] == 1
    assert stats["mentions_added"] == 1  # body 窓パスが言及 entity も付ける
    assert stats["months_redistilled"] == 1
    rows = repo.list_actor_month_profiles(["nightspire"])
    assert len(rows) == 1 and rows[0].subject_articles == 1
    assert ("actor", "nightspire") in repo.get_entities_by_article("a1")


def test_title_pass_reaches_beyond_body_window(repo: RunHistoryRepository) -> None:
    """title は永久メタ — body 窓 (90日) を超えた過去記事にも主題が付く (D3 の本体)。"""
    old = datetime.now(UTC) - timedelta(days=200)
    _add(repo, "a1", title="NightSpire hits manufacturing plant", created_at=old)
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["subjects_added"] == 1
    assert stats["mentions_added"] == 0  # body なし → 言及 entity は付かない
    months = [p.month for p in repo.list_actor_month_profiles(["nightspire"])]
    assert len(months) == 1  # 200 日前の月が再蒸留されている


def test_body_only_mention_does_not_become_subject(repo: RunHistoryRepository) -> None:
    _add(
        repo,
        "a1",
        title="Hospital hit by ransomware attack",
        body="Researchers attribute the incident to NightSpire.",
    )
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["mentions_added"] == 1
    assert stats["subjects_added"] == 0  # title 層は不成立、LLM 生入力もなし
    assert repo.list_actor_month_profiles(["nightspire"]) == []


def test_existing_subject_is_preserved_and_appended(repo: RunHistoryRepository) -> None:
    _add(
        repo,
        "a1",
        title="Qilin and NightSpire double extortion wave",
        body="Qilin and NightSpire both listed victims.",
        subject="qilin",
        subject_source="llm",
    )
    reattribute_actor(repo, "nightspire", registry=_registry())
    rows = repo.search_recent_articles_by_names(
        ["NightSpire"], datetime.now(UTC) - timedelta(days=30)
    )
    row = next(r for r in rows if r["article_id"] == "a1")
    assert row["subject_actor_ids"] == "qilin,nightspire"  # 既存判定を保持して追加
    assert row["subject_actor_source"] == "llm"


# ---------- パス 2: LLM 出力全期間 (D1 で保存した生入力) ----------


def test_llm_pass_purged_body_exact_match_adds_subject(repo: RunHistoryRepository) -> None:
    """本文 purge 済みでも、保存済み LLM 生出力が名前と完全一致すれば主題が付く。"""
    old = datetime.now(UTC) - timedelta(days=200)
    _add(
        repo,
        "a1",
        title="Hospital breach disclosed",  # title に名前なし
        created_at=old,
        llm_raw="nightspire",
        llm_conf="high",
    )
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["llm_candidates"] == 1
    assert stats["subjects_added"] == 1
    rows = repo.search_titles_by_names(["Hospital breach"])
    assert rows[0]["subject_actor_source"] == "llm"


def test_llm_pass_slug_form_resolves(repo: RunHistoryRepository) -> None:
    """slug 形 ('night-spire') も空白変換で名前完全一致として扱う。"""
    old = datetime.now(UTC) - timedelta(days=200)
    _add(repo, "a1", title="Plant hit", created_at=old, llm_raw="night-spire", llm_conf="medium")
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["subjects_added"] == 1


def test_llm_pass_fuzzy_without_body_not_added(repo: RunHistoryRepository) -> None:
    """本文がない場合、fuzzy 解決 (完全一致でない) では付与しない (保守則)。"""
    old = datetime.now(UTC) - timedelta(days=200)
    _add(repo, "a1", title="Plant hit", created_at=old, llm_raw="nightspire crew", llm_conf="high")
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["llm_candidates"] == 1
    assert stats["subjects_added"] == 0


def test_llm_pass_low_confidence_skipped(repo: RunHistoryRepository) -> None:
    old = datetime.now(UTC) - timedelta(days=200)
    _add(repo, "a1", title="Plant hit", created_at=old, llm_raw="nightspire", llm_conf="low")
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["subjects_added"] == 0


def test_llm_pass_with_body_requires_mention(repo: RunHistoryRepository) -> None:
    """本文が現存する場合は word-boundary 言及検証を通す (二重ゲートの再現)。"""
    _add(
        repo,
        "a1",
        title="Breach disclosed",
        body="An unnamed group encrypted the plant.",  # 本文に名前なし
        llm_raw="nightspire",
        llm_conf="high",
    )
    stats = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats["subjects_added"] == 0
    _add(
        repo,
        "a2",
        title="Second breach disclosed",
        body="NightSpire encrypted the second plant.",
        llm_raw="nightspire",
        llm_conf="high",
    )
    stats2 = reattribute_actor(repo, "nightspire", registry=_registry())
    assert stats2["subjects_added"] == 1


# ---------- 共通 ----------


def test_no_match_returns_zero(repo: RunHistoryRepository) -> None:
    _add(repo, "a1", title="Unrelated news", body="Nothing about actors here.")
    assert reattribute_actor(repo, "nightspire", registry=_registry()) == _ZERO_STATS


def test_merged_stub_is_noop(repo: RunHistoryRepository) -> None:
    reg = ActorAliasRegistry(
        actors=(
            ActorAlias(id="a", canonical="A"),
            ActorAlias(id="b", canonical="B", status="merged", merged_into="a"),
        ),
    )
    assert reattribute_actor(repo, "b", registry=reg) == _ZERO_STATS
