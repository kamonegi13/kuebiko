"""重複として分析に残した記事 (status='posted' + duplicate_of、2026-10-02) を数えない消費者。

記事を読む分析 (台帳の証拠・総括) には残すが、件数・順位に効く消費者が同じ出来事を
二重に数えないことを固定する。事象単位で数える仕組みが入るまでの境界。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    r = RunHistoryRepository(db_path=tmp_path / "kept.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid, dup in (("orig", None), ("dup", "orig")):
        r.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title="t",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                posted_channel=None if dup else "watch",
                importance="high",
                category="incident",
                dedup_key="same-key",
                duplicate_of=dup,
                created_at=datetime.now(UTC),
            )
        )
    return r


def test_pir_evaluation_rows_exclude_kept_duplicates(repo: RunHistoryRepository) -> None:
    from src.pir.evaluator import _load_posted_rows

    rows, _actors = _load_posted_rows(repo=repo)

    assert [r["article_id"] for r in rows] == ["orig"]


def test_followup_prior_count_ignores_kept_duplicates(repo: RunHistoryRepository) -> None:
    count, latest = repo.count_prior_posts_by_dedup_key("same-key", lookback_hours=24)

    assert count == 1
    assert latest is not None and latest.article_id == "orig"


def test_analysis_only_rows_feed_pir_evaluation_but_not_delivery(tmp_path: Path) -> None:
    """分析専用の行は PIR 評価の根拠には載るが、日次の要点・前報の数え方には出ない。"""
    from src.digest.pir_daily_focus import _filter_meaningful_matches
    from src.pir.evaluator import PirMatch, _load_posted_rows, _to_pir_match

    # Arrange
    r = RunHistoryRepository(db_path=tmp_path / "analysis_only.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    for aid, marker in (("ana", "geo_rescue"), ("norm", None)):
        r.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title="t",
                url=f"https://kuebiko.example/{aid}",
                status="posted",
                posted_channel=None if marker else "watch",
                importance="medium",
                category="geopolitical",
                dedup_key="geo-key",
                analysis_only=marker,
                created_at=datetime.now(UTC),
            )
        )

    # Act
    rows, _actors = _load_posted_rows(repo=r)
    matches = [_to_pir_match(row, ("keywords",)) for row in rows]
    count, latest = r.count_prior_posts_by_dedup_key("geo-key", lookback_hours=24)

    # Assert
    assert {row["article_id"] for row in rows} == {"ana", "norm"}  # 評価の根拠には載る
    by_id: dict[str, PirMatch] = {m.article_id: m for m in matches}
    assert by_id["ana"].analysis_only is True
    assert [m.article_id for m in _filter_meaningful_matches(matches)] == ["norm"]  # 配信側は除外
    assert count == 1 and latest is not None and latest.article_id == "norm"


def _add(r: RunHistoryRepository, rid: int, aid: str, *, marker: str | None, key: str) -> None:
    r.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title="t",
            url=f"https://kuebiko.example/{aid}",
            status="posted",
            posted_channel=None if marker else "watch",
            importance="medium",
            category="incident",
            dedup_key=key,
            analysis_only=marker,
            created_at=datetime.now(UTC),
        )
    )


def test_dedup_lookups_ignore_analysis_only_rows(tmp_path: Path) -> None:
    """配信していない行が「既出」の錨になり、配信すべき記事を抑えない (dedup_key と CVE)。"""
    # Arrange
    r = RunHistoryRepository(db_path=tmp_path / "anchor.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    _add(r, rid, "ana", marker="geo_rescue", key="cve-2026-1111")

    # Act / Assert
    assert r.find_recent_article_by_dedup_key("cve-2026-1111", within_hours=24) is None
    assert r.find_recent_post_by_cve("cve-2026-1111", within_hours=24) is None
    _add(r, rid, "norm", marker=None, key="cve-2026-1111")
    found = r.find_recent_article_by_dedup_key("cve-2026-1111", within_hours=24)
    assert found is not None and found.article_id == "norm"
    cve = r.find_recent_post_by_cve("cve-2026-1111", within_hours=24)
    assert cve is not None and cve.article_id == "norm"


def test_victim_org_lookup_ignores_analysis_only_rows(tmp_path: Path) -> None:
    # Arrange
    r = RunHistoryRepository(db_path=tmp_path / "org.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    _add(r, rid, "ana", marker="geo_rescue", key="k")
    with r._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO article_entities (article_id, entity_type, value, created_at)"
            " VALUES ('ana', 'victim_org', 'Acme', ?)",
            (datetime.now(UTC).isoformat(),),
        )

    # Act
    hit = r.find_recent_posted_article_by_victim_org("acme", within_hours=24)

    # Assert
    assert hit is None


def test_semantic_dedup_anchor_excludes_analysis_only_rows(tmp_path: Path) -> None:
    # Arrange
    r = RunHistoryRepository(db_path=tmp_path / "emb.db")
    rid = r.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    _add(r, rid, "ana", marker="geo_rescue", key="k1")
    _add(r, rid, "norm", marker=None, key="k2")
    vec = [1.0, 0.0, 0.0]
    for aid in ("ana", "norm"):
        r.mark_url_seen(url_hash=f"h-{aid}", url=f"https://kuebiko.example/{aid}", article_id=aid)
        r.add_article_embedding(
            url_hash=f"h-{aid}", url=f"https://kuebiko.example/{aid}", vector=vec, model="m"
        )

    # Act
    hit = r.find_similar_embedding(vec, model="m", threshold=0.9)

    # Assert: 配信した行だけが錨になる
    assert hit is not None and hit[0] == "h-norm"
