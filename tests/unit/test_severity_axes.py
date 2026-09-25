"""深刻度の軸 (2026-09-25) — 語彙・特徴量・保存・穴埋め・毎時の段。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, get_args

import pytest

from src.cti import severity_axes as sa
from src.storage.run_history import RunHistoryRepository

_AXES = {
    "scope": "single_org",
    "impact": "data_exposure",
    "confirmation": "possible",
    "magnitude": "unknown",
    "exploitation": "not_applicable",
    "actor": "unknown",
    "target": "general_org",
}


class TestVocabulary:
    def test_axes_follow_the_schema_literals(self) -> None:
        fields = sa.SeverityAxes.model_fields
        for name, opts in sa.AXES.items():
            assert opts == get_args(fields[name].annotation)

    def test_japan_is_not_an_axis(self) -> None:
        # 関連性 (日本かどうか) は別の軸。深刻度に混ぜると triage と同じ混同が起きる
        assert not any("japan" in n or "jp" in n.lower() for n in sa.AXIS_FEATURE_NAMES)

    def test_prompt_lists_every_option(self) -> None:
        # 選択肢を足したらプロンプトにも書く (書かなければ LLM が選べない)
        for opts in sa.AXES.values():
            for o in opts:
                assert o in sa.PROMPT, o

    def test_out_of_vocabulary_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            sa.SeverityAxes.model_validate({**_AXES, "scope": "galaxy"})


class TestFeatures:
    def test_vector_matches_names(self) -> None:
        assert len(sa.axis_feature_vector(_AXES, "")) == len(sa.AXIS_FEATURE_NAMES)

    def test_one_hot_marks_each_axis_once(self) -> None:
        vec = sa.axis_feature_vector(_AXES, "")
        on = {n for n, v in zip(sa.AXIS_FEATURE_NAMES, vec, strict=True) if v == 1.0}
        assert "axis_scope=single_org" in on and "axis_target=general_org" in on
        assert len(on) == len(sa.AXES)  # magnitude は one-hot でない (本文の数字から取る)

    def test_missing_axes_give_all_zero_one_hot(self) -> None:
        vec = sa.axis_feature_vector(None, "")
        assert vec == [0.0] * len(sa.AXIS_FEATURE_NAMES)

    @pytest.mark.parametrize(
        ("text", "order"),
        [
            ("最大1,360,563アカウントに影響、うちレンタルサーバは951アカウント", 6),
            ("顧客情報 771万件", 6),
            ("約2億件の記録", 8),
            ("数字なし", 0),
        ],
    )
    def test_magnitude_takes_the_largest_count(self, text: str, order: int) -> None:
        # LLM は「最大 120 万」を 951 と答えた — 規模は本文から機械的に取る (桁 = log10 の整数部)
        assert int(sa.magnitude_log10(text)) == order


class TestClassify:
    def test_failure_returns_none(self) -> None:
        class _Boom:
            model = "m"

            async def generate_structured(self, *a: Any, **k: Any) -> Any:
                raise RuntimeError("down")

        assert asyncio.run(sa.classify_axes(_Boom(), "t", "s")) is None  # type: ignore[arg-type]

    def test_prompt_truncates_long_summary(self) -> None:
        p = sa.build_prompt("見出し", "あ" * 5000)
        assert "あ" * 1500 in p and "あ" * 1501 not in p


class TestStorage:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> RunHistoryRepository:
        return RunHistoryRepository(db_path=tmp_path / "axes.db")

    def test_round_trip_keeps_model(self, repo: RunHistoryRepository) -> None:
        repo.set_severity_axes("a1", _AXES, "kuebiko-sft:s17")
        got = repo.get_severity_axes(["a1", "missing"])
        assert set(got) == {"a1"}
        assert got["a1"]["scope"] == "single_org" and got["a1"]["model"] == "kuebiko-sft:s17"

    def test_first_label_is_kept(self, repo: RunHistoryRepository) -> None:
        repo.set_severity_axes("a1", _AXES, "m1")
        repo.set_severity_axes("a1", {**_AXES, "scope": "national"}, "m2")
        assert repo.get_severity_axes(["a1"])["a1"]["scope"] == "single_org"

    def test_hourly_job_picks_unlabeled_posted_high_medium(
        self, repo: RunHistoryRepository
    ) -> None:
        from src.ui.services.severity_axes_job import pending_articles

        now = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (started_at, pipeline, dry_run, status)"
                " VALUES ('2026-09-25T00:00:00+00:00', 't', 0, 'done')"
            )
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            for aid, imp, status, ts in (
                ("new_med", "medium", "posted", "2026-09-25T02:00:00+00:00"),
                ("labeled", "high", "posted", "2026-09-25T02:00:00+00:00"),
                ("low", "low", "posted", "2026-09-25T02:00:00+00:00"),
                ("collected", "high", "collected", "2026-09-25T02:00:00+00:00"),
                ("old", "high", "posted", "2026-09-20T00:00:00+00:00"),
            ):
                conn.execute(
                    "INSERT INTO articles (run_id, article_id, title, summary, url, status,"
                    " importance, created_at) VALUES (?, ?, ?, 's', ?, ?, ?, ?)",
                    (rid, aid, f"t-{aid}", f"https://kuebiko.example/{aid}", status, imp, ts),
                )
        repo.set_severity_axes("labeled", _AXES, "m")

        got = pending_articles(repo, cap=10, now=now)

        assert [a[0] for a in got] == ["new_med"]


class TestEnsureAxes:
    class _Repo:
        def __init__(self, have: set[str]) -> None:
            self.have = have
            self.written: list[tuple[str, str]] = []

        def get_severity_axes(self, ids: list[str]) -> dict[str, dict[str, str]]:
            return {a: dict(_AXES) for a in ids if a in self.have}

        def set_severity_axes(self, aid: str, axes: dict[str, str], model: str) -> None:
            self.written.append((aid, model))

    def test_only_missing_up_to_limit_and_failures_not_saved(self) -> None:
        from src.synthesis.grounded.detect_ml import ensure_axes

        repo = self._Repo({"a"})

        async def classify(title: str, summary: str) -> dict[str, str] | None:
            return None if title == "tc" else dict(_AXES)

        n = asyncio.run(
            ensure_axes(
                repo,
                [("a", "ta", ""), ("b", "tb", ""), ("c", "tc", ""), ("d", "td", "")],
                classify,
                model_label="m",
                limit=2,
            )
        )

        assert n == 1  # b は保存、c は失敗 (次の run でまた試す)、d は上限超過
        assert repo.written == [("b", "m")]


def test_detect_features_include_axes() -> None:
    from src.synthesis.grounded.detect_features import FEATURE_NAMES, DetectArticle, feature_vector

    art = DetectArticle(
        article_id="a",
        title="t",
        summary="",
        importance="medium",
        category="breach",
        tier="news",
        kind="breach",
        axes=_AXES,
    )
    vec = dict(zip(FEATURE_NAMES, feature_vector(art), strict=True))
    assert vec["axis_scope=single_org"] == 1.0
    assert vec["axis_scope=national"] == 0.0


def test_shipped_detect_model_matches_the_feature_contract() -> None:
    """同梱モデルが軸つきの列で学習されていること (列ずれは load で None = ML が黙って外れる)。"""
    from src.synthesis.grounded.detect_ml import load_detect_model

    m = load_detect_model()
    assert m is not None, "同梱 detect モデルが列ずれしている"
    assert m.axes_model, "軸を付けたモデルが記録されていない (照合できない)"
