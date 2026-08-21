"""較正格子 P3: シャドーパネルのテスト。

パネルは E1 正解付き事例のみを入力とし (per-article 呼び出し禁止 §5-6)、
シャドー専用テーブルに書くだけで本番は読まない (§10.1)。ここでは
係争/対照の仕分け・盲検裁定の合意分類・候補未到達スキップ・冪等性を固定する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.shadow_panel import (
    _classify_agreement,
    run_weekly_shadow_panel,
)

_NOW = datetime(2026, 8, 23, 4, 45, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "panel.db")


def _seed_labeled_article(
    repo: RunHistoryRepository,
    *,
    article_id: str,
    truth: str,
    production: str,
    body: str,
    arrived_at: datetime = _NOW,
) -> None:
    """E1 主体ラベル + 記事本体 (パネルの入力形) を seed する。"""
    iso = arrived_at.isoformat()
    with repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (iso,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " subject_actor_ids, body, category)"
            " VALUES (?,?,?,?, 'posted', ?, ?, ?, 'ransomware')",
            (
                rid,
                article_id,
                f"title-{article_id}",
                f"https://kuebiko.example/{article_id}",
                iso,
                production,
                body,
            ),
        )
    repo.record_tuning_label(
        dedup_key=f"feedmatch:{article_id}:{truth}",
        field="subject_actor",
        label_value=truth,
        source="E1",
        strength="strong",
        provenance="{}",
        article_id=article_id,
        arrived_at=arrived_at,
    )


class _FakeAlias:
    def __init__(self, actor_id: str) -> None:
        self.id = actor_id


class _FakeLLM:
    """model_ref ごとに固定の主体を返す fake (盲検 — 事例内容は見ない)。"""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.calls = 0


class TestClassifyAgreement:
    def test_patterns(self) -> None:
        assert _classify_agreement(["qilin", "qilin"], "qilin") == "unanimous_correct"
        assert _classify_agreement(["akira", "akira"], "qilin") == "unanimous_wrong"
        assert _classify_agreement(["qilin", "akira"], "qilin") == "split"
        assert _classify_agreement(["qilin", "(error)"], "qilin") == "error"


@pytest.fixture
def patched_pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """candidates ゲートと classify_judgment を fake に差し替える。

    candidates は truth を常に含む (到達可)。answers[model_label] を返す。
    """
    state: dict[str, Any] = {"answers": {}, "candidates": ["qilin", "akira"], "calls": []}

    class _Aliases:
        def find_all(self, text: str) -> list[Any]:
            return []

    monkeypatch.setattr("src.cti.actor_normalizer.load_actor_aliases", lambda: _Aliases())
    monkeypatch.setattr(
        "src.pipeline.persistence._relevant_actors",
        lambda found, category: [_FakeAlias(a) for a in state["candidates"]],
    )

    async def fake_classify(llm: Any, **kwargs: Any) -> Any:
        state["calls"].append(llm.answer)
        llm.calls += 1
        if llm.answer == "RAISE":
            raise RuntimeError("panel judge boom")

        class _Out:
            subject_actor_id = llm.answer

        return _Out()

    monkeypatch.setattr("src.cti.judgment_classifier.classify_judgment", fake_classify)
    return state


def _factory(answers: dict[str | None, str]) -> Any:
    def make(ref: str | None) -> _FakeLLM:
        return _FakeLLM(answers[ref])

    return make


class TestShadowPanelRun:
    @pytest.mark.asyncio
    async def test_dispute_and_control_are_judged_and_recorded(
        self,
        repo: RunHistoryRepository,
        patched_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TUNING_PANEL_SECOND_MODEL", "dummy:second")
        # 係争 (本番 akira ≠ 正解 qilin) + 一致対照 (本番 qilin = 正解)
        _seed_labeled_article(
            repo, article_id="a1", truth="qilin", production="akira", body="x" * 600
        )
        _seed_labeled_article(
            repo, article_id="a2", truth="qilin", production="qilin", body="y" * 600
        )

        result = await run_weekly_shadow_panel(
            repo=repo,
            llm_factory=_factory({None: "qilin", "dummy:second": "akira"}),
            now=_NOW,
        )
        assert result.cases == 2
        assert result.disputes == 1
        assert result.controls == 1
        assert result.splits == 2  # main=qilin vs second=akira で両件とも分裂

        totals = repo.summarize_panel_verdicts()
        assert totals["judged"] == 2
        assert totals["split_rate"] == 1.0
        # verdicts の中身 (モデル別の値) が残る
        with repo._connect() as conn:  # noqa: SLF001
            row = conn.execute(
                "SELECT verdicts, is_dispute FROM panel_verdicts WHERE article_id='a1'"
            ).fetchone()
        assert row["is_dispute"] == 1
        assert {v["model"] for v in json.loads(row["verdicts"])} == {"main", "dummy:second"}

    @pytest.mark.asyncio
    async def test_unanimous_correct_and_idempotency(
        self,
        repo: RunHistoryRepository,
        patched_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TUNING_PANEL_SECOND_MODEL", "dummy:second")
        _seed_labeled_article(repo, article_id="a1", truth="qilin", production="", body="x" * 600)

        factory = _factory({None: "qilin", "dummy:second": "qilin"})
        first = await run_weekly_shadow_panel(repo=repo, llm_factory=factory, now=_NOW)
        assert first.cases == 1 and first.splits == 0
        totals = repo.summarize_panel_verdicts()
        assert totals["by_agreement"]["unanimous_correct"]["n"] == 1

        # 再実行は case_key で skip (裁定は 1 事例 1 回)
        second = await run_weekly_shadow_panel(repo=repo, llm_factory=factory, now=_NOW)
        assert second.cases == 0
        assert second.skipped_existing == 1

    @pytest.mark.asyncio
    async def test_unreachable_truth_is_skipped_not_judged(
        self,
        repo: RunHistoryRepository,
        patched_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TUNING_PANEL_SECOND_MODEL", "dummy:second")
        patched_pipeline["candidates"] = ["akira"]  # 正解 qilin が候補に無い
        _seed_labeled_article(
            repo, article_id="a1", truth="qilin", production="akira", body="x" * 600
        )

        result = await run_weekly_shadow_panel(
            repo=repo, llm_factory=_factory({None: "akira", "dummy:second": "akira"}), now=_NOW
        )
        assert result.cases == 0
        assert result.unreachable == 1
        assert repo.summarize_panel_verdicts()["judged"] == 0

    @pytest.mark.asyncio
    async def test_error_verdict_is_not_persisted_and_retries(
        self,
        repo: RunHistoryRepository,
        patched_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """情報ゼロの error 裁定は記録しない — case_key を塞ぐと修正後に再裁定できない。"""
        monkeypatch.setenv("TUNING_PANEL_SECOND_MODEL", "dummy:second")
        _seed_labeled_article(
            repo, article_id="a1", truth="qilin", production="akira", body="x" * 600
        )
        broken = await run_weekly_shadow_panel(
            repo=repo, llm_factory=_factory({None: "qilin", "dummy:second": "RAISE"}), now=_NOW
        )
        assert broken.cases == 0
        assert broken.errors  # 可視化はされる
        assert repo.summarize_panel_verdicts()["judged"] == 0

        # モデル側の問題を直したら同じ事例が再裁定できる
        fixed = await run_weekly_shadow_panel(
            repo=repo, llm_factory=_factory({None: "qilin", "dummy:second": "qilin"}), now=_NOW
        )
        assert fixed.cases == 1
        assert repo.summarize_panel_verdicts()["judged"] == 1

    @pytest.mark.asyncio
    async def test_force_think_wrapper_targets_reasoning_models(self) -> None:
        from src.tuning.shadow_panel import _ForceThink

        captured: dict[str, Any] = {}

        class _Inner:
            answer = "x"

            async def generate_structured(self, *a: Any, **k: Any) -> str:
                captured.update(k)
                return "ok"

        wrapped = _ForceThink(_Inner())
        assert await wrapped.generate_structured("p", think=False) == "ok"
        assert captured["think"] is True  # think=False 指定でも強制される
        assert wrapped.answer == "x"  # その他の属性は素通し

    @pytest.mark.asyncio
    async def test_old_labels_are_out_of_window(
        self,
        repo: RunHistoryRepository,
        patched_pipeline: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("TUNING_PANEL_SECOND_MODEL", "dummy:second")
        _seed_labeled_article(
            repo,
            article_id="a1",
            truth="qilin",
            production="akira",
            body="x" * 600,
            arrived_at=_NOW - timedelta(days=30),
        )
        result = await run_weekly_shadow_panel(
            repo=repo, llm_factory=_factory({None: "qilin", "dummy:second": "qilin"}), now=_NOW
        )
        assert result.cases == 0


class TestTaxonomyTierAgreement:
    def test_rates_by_tier(self, repo: RunHistoryRepository) -> None:
        from src.storage.records import TaxonomyProposalRecord

        def _insert(tier: str, status: str) -> None:
            pid = repo.insert_taxonomy_proposal(
                TaxonomyProposalRecord(
                    proposal_type="pattern_4",
                    tier=tier,
                    target_yaml="victim_sectors",
                    target_canonical="x",
                    proposed_change="{}",
                    rationale="r",
                    confidence="high",
                )
            )
            repo.update_taxonomy_proposal_status(pid, status=status)

        _insert("tier_1_auto", "accepted")
        _insert("tier_1_auto", "accepted")
        _insert("tier_2_review", "accepted")
        _insert("tier_2_review", "rejected")
        rows = {r["tier"]: r for r in repo.taxonomy_tier_agreement()}
        assert rows["tier_1_auto"]["agreement_rate"] == 1.0
        assert rows["tier_2_review"]["agreement_rate"] == 0.5
