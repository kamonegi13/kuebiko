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


class TestExtraFields:
    """s23 (2026-10-08): 本文から付ける 5 欄。"""

    def test_schema_has_the_five_extra_fields_optional(self) -> None:
        fields = sa.SeverityAxes.model_fields
        for name in sa.EXTRA_FIELDS:
            assert name in fields
            assert fields[name].default is None  # 欠測は None (false に既定しない)

    def test_missing_extra_fields_stay_none(self) -> None:
        axes = sa.SeverityAxes.model_validate(_AXES)
        for name in sa.EXTRA_FIELDS:
            assert getattr(axes, name) is None

    def test_extra_fields_accept_their_values(self) -> None:
        axes = sa.SeverityAxes.model_validate(
            {
                **_AXES,
                "recent_action": True,
                "victim_size": "large",
                "recoverability": "not_recoverable",
                "credential_compromise": False,
                "distribution_compromise": True,
            }
        )
        assert axes.victim_size == "large"
        assert axes.distribution_compromise is True


class TestBodyPrompt:
    """``AXES_FROM_BODY`` フラグ — 既定 (0) は今日の prompt と byte-identical。"""

    def test_default_build_prompt_is_unchanged(self) -> None:
        # 既定経路 (build_prompt) は本改修で一切触っていないことを文字列で固定する
        p = sa.build_prompt("見出し", "要約です")
        assert p == sa.PROMPT.format(title="見出し", summary="要約です")

    def test_flag_off_classify_axes_ignores_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AXES_FROM_BODY", raising=False)
        seen: list[str] = []

        class _Recorder:
            model = "m"

            async def generate_structured(self, prompt: str, *a: Any, **k: Any) -> Any:
                seen.append(prompt)
                return sa.SeverityAxes.model_validate(_AXES)

        asyncio.run(sa.classify_axes(_Recorder(), "見出し", "要約", body="本文のはず"))  # type: ignore[arg-type]
        assert seen == [sa.build_prompt("見出し", "要約")]
        assert "本文のはず" not in seen[0]

    def test_flag_on_without_body_still_uses_summary_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AXES_FROM_BODY", "1")
        assert sa.axes_from_body_enabled() is True

        seen: list[str] = []

        class _Recorder:
            model = "m"

            async def generate_structured(self, prompt: str, *a: Any, **k: Any) -> Any:
                seen.append(prompt)
                return sa.SeverityAxes.model_validate(_AXES)

        asyncio.run(sa.classify_axes(_Recorder(), "見出し", "要約", body=""))  # type: ignore[arg-type]
        assert seen == [sa.build_prompt("見出し", "要約")]

    def test_flag_on_with_body_uses_body_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AXES_FROM_BODY", "1")
        seen: list[str] = []

        class _Recorder:
            model = "m"

            async def generate_structured(self, prompt: str, *a: Any, **k: Any) -> Any:
                seen.append(prompt)
                return sa.SeverityAxes.model_validate(_AXES)

        asyncio.run(sa.classify_axes(_Recorder(), "見出し", "要約は無視", body="攻撃の本文"))  # type: ignore[arg-type]
        assert "攻撃の本文" in seen[0]
        assert "要約は無視" not in seen[0]
        assert "distribution_compromise" in seen[0]

    def test_body_prompt_truncates_at_teacher_limit(self) -> None:
        p = sa.build_body_prompt("見出し", "あ" * (sa.BODY_MAX_CHARS + 500))
        assert "あ" * sa.BODY_MAX_CHARS in p
        assert "あ" * (sa.BODY_MAX_CHARS + 1) not in p
        assert "ここで切れています" in p

    def test_body_prompt_lists_every_axis_option_and_extra_fields(self) -> None:
        p = sa.build_body_prompt("t", "b")
        for opts in sa.AXES.values():
            for o in opts:
                assert o in p, o
        for name in sa.EXTRA_FIELDS:
            assert name in p


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

    def test_missing_extra_fields_are_absent_from_the_dict(
        self, repo: RunHistoryRepository
    ) -> None:
        # s21 (要約入力) は 5 欄を返さない — NULL のまま、"false" に化けさせない
        repo.set_severity_axes("a1", sa.SeverityAxes.model_validate(_AXES).model_dump(), "s21")
        got = repo.get_severity_axes(["a1"])["a1"]
        for name in sa.EXTRA_FIELDS:
            assert name not in got

    def test_extra_fields_round_trip_booleans_and_enums(self, repo: RunHistoryRepository) -> None:
        axes = sa.SeverityAxes.model_validate(
            {
                **_AXES,
                "recent_action": True,
                "victim_size": "large",
                "recoverability": "not_recoverable",
                "credential_compromise": False,
                "distribution_compromise": True,
            }
        )
        repo.set_severity_axes("a1", axes.model_dump(), "s23")
        got = repo.get_severity_axes(["a1"])["a1"]
        assert got["recent_action"] == "true"
        assert got["credential_compromise"] == "false"
        assert got["distribution_compromise"] == "true"
        assert got["victim_size"] == "large"
        assert got["recoverability"] == "not_recoverable"

    def _importance_ids(self, repo: RunHistoryRepository) -> set[str]:
        with repo._connect() as conn:
            return {
                str(r[0])
                for r in conn.execute("SELECT article_id FROM article_importance_v2").fetchall()
            }

    def _save_importance(self, repo: RunHistoryRepository, aid: str) -> None:
        with repo._connect() as conn:
            conn.execute(
                "INSERT INTO article_importance_v2 (article_id, severity, severity_basis,"
                " strategic_weight, jp, nations, sir_ids, relevant, rule_version, created_at)"
                " VALUES (?, 'S2', 'x', NULL, 'none', '[]', '[]', 0, 'v', '2026-10-27')",
                (aid,),
            )

    def test_replace_overwrites_axes_and_drops_importance(self, repo: RunHistoryRepository) -> None:
        # 付け直した記事は軸が新しいモデルに替わり、importance_v2 は毎時の段の付け直しに回る
        repo.set_severity_axes("a1", _AXES, "s23i")
        repo.set_severity_axes("a2", _AXES, "s23i")
        self._save_importance(repo, "a1")
        self._save_importance(repo, "a2")
        new = sa.SeverityAxes.model_validate(
            {**_AXES, "scope": "national", "recent_action": True}
        ).model_dump()

        n = repo.replace_severity_axes([("a1", new)], "s24ci65")

        got = repo.get_severity_axes(["a1", "a2"])
        assert n == 1
        assert got["a1"]["scope"] == "national" and got["a1"]["model"] == "s24ci65"
        assert got["a1"]["recent_action"] == "true"
        assert got["a2"]["model"] == "s23i"
        assert self._importance_ids(repo) == {"a2"}

    def test_replace_inserts_articles_without_prior_axes(self, repo: RunHistoryRepository) -> None:
        repo.replace_severity_axes([("a9", _AXES)], "s24ci65")
        assert repo.get_severity_axes(["a9"])["a9"]["model"] == "s24ci65"

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


