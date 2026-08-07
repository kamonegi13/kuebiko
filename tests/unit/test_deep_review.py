"""夜間 deep-review (台帳 ACH の think 再評価) の unit テスト。

LLM 呼出 (ground/incremental/adversarial) は deep_review モジュール名前空間で
monkeypatch し、選定・prior の取り方 (二重加算防止)・revision 書込を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.assessment import deep_review
from src.assessment.deep_review import run_deep_review
from src.assessment.situation_store import RevisionRow, SituationStore
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.synthesis.grounded.estimate import HypothesisScore
from src.synthesis.grounded.passes import ClaimAnalysis

_NOW = datetime(2026, 7, 24, 18, 0, tzinfo=UTC)


def _analysis(leading: str = "organized_state_op") -> ClaimAnalysis:
    return ClaimAnalysis(
        evidence=(),
        hypotheses=(HypothesisScore(leading, 3, 0, "leading"),),
        leading_hypothesis=leading,
        llm_confidence="moderate",
        key_assumptions=("a",),
        missing_evidence=("m",),
        indicators=("i",),
        claim_type="ongoing_activity",
        implication="imp",
    )


def _rev(sid: str, *, leading: str, created_at: str, note: str = "") -> RevisionRow:
    return RevisionRow(
        situation_id=sid,
        rev=0,  # add_revision が採番
        claim="露アクターの活動",
        claim_type="ongoing_activity",
        leading_hypothesis=leading,
        confidence="moderate",
        confidence_basis="ACH=moderate",
        hypotheses_json=(
            '[{"hypothesis": "organized_state_op", "consistent": 2, "inconsistent": 0}]'
        ),
        assumptions_json="[]",
        missing_json="[]",
        indicators_json="[]",
        implication="",
        delta_type="no_change",
        delta_note=note,
        created_at=created_at,
    )


def _seed_situation(
    tmp_path: Path, *, with_today_evidence: bool = True
) -> tuple[RunHistoryRepository, SituationStore, str]:
    repo = RunHistoryRepository(db_path=tmp_path / "dr.db")
    store = SituationStore(db_path=tmp_path / "dr.db")
    run_id = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=True))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="a1",
            title="露アクター活動の続報",
            url="https://x.example/a1",
            category="apt",
            status="posted",
            importance="high",
            summary="s",
            published_at=_NOW,
        )
    )
    repo.update_article_body("a1", "本文: 露アクターの活動詳細")
    row = store.open_situation(
        title="露アクターの活動",
        domain="cyber_incident",
        anchors=frozenset(),
        pir_ids=(),
        now_iso=(_NOW - timedelta(days=3)).isoformat(),
    )
    sid = row.situation_id
    # prior (当日窓の外) と、当日の OFF 判定 revision
    store.add_revision(
        _rev(sid, leading="organized_state_op", created_at=(_NOW - timedelta(days=2)).isoformat())
    )
    store.add_revision(
        _rev(sid, leading="organized_state_op", created_at=(_NOW - timedelta(hours=3)).isoformat())
    )
    if with_today_evidence:
        store.record_assignment(
            situation_id=sid,
            article_id="a1",
            added_at=(_NOW - timedelta(hours=4)).isoformat(),
            assigned_by="token",
        )
    return repo, store, sid


@pytest.mark.asyncio
async def test_deep_review_refines_and_records_revision(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo, store, sid = _seed_situation(tmp_path)
    captured: dict[str, Any] = {}

    async def _fake_inc(**kw: Any) -> Any:
        captured["prior"] = kw["prior"]
        # think 再評価で leading が変わるケース (flip 計上を検証)
        return SimpleNamespace(analysis=_analysis("reporting_artifact"), claim="露アクターの活動")

    async def _fake_adv(**kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(deep_review, "incremental_ground_and_score", _fake_inc)
    monkeypatch.setattr(deep_review, "adversarial_review", _fake_adv)

    stats = await run_deep_review(
        llm=SimpleNamespace(model="claudecode:sonnet"),  # type: ignore[arg-type]
        repo=repo,
        store=store,
        now=_NOW,
        db_path=tmp_path / "dr.db",
    )

    assert stats["reviewed"] == 1
    assert stats["flips"] == 1  # organized_state_op → reporting_artifact
    latest = store.latest_revision(sid)
    assert latest is not None
    assert latest.leading_hypothesis == "reporting_artifact"
    assert latest.delta_note.startswith("夜間精査")
    assert "夜間精査" in latest.confidence_basis


@pytest.mark.asyncio
async def test_deep_review_prior_is_pre_window_revision(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """二重加算防止: prior は当日窓より前の revision (当日 OFF 判定ではない)。"""
    repo = RunHistoryRepository(db_path=tmp_path / "dr.db")
    store = SituationStore(db_path=tmp_path / "dr.db")
    run_id = repo.start_run(RunRecord(started_at=_NOW, pipeline="t", dry_run=True))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id="a1",
            title="t",
            url="https://x.example/a1",
            category="apt",
            status="posted",
            importance="high",
            summary="s",
            published_at=_NOW,
        )
    )
    repo.update_article_body("a1", "本文")
    row = store.open_situation(
        title="露アクターの活動",
        domain="cyber_incident",
        anchors=frozenset(),
        pir_ids=(),
        now_iso=(_NOW - timedelta(days=3)).isoformat(),
    )
    sid = row.situation_id
    # prior (窓外) は leading=criminal_financial / 当日 OFF 判定は organized_state_op —
    # どちらが prior として渡るかを leading で判別する
    store.add_revision(
        _rev(sid, leading="criminal_financial", created_at=(_NOW - timedelta(days=2)).isoformat())
    )
    store.add_revision(
        _rev(sid, leading="organized_state_op", created_at=(_NOW - timedelta(hours=3)).isoformat())
    )
    store.record_assignment(
        situation_id=sid,
        article_id="a1",
        added_at=(_NOW - timedelta(hours=4)).isoformat(),
        assigned_by="token",
    )
    captured: dict[str, Any] = {}

    async def _fake_inc(**kw: Any) -> Any:
        captured["prior"] = kw["prior"]
        return SimpleNamespace(analysis=_analysis(), claim="露アクターの活動")

    async def _fake_adv(**kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(deep_review, "incremental_ground_and_score", _fake_inc)
    monkeypatch.setattr(deep_review, "adversarial_review", _fake_adv)

    await run_deep_review(
        llm=SimpleNamespace(model="claudecode:sonnet"),  # type: ignore[arg-type]
        repo=repo,
        store=store,
        now=_NOW,
        db_path=tmp_path / "dr.db",
    )

    # 当日窓の外 (2 日前・criminal_financial) が prior — 当日 OFF 判定ではない
    assert captured["prior"].leading_hypothesis == "criminal_financial"


@pytest.mark.asyncio
async def test_deep_review_records_read_and_assessment(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """接地 prompt に供給した記事は read_at、ACH 引用は assessed_at を刻む。

    監査 2026-08-01: 最大能力 (cap 20) の夜間パスが評価状態を書かず、未評価
    backlog 55% 滞留 + 翌朝の同一記事再読というスループット純損失になっていた。
    mark_read のセマンティクス (「接地 prompt に本文を供給した記事を刻む」) 上、
    deep-review も供給者なので刻むのが正。
    """
    from src.synthesis.grounded.estimate import EvidenceItem

    repo, store, sid = _seed_situation(tmp_path)

    async def _fake_inc(**kw: Any) -> Any:
        analysis = ClaimAnalysis(
            evidence=(
                EvidenceItem(
                    article_id="a1",
                    source_tier="vendor",
                    attribution_basis="vendor_confirmed",
                    excerpt="抜粋",
                    polarity="supports",
                ),
            ),
            hypotheses=(HypothesisScore("organized_state_op", 3, 0, "leading"),),
            leading_hypothesis="organized_state_op",
            llm_confidence="moderate",
            key_assumptions=(),
            missing_evidence=(),
            indicators=("指標X",),
            claim_type="ongoing_activity",
            implication="",
        )
        return SimpleNamespace(
            analysis=analysis, claim="露アクターの活動", fired_indicators=("指標X",)
        )

    async def _fake_adv(**kw: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(deep_review, "incremental_ground_and_score", _fake_inc)
    monkeypatch.setattr(deep_review, "adversarial_review", _fake_adv)

    # 発火対象の open forecast (deep-review 由来の hit 採点を検証)
    store.open_forecast(
        situation_id=sid,
        indicator="指標X",
        opened_at=(_NOW - timedelta(days=2)).isoformat(),
        horizon_days=30,
    )

    stats = await run_deep_review(
        llm=SimpleNamespace(model="claudecode:sonnet"),  # type: ignore[arg-type]
        repo=repo,
        store=store,
        now=_NOW,
        db_path=tmp_path / "dr.db",
    )

    assert stats["reviewed"] == 1
    with repo._connect() as conn:  # noqa: SLF001
        row = conn.execute(
            "SELECT read_at, assessed_at FROM situation_evidence"
            " WHERE situation_id=? AND article_id='a1'",
            (sid,),
        ).fetchone()
    assert row["read_at"] is not None, "接地 prompt 供給記事に read_at が刻まれていない"
    assert row["assessed_at"] is not None, "ACH 引用証拠に assessed_at が刻まれていない"
    # fired_indicators → forecast hit 同期 (夜間パスの hit 取りこぼし防止)。
    # 注: 最新 revision が同指標を持つ場合は採点後に再 open される (昼と同じ仕様)
    # ため「open が消える」ではなく「hit 行が刻まれた」ことを検証する。
    with repo._connect() as conn:  # noqa: SLF001
        hit = conn.execute(
            "SELECT COUNT(*) AS n FROM situation_forecasts"
            " WHERE situation_id=? AND indicator='指標X' AND status='hit'",
            (sid,),
        ).fetchone()
    assert int(hit["n"]) == 1, (
        "発火済み指標が hit 採点されていない (deep-review が update_forecasts を呼んでいない)"
    )


@pytest.mark.asyncio
async def test_deep_review_skips_without_today_evidence(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """当日証拠なし (sweep 等の判断のみの revision) は再接地の材料がないため skip。"""
    repo, store, _sid = _seed_situation(tmp_path, with_today_evidence=False)

    async def _boom(**kw: Any) -> Any:
        raise AssertionError("LLM が呼ばれてはいけない")

    monkeypatch.setattr(deep_review, "incremental_ground_and_score", _boom)
    monkeypatch.setattr(deep_review, "ground_and_score", _boom)

    stats = await run_deep_review(
        llm=SimpleNamespace(model="claudecode:sonnet"),  # type: ignore[arg-type]
        repo=repo,
        store=store,
        now=_NOW,
        db_path=tmp_path / "dr.db",
    )

    assert stats["reviewed"] == 0
    assert stats["skipped_no_sources"] == 1
