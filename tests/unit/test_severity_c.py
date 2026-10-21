# ruff: noqa: E501  (日本語の文は幅 2 で数えられ長く見える)
"""深刻さ C (9 問の logprob + RF) の shadow: 特徴量・モデル・外す記事・記録・集計・API・flag。

配信・重要度は変えない (記録のみ) ことと、問いを当てない記事に GPU を使わないことを固定する。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.cti import severity_c as sc
from src.cti.severity_c_summary import summarize
from src.eval.severity_gold_marks import ROUNDUP_GOLD_S3_IDS, gold_exclusion
from src.storage.repo_severity_c_shadow import SeverityCShadowRow
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.ui.services import severity_c_shadow_job as job
from tests.unit.test_ui_app import _bootstrap_project

_META = sc.ArticleMeta("breach", "breaking", "breach", True, 9.8, 1_000_000.0, 3)
_PROBE_MODEL = "kuebiko-sft:s23i"


def _tree(feature: int, threshold: float, low: float, high: float) -> dict[str, list[float]]:
    return {
        "left": [1, -1, -1],
        "right": [2, -1, -1],
        "feature": [feature, -2, -2],
        "threshold": [threshold, 0.0, 0.0],
        "proba": [0.5, low, high],
    }


def _write_model(
    path: Path, *, names: list[str] | None = None, probe_model: str = _PROBE_MODEL
) -> None:
    names = names or ["p.severe", "cat=breach", "kev"]
    raw = {
        "version": 1,
        "model_version": "severity_s3.test",
        "probe_model": probe_model,
        "feature_names": names,
        "threshold": 0.5,
        "trees": [_tree(0, 0.0, 0.1, 0.9)],  # logit(P(severe)) > 0 → 0.9
    }
    path.write_text(json.dumps(raw), encoding="utf-8")


def _model(tmp_path: Path) -> sc.SeverityCModel:
    path = tmp_path / "m.json"
    _write_model(path)
    loaded = sc.load_severity_c_model(str(path))
    assert loaded is not None
    return loaded


class TestQuestions:
    def test_nine_fixed_questions_in_order(self) -> None:
        assert sc.QUESTION_IDS == (
            "cyber", "new_event", "harm_confirmed", "exploited", "wide",
            "critical", "state", "large", "severe",
        )  # fmt: skip
        # 言い回しは比較で固定した (直すと RF の学習時の特徴とずれる)。変えるなら学習し直して指紋も更新する
        digest = hashlib.sha256(json.dumps(sc.QUESTIONS, ensure_ascii=False).encode()).hexdigest()
        assert digest[:16] == "b26b527c5a8f7b33"

    def test_prompt_puts_body_before_question(self) -> None:
        text = sc.build_question("見出し", "2026-10-01", "本文" * 10, "問い?")

        assert text.index("本文") < text.index("# 問い")
        assert "公開日: 2026-10-01" in text

    def test_body_is_truncated_and_missing_date_is_unknown(self) -> None:
        text = sc.build_question("t", None, "x" * (sc.BODY_MAX_CHARS + 500), "q")

        assert "公開日: 不明" in text
        assert text.count("x") == sc.BODY_MAX_CHARS


class TestFeatureVector:
    def test_probe_logit_onehot_and_numeric(self) -> None:
        names = ["p.severe", "p.cyber", "cat=breach", "cat=apt", "type=breaking", "kind=roundup",
                 "kev", "cvss", "cvss_ge9", "log_loss", "n_cves"]  # fmt: skip

        got = sc.feature_vector(names, {"severe": 0.5, "cyber": None}, _META)

        assert got[0] == pytest.approx(0.0)  # logit(0.5)
        assert got[1] == 0.0  # 欠測は中立
        assert got[2:6] == [1.0, 0.0, 1.0, 0.0]
        assert got[6] == 1.0
        assert got[7] == pytest.approx(0.98)
        assert got[8] == 1.0
        assert got[9] == pytest.approx(math.log1p(1_000_000.0) / 25.0)
        assert got[10] == pytest.approx(3 / 20)

    def test_probability_is_clamped_in_logit(self) -> None:
        (hi,) = sc.feature_vector(["p.severe"], {"severe": 1.0}, _META)
        (lo,) = sc.feature_vector(["p.severe"], {"severe": 0.0}, _META)

        assert hi == pytest.approx(-lo)
        assert hi < 10

    def test_unknown_feature_name_raises(self) -> None:
        with pytest.raises(ValueError, match="未知の特徴量"):
            sc.feature_vector(["bogus"], {}, _META)


class TestModelLoad:
    def test_loads_and_scores(self, tmp_path: Path) -> None:
        model = _model(tmp_path)

        high = model.probability({"severe": 0.99}, _META)
        low = model.probability({"severe": 0.01}, _META)

        assert high == pytest.approx(0.9)
        assert low == pytest.approx(0.1)
        assert model.probe_model == _PROBE_MODEL
        assert model.threshold == 0.5

    def test_missing_or_broken_model_is_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        unknown = tmp_path / "unk.json"
        _write_model(unknown, names=["p.severe", "weird"])

        assert sc.load_severity_c_model(str(tmp_path / "none.json")) is None
        assert sc.load_severity_c_model(str(bad)) is None
        assert sc.load_severity_c_model(str(unknown)) is None


class TestForestProbability:
    def test_threshold_is_compared_in_float64(self) -> None:
        # Arrange: float32 では 1.875 と区別できない閾値 (1.8749999) に特徴量 1.875 が来る。
        # 閾値を float32 に丸めると「以下」と誤判定して左に進む
        tree: dict[str, list[float]] = {
            "left": [1, -1, -1], "right": [2, -1, -1], "feature": [0, -2, -2],
            "threshold": [1.8749999, 0.0, 0.0], "proba": [0.5, 0.0, 1.0],
        }  # fmt: skip

        # Act / Assert
        assert sc.forest_probability([tree], [1.875]) == 1.0
        assert sc.forest_probability([tree], [1.8749999]) == 0.0

    def test_averages_trees(self) -> None:
        leaf0: dict[str, list[float]] = {
            "left": [-1],
            "right": [-1],
            "feature": [-2],
            "threshold": [0.0],
            "proba": [0.2],
        }
        leaf1 = {**leaf0, "proba": [0.6]}

        assert sc.forest_probability([leaf0, leaf1], [0.0]) == pytest.approx(0.4)


class TestScope:
    @pytest.mark.parametrize(
        ("basis", "kind", "expected"),
        [
            ("non_cyber", "", "non_cyber"),
            ("derivative", "", "derivative"),
            ("incident", "roundup", "roundup"),
            ("incident", "breach", None),
            ("no_axes", "", None),
            ("kev", "advisory", None),
        ],
    )
    def test_skip_reason(self, basis: str, kind: str, expected: str | None) -> None:
        assert sc.scope_skip_reason(basis, kind) == expected

    def test_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(sc.SHADOW_ENV, raising=False)
        assert sc.shadow_enabled() is True
        monkeypatch.setenv(sc.SHADOW_ENV, "0")
        assert sc.shadow_enabled() is False


class TestGoldMarks:
    def test_roundup_kind_and_listed_ids_are_excluded(self) -> None:
        assert gold_exclusion("x", "roundup") == "roundup"
        for aid in ROUNDUP_GOLD_S3_IDS:
            assert gold_exclusion(aid, "advisory") == "roundup"
        assert len(ROUNDUP_GOLD_S3_IDS) == 4

    def test_ordinary_article_is_kept(self) -> None:
        assert gold_exclusion("rss:https://example.com/a", "breach") is None


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "c.db")


def _add(
    repo: RunHistoryRepository, aid: str, *, category: str = "breach", article_type: str = "breaking",
    title: str = "ある組織への不正アクセス", hours_ago: float = 1.0, kind: str = "",
) -> None:  # fmt: skip
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid, article_id=aid, title=title, summary="要約",
            url=f"https://kuebiko.example/{aid}", status="posted", importance="high",
            category=category, article_type=article_type,
            created_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        )
    )  # fmt: skip
    if kind:
        repo.set_article_kind(aid, kind, "m")


class _Probe:
    def __init__(self, value: float | None = 0.99, missing: int = 0) -> None:
        self.calls: list[str] = []
        self.value, self.missing = value, missing

    async def __call__(
        self, title: str, published: str | None, body: str
    ) -> dict[str, float | None]:
        self.calls.append(title)
        return {
            q: (None if i < self.missing else self.value) for i, q in enumerate(sc.QUESTION_IDS)
        }


class TestRunShadow:
    async def test_skips_out_of_scope_without_probing(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        # Arrange
        _add(repo, "geo", category="geopolitical")
        _add(repo, "op", article_type="opinion")
        _add(repo, "wk", kind="roundup")
        _add(repo, "ok")
        probe = _Probe()

        # Act
        summary = await job.run_shadow(
            repo, probe=probe, probe_model=_PROBE_MODEL, model=_model(tmp_path), budget_s=100.0
        )

        # Assert: 問いを当てたのは ok の 1 件だけ
        assert probe.calls == ["ある組織への不正アクセス"]
        assert (summary["probed"], summary["skipped"]) == (1, 3)
        rows = {r.article_id: r for r in repo.list_severity_c_shadow(days=1)}
        assert rows["geo"].skip_reason == "non_cyber" and rows["geo"].p_s3 is None
        assert rows["op"].skip_reason == "derivative"
        assert rows["wk"].skip_reason == "roundup"
        assert rows["ok"].skip_reason is None
        assert rows["ok"].would_s3 is True and rows["ok"].model_match is True
        assert rows["ok"].a_basis == "no_axes"

    async def test_records_model_mismatch(self, repo: RunHistoryRepository, tmp_path: Path) -> None:
        _add(repo, "ok")

        await job.run_shadow(
            repo,
            probe=_Probe(),
            probe_model="kuebiko-sft:s24b",
            model=_model(tmp_path),
            budget_s=100.0,
        )

        (row,) = repo.list_severity_c_shadow(days=1)
        assert row.model_match is False
        assert (row.probe_model, row.trained_model) == ("kuebiko-sft:s24b", _PROBE_MODEL)

    async def test_budget_leaves_rest_for_next_hour_but_still_skips(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        # Arrange: 時計が 1 回の判定ごとに 100 秒進む
        for i in range(3):
            _add(repo, f"ok{i}")
        _add(repo, "geo", category="policy")
        ticks = iter(range(0, 100_000, 100))

        # Act
        summary = await job.run_shadow(
            repo, probe=_Probe(), probe_model=_PROBE_MODEL, model=_model(tmp_path),
            budget_s=150.0, clock=lambda: float(next(ticks)),
        )  # fmt: skip

        # Assert: 予算内の分だけ問い、外す記事は予算に関係なく記録、残りは未記録のまま
        assert summary["probed"] < 3
        recorded = {r.article_id for r in repo.list_severity_c_shadow(days=1)}
        assert "geo" in recorded
        assert len(job.pending_rows(repo)) == 3 - summary["probed"]

    async def test_failed_probes_are_not_recorded_and_stop_after_streak(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        for i in range(6):
            _add(repo, f"ok{i}")
        probe = _Probe(value=None)

        summary = await job.run_shadow(
            repo, probe=probe, probe_model=_PROBE_MODEL, model=_model(tmp_path), budget_s=100.0
        )

        assert repo.list_severity_c_shadow(days=1) == []
        assert summary["failed"] == job.MAX_CONSECUTIVE_FAILURES
        assert len(probe.calls) == job.MAX_CONSECUTIVE_FAILURES

    async def test_partial_missing_is_recorded_but_too_many_is_not(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        _add(repo, "ok")
        few = _Probe(missing=job.MAX_MISSING_QUESTIONS)
        many = _Probe(missing=job.MAX_MISSING_QUESTIONS + 1)

        await job.run_shadow(
            repo, probe=many, probe_model=_PROBE_MODEL, model=_model(tmp_path), budget_s=100.0
        )
        assert repo.list_severity_c_shadow(days=1) == []
        await job.run_shadow(
            repo, probe=few, probe_model=_PROBE_MODEL, model=_model(tmp_path), budget_s=100.0
        )

        (row,) = repo.list_severity_c_shadow(days=1)
        assert json.loads(row.probs_json or "{}")["cyber"] is None

    async def test_old_and_already_recorded_articles_are_not_selected(
        self, repo: RunHistoryRepository, tmp_path: Path
    ) -> None:
        _add(repo, "old", hours_ago=job.LOOKBACK_HOURS + 5)
        _add(repo, "done")
        repo.record_severity_c_shadow(SeverityCShadowRow("done", skip_reason="roundup"))
        probe = _Probe()

        summary = await job.run_shadow(
            repo, probe=probe, probe_model=_PROBE_MODEL, model=_model(tmp_path), budget_s=100.0
        )

        assert summary["pending"] == 0 and probe.calls == []

    async def test_entry_respects_flag_and_missing_model(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sc.SHADOW_ENV, "0")
        assert await job.run_severity_c_shadow(repo) == {"skipped": "flag_off"}
        monkeypatch.delenv(sc.SHADOW_ENV)
        monkeypatch.setattr(job, "load_severity_c_model", lambda: None)
        assert await job.run_severity_c_shadow(repo) == {"skipped": "no_model"}

    def test_budget_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(job.BUDGET_ENV, raising=False)
        assert job.shadow_budget_s() == job.DEFAULT_BUDGET_S
        monkeypatch.setenv(job.BUDGET_ENV, "30")
        assert job.shadow_budget_s() == 30.0
        monkeypatch.setenv(job.BUDGET_ENV, "x")
        assert job.shadow_budget_s() == job.DEFAULT_BUDGET_S

    def test_fits_hourly_stage_limit(self) -> None:
        # 1 記事 3.8 秒 × (予算内の件数) が段の上限 5 分から軸の段 (~1 分) を引いた枠に収まる
        assert job.DEFAULT_BUDGET_S + 60 + 3.8 < 5 * 60


class TestRepo:
    def test_record_roundtrip_unique_and_purge(self, repo: RunHistoryRepository) -> None:
        row = SeverityCShadowRow(
            "a1", p_s3=0.8, would_s3=True, threshold=0.5, probe_model="m", trained_model="m",
            model_match=True, probs_json="{}", a_severity="S2", a_basis="harm",
        )  # fmt: skip

        repo.record_severity_c_shadow(row)
        repo.record_severity_c_shadow(SeverityCShadowRow("a1", skip_reason="roundup"))  # 無視される

        (got,) = repo.list_severity_c_shadow(days=1)
        assert (got.p_s3, got.would_s3, got.skip_reason, got.a_severity) == (0.8, True, None, "S2")
        assert repo.purge_severity_c_shadow(days=0) == 1
        assert repo.list_severity_c_shadow(days=1) == []


class TestSummary:
    def test_crosstab_skips_and_disagreements(self) -> None:
        rows = [
            SeverityCShadowRow(
                "s3both", p_s3=0.9, would_s3=True, a_severity="S3", model_match=True
            ),
            SeverityCShadowRow("conly", p_s3=0.7, would_s3=True, a_severity="S2", model_match=True),
            SeverityCShadowRow(
                "aonly", p_s3=0.2, would_s3=False, a_severity="S3", model_match=False
            ),
            SeverityCShadowRow("neither", p_s3=0.1, would_s3=False, a_basis="no_axes"),
            SeverityCShadowRow("skip", skip_reason="roundup"),
        ]

        out = summarize(rows, days=7, limit=10)

        assert (out["total"], out["scored"], out["skipped"]) == (5, 4, {"roundup": 1})
        assert {"a_severity": "none", "c_s3": False, "count": 1} in out["crosstab"]
        assert out["model_mismatch"] == 1
        assert {d["article_id"] for d in out["disagreements"]} == {"conly", "aonly"}
        assert sum(out["p_histogram"]) == 4 and out["p_histogram"][9] == 1

    def test_limit_applies_to_disagreements(self) -> None:
        rows = [
            SeverityCShadowRow(f"a{i}", p_s3=0.9, would_s3=True, a_severity="S2") for i in range(5)
        ]

        assert len(summarize(rows, days=1, limit=2)["disagreements"]) == 2


@pytest.fixture
def api_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[TestClient, RunHistoryRepository]]:
    project = _bootstrap_project(tmp_path)
    monkeypatch.setenv("CTI_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    from src.ui import app as app_module

    with TestClient(app_module.create_app()) as c:
        yield c, RunHistoryRepository()


class TestApi:
    def test_summary_endpoint(self, api_client: tuple[TestClient, RunHistoryRepository]) -> None:
        client, repo = api_client
        repo.record_severity_c_shadow(
            SeverityCShadowRow("x", p_s3=0.9, would_s3=True, a_severity="S2", model_match=True)
        )
        repo.record_severity_c_shadow(SeverityCShadowRow("y", skip_reason="non_cyber"))

        data: dict[str, Any] = client.get("/api/v1/severity-c-shadow/summary?days=7").json()

        assert (data["total"], data["scored"], data["skipped"]) == (2, 1, {"non_cyber": 1})
        assert data["disagreements"][0]["article_id"] == "x"

    def test_summary_is_not_public(self) -> None:
        from src.ui.read_only_policy import is_read_only_blocked_get

        assert is_read_only_blocked_get("/api/v1/severity-c-shadow/summary") is True


def test_fill_rate_metric_registered() -> None:
    from src.ui.services.fill_rate_audit import METRICS

    assert "severity_c_shadow" in {m.key for m in METRICS}
