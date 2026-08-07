"""actor 行動史 API (/api/v1/actors/{id}/history, /situations) のテスト。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.assessment.situation_store import SituationRow
from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.actor_observed_history import ActorMonthProfile
from src.storage.run_history import RunHistoryRepository
from src.ui.api import actor_history as api_mod


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "hist.db")


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(id="qilin", canonical="Qilin", aliases=("Agenda",)),
            ActorAlias(
                id="old_qilin",
                canonical="Old Qilin",
                status="merged",
                merged_into="qilin",
            ),
        ),
    )


@pytest.fixture
def patched(repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch) -> RunHistoryRepository:
    monkeypatch.setattr(api_mod, "RunHistoryRepository", lambda: repo)
    monkeypatch.setattr(api_mod, "load_actor_aliases", _registry)
    return repo


def test_history_merges_redirected_rows(patched: RunHistoryRepository) -> None:
    patched.replace_actor_month_profiles(
        "2026-07",
        [
            ActorMonthProfile(
                actor_id="qilin",
                month="2026-07",
                subject_articles=3,
                distinct_sources=2,
                sectors={"healthcare": 1},
            ),
            ActorMonthProfile(
                actor_id="old_qilin",
                month="2026-07",
                subject_articles=2,
                distinct_sources=1,
                sectors={"energy": 2},
            ),
        ],
    )
    out = api_mod.actor_history("old_qilin")  # 旧 id で照会しても canonical に解決
    assert out["actor_id"] == "qilin"
    assert out["merged_from"] == ["old_qilin"]
    assert len(out["months"]) == 1
    m = out["months"][0]
    assert m["subject_articles"] == 5  # 表示時合算
    assert m["sectors"] == {"healthcare": 1, "energy": 2}
    # series は epoch (2026-04) から連続 (観測なし月は 0)
    assert out["series"][0]["month"] == "2026-04"
    assert out["series"][0]["subject_articles"] == 0
    july = next(p for p in out["series"] if p["month"] == "2026-07")
    assert july["subject_articles"] == 5
    assert out["note"]


def test_history_empty_actor_returns_zero_series(patched: RunHistoryRepository) -> None:
    out = api_mod.actor_history("qilin")
    assert out["months"] == []
    assert all(p["subject_articles"] == 0 for p in out["series"])


def test_situations_reverse_lookup(
    patched: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        SituationRow(
            situation_id="sit-1",
            title="Qilin 医療機関攻撃",
            domain="crime",
            status="active",
            anchors=frozenset({"actor:qilin", "sector:healthcare"}),
            pir_ids=(),
            opened_at="2026-07-01T00:00:00+00:00",
            last_evidence_at="2026-07-20T00:00:00+00:00",
        ),
        SituationRow(
            situation_id="sit-2",
            title="無関係な情勢",
            domain="geo",
            status="active",
            anchors=frozenset({"actor:apt29"}),
            pir_ids=(),
            opened_at="2026-07-01T00:00:00+00:00",
            last_evidence_at="2026-07-21T00:00:00+00:00",
        ),
        SituationRow(
            situation_id="sit-3",
            title="旧 id anchor の情勢",
            domain="crime",
            status="dormant",
            anchors=frozenset({"actor:old_qilin"}),
            pir_ids=(),
            opened_at="2026-06-01T00:00:00+00:00",
            last_evidence_at="2026-06-20T00:00:00+00:00",
        ),
    ]

    class FakeStore:
        def load_situations(self, statuses: Any) -> list[SituationRow]:
            return rows

    monkeypatch.setattr("src.assessment.situation_store.SituationStore", lambda: FakeStore())
    out = api_mod.actor_situations("qilin")
    ids = [s["situation_id"] for s in out["situations"]]
    # canonical anchor と merge 旧 id anchor の両方が引ける (新しい証拠順)
    assert ids == ["sit-1", "sit-3"]
    assert out["total"] == 2


def test_month_articles_drilldown(
    patched: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: 月次行の証拠開示 — その月の主題記事のライブ照会 (merge 旧 id 込み)。"""
    from datetime import UTC, datetime

    from src.storage.records import ArticleRecord, RunRecord

    monkeypatch.setattr("src.tools.kev_client.get_kev_cve_set", lambda: frozenset({"CVE-2026-1"}))
    created = datetime(2026, 7, 10, tzinfo=UTC)
    rid = patched.start_run(RunRecord(started_at=created, pipeline="x", dry_run=False))
    for aid, subject, country in (
        ("a1", "qilin", "JP"),
        ("a2", "old_qilin", None),  # merge 旧 id の記事も引ける
        ("a3", "apt29", None),  # 他アクターは含まれない
    ):
        patched.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=aid,
                title=f"t-{aid}",
                url=f"https://x.example/{aid}",
                status="posted",
                subject_actor_ids=subject,
                victim_country_iso=country,
                created_at=created,
            ),
        )
    patched.add_article_entities("a1", [("cve", "CVE-2026-1")])
    out = api_mod.actor_month_articles("qilin", "2026-07")
    ids = {a["article_id"] for a in out["articles"]}
    assert ids == {"a1", "a2"}
    assert out["total"] == 2
    a1 = next(a for a in out["articles"] if a["article_id"] == "a1")
    assert a1["japan_targeted"] is True
    assert a1["kev_hit"] is True


def test_month_articles_rejects_bad_month(patched: RunHistoryRepository) -> None:
    import pytest as _pytest
    from fastapi import HTTPException

    with _pytest.raises(HTTPException):
        api_mod.actor_month_articles("qilin", "not-a-month")