class TestAxesFromBodyWiring:
    """3 つの呼び出し経路が本文を渡す。AXES_FROM_BODY=0 では prompt が従来と byte-identical。"""

    class _LLM:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def generate_structured(
            self, prompt: str, *_a: object, **_k: object
        ) -> sa.SeverityAxes:
            self.prompts.append(prompt)
            return sa.SeverityAxes.model_validate(_AXES)

    def test_flag_off_prompt_is_byte_identical_even_with_body(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AXES_FROM_BODY", raising=False)
        llm = self._LLM()
        asyncio.run(sa.classify_axes(llm, "t", "s", "本文あり"))  # type: ignore[arg-type]
        assert llm.prompts == [sa.build_prompt("t", "s")]

    def test_flag_on_uses_body_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AXES_FROM_BODY", "1")
        llm = self._LLM()
        asyncio.run(sa.classify_axes(llm, "t", "s", "本文あり"))  # type: ignore[arg-type]
        assert llm.prompts == [sa.build_body_prompt("t", "本文あり")]

    def test_ensure_axes_passes_body_only_when_given(self) -> None:
        from src.synthesis.grounded.detect_ml import ensure_axes

        seen: list[tuple[str, ...]] = []

        async def classify(*args: str) -> dict[str, str] | None:
            seen.append(args)
            return dict(_AXES)

        repo = TestEnsureAxes._Repo(set())
        asyncio.run(
            ensure_axes(
                repo,
                [("a", "ta", "sa"), ("b", "tb", "sb")],
                classify,
                model_label="m",
                bodies={"a": "BODY"},
            )
        )
        assert sorted(seen) == [("ta", "sa", "BODY"), ("tb", "sb")]

    def test_fetch_bodies_off_returns_empty_without_db(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AXES_FROM_BODY", raising=False)

        class _NoDb:
            def _connect(self) -> None:
                raise AssertionError("DB を読んではならない")

        assert sa.fetch_bodies(_NoDb(), ["a"]) == {}

    def test_fetch_bodies_on_reads_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AXES_FROM_BODY", "1")
        repo = RunHistoryRepository(tmp_path / "r.db")
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "INSERT INTO runs (started_at, pipeline, dry_run, status)"
                " VALUES ('2026-09-25T00:00:00+00:00', 't', 0, 'done')"
            )
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            for body in ("長い本文です",):
                conn.execute(
                    "INSERT INTO articles (run_id, article_id, title, summary, url, status, body,"
                    " created_at)"
                    " VALUES (?, 'x', 't', 's', 'https://kuebiko.example/x', 'posted', ?,"
                    " '2026-09-25T02:00:00+00:00')",
                    (rid, body),
                )
        assert sa.fetch_bodies(repo, ["x", "none"]) == {"x": "長い本文です"}


class TestPromptV10:
    """本文入力の prompt は教師 (axes_prompt_v10) と同一、公開日つき (s24 の切替用、既定 off)。"""

    def test_matches_teacher_prompt_byte_for_byte(self) -> None:
        import importlib.util
        from pathlib import Path

        path = Path(__file__).resolve().parents[2] / "data" / "mlx" / "axes_prompt_v10.py"
        if not path.exists():
            pytest.skip("data/mlx/axes_prompt_v10.py が無い")
        spec = importlib.util.spec_from_file_location("axes_prompt_v10", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert sa.PROMPT_BODY == mod.PROMPT_BODY_V10
        for pub in ("2026-10-01", None):
            assert sa.build_body_prompt("見出し", "本文", pub) == mod.build_body_prompt_v10(
                "見出し", "本文", pub
            )

    def test_published_is_in_the_prompt_and_defaults_to_unknown(self) -> None:
        assert "公開日: 2026-10-01\n" in sa.build_body_prompt("t", "b", "2026-10-01")
        assert "公開日: 不明\n" in sa.build_body_prompt("t", "b")

    def test_classify_axes_passes_published_to_body_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AXES_FROM_BODY", "1")
        seen: list[str] = []

        class _LLM:
            async def generate_structured(self, prompt: str, *_a: object, **_k: object) -> Any:
                seen.append(prompt)
                return sa.SeverityAxes.model_validate(_AXES)

        asyncio.run(sa.classify_axes(_LLM(), "t", "s", "本文", "2026-10-02"))  # type: ignore[arg-type]
        assert seen == [sa.build_body_prompt("t", "本文", "2026-10-02")]

    def test_flag_off_summary_prompt_ignores_published(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("AXES_FROM_BODY", raising=False)
        seen: list[str] = []

        class _LLM:
            async def generate_structured(self, prompt: str, *_a: object, **_k: object) -> Any:
                seen.append(prompt)
                return sa.SeverityAxes.model_validate(_AXES)

        asyncio.run(sa.classify_axes(_LLM(), "t", "s", "本文", "2026-10-02"))  # type: ignore[arg-type]
        assert seen == [sa.build_prompt("t", "s")]

    def test_fetch_published_is_noop_when_flag_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AXES_FROM_BODY", raising=False)
        assert sa.fetch_published(object(), ["a"]) == {}

    def test_ensure_axes_passes_published_as_fourth_argument(self) -> None:
        from src.synthesis.grounded.detect_ml import ensure_axes

        seen: list[tuple[object, ...]] = []

        async def classify(*args: object) -> dict[str, str] | None:
            seen.append(args)
            return dict(_AXES)

        class _Repo:
            def get_severity_axes(self, ids: list[str]) -> dict[str, object]:
                return {}

            def set_severity_axes(self, *_a: object) -> None:
                return None

        asyncio.run(
            ensure_axes(
                _Repo(),
                [("a", "t", "s")],
                classify,
                model_label="m",
                bodies={"a": "本文"},
                published={"a": "2026-10-02"},
            )
        )
        assert seen == [("t", "s", "本文", "2026-10-02")]
