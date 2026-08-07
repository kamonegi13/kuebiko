"""Phase 2 victim_org 抽出の永続化テスト。

地図プロット用 victim_org (実際に攻撃された組織のみ) が article_entities に
entity_type='victim_org' で保存され、重複/空白が正規化されることを検証する。
"""

from __future__ import annotations

from pathlib import Path

from src.main import SummaryOutput, _persist_article_entities
from src.storage.run_history import RunHistoryRepository
from src.tools.discord_publisher import BriefingMessage


def _bm(victim_orgs: list[str]) -> BriefingMessage:
    return BriefingMessage(
        title="t",
        summary="s",
        importance="medium",
        category="breach",
        metadata={"victim_orgs": victim_orgs},
    )


def test_summary_output_has_victim_orgs_default_empty() -> None:
    s = SummaryOutput(title_ja="x", importance="medium", category="breach", summary="y")
    assert s.victim_orgs == []
    s2 = SummaryOutput(
        title_ja="x",
        importance="medium",
        category="breach",
        summary="y",
        victim_orgs=["B病院"],
    )
    assert s2.victim_orgs == ["B病院"]


def test_victim_org_persisted_as_entity(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "vorg.db")
    # 重複(大文字小文字)・空白・空文字を含む入力
    msg = _bm(["B病院", "b病院", "  X社  ", ""])
    _persist_article_entities(dedup_repo=repo, article_id="art1", msg=msg)
    out = repo.entities_for_articles(["art1"], entity_type="victim_org")
    # case-insensitive dedup + strip + 空除去 → 2 件
    assert set(out["art1"]) == {"B病院", "X社"}


def test_no_victim_orgs_persists_nothing(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "vorg2.db")
    _persist_article_entities(dedup_repo=repo, article_id="art2", msg=_bm([]))
    assert repo.entities_for_articles(["art2"], entity_type="victim_org") == {}


# ── #7: victim_city ──


def test_summary_output_victim_city_default_none() -> None:
    s = SummaryOutput(title_ja="x", importance="medium", category="breach", summary="y")
    assert s.victim_city is None
    s2 = SummaryOutput(
        title_ja="x",
        importance="medium",
        category="breach",
        summary="y",
        victim_city="Osaka",
    )
    assert s2.victim_city == "Osaka"


def test_victim_city_persisted_as_entity(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "vcity.db")
    msg = BriefingMessage(
        title="t",
        summary="s",
        importance="medium",
        category="breach",
        metadata={"victim_city": "  Osaka  "},
    )
    _persist_article_entities(dedup_repo=repo, article_id="artc", msg=msg)
    out = repo.entities_for_articles(["artc"], entity_type="victim_city")
    assert out["artc"] == ["Osaka"]


def test_no_victim_city_persists_nothing(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "vcity2.db")
    msg = BriefingMessage(
        title="t",
        summary="s",
        importance="medium",
        category="breach",
        metadata={},
    )
    _persist_article_entities(dedup_repo=repo, article_id="artd", msg=msg)
    assert repo.entities_for_articles(["artd"], entity_type="victim_city") == {}
