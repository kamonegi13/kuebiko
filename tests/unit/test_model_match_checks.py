"""P0-04: モデルの照合 3 つ (割当 probe / 組織名の SFT 判定 / pair の判定器)。

どれも本番の判定を変えない: 割当と一致・未宣言なら従来どおり、pair は警告だけ。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.assessment import assign_model
from src.assessment.assign_features import FEATURE_NAMES_WITH_PROBE
from src.eventnews import pair_model
from src.eventnews.pair_model import PairModel, pair_llm_mismatch
from src.tools.model_tiers import Step
from src.tools.task_prefix import is_prefix_trained


def _write_probe_json(path: Path, *, probe_model: str | None) -> Path:
    tree = {
        "left": [-1],
        "right": [-1],
        "feature": [-2],
        "threshold": [-2.0],
        "proba": [0.9],
    }
    body: dict[str, object] = {
        "feature_names": list(FEATURE_NAMES_WITH_PROBE),
        "threshold": 0.6,
        "trees": [tree],
    }
    if probe_model is not None:
        body["probe_model"] = probe_model
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_caches() -> None:
    assign_model._load_probe_file.cache_clear()


def _assign_to(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    monkeypatch.setattr(assign_model, "resolve_step_model", lambda step, **_: model)


class TestAssignProbeModelMatch:
    def test_matching_probe_model_is_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assign_to(monkeypatch, "gemma4:26b")
        path = _write_probe_json(tmp_path / "p.json", probe_model="gemma4:26b")

        loaded = assign_model.load_assign_model_probe(str(path))

        assert loaded is not None
        assert loaded.probe_model == "gemma4:26b"

    def test_mismatched_probe_model_falls_back_to_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assign_to(monkeypatch, "kuebiko-sft:s24ci65")
        path = _write_probe_json(tmp_path / "p.json", probe_model="gemma4:26b")

        assert assign_model.load_assign_model_probe(str(path)) is None

    def test_follows_assignment_changes_without_reload(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _write_probe_json(tmp_path / "p.json", probe_model="gemma4:26b")
        _assign_to(monkeypatch, "gemma4:26b")
        assert assign_model.load_assign_model_probe(str(path)) is not None

        _assign_to(monkeypatch, "kuebiko-sft:s23i")
        assert assign_model.load_assign_model_probe(str(path)) is None

    def test_missing_key_is_used_as_before_in_stage_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assign_to(monkeypatch, "kuebiko-sft:s24ci65")
        path = _write_probe_json(tmp_path / "p.json", probe_model=None)

        assert assign_model.load_assign_model_probe(str(path)) is not None

    def test_missing_key_is_refused_once_required(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _assign_to(monkeypatch, "gemma4:26b")
        monkeypatch.setattr(assign_model, "REQUIRE_PROBE_MODEL_KEY", True)
        path = _write_probe_json(tmp_path / "p.json", probe_model=None)

        assert assign_model.load_assign_model_probe(str(path)) is None

    def test_resolve_failure_does_not_stop_assignment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(step: Step, **_: object) -> str:
            raise RuntimeError("db down")

        monkeypatch.setattr(assign_model, "resolve_step_model", boom)
        path = _write_probe_json(tmp_path / "p.json", probe_model="gemma4:26b")

        assert assign_model.load_assign_model_probe(str(path)) is not None

    def test_real_probe_json_loads_for_current_assignment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """本物の JSON を読む: 足し忘れ・形の崩れで割当が base に落ちないこと。"""
        raw = json.loads(assign_model.PROBE_MODEL_PATH.read_text(encoding="utf-8"))
        declared = raw.get("probe_model")
        _assign_to(monkeypatch, declared or "gemma4:26b")

        assert assign_model.load_assign_model_probe() is not None


class TestIsPrefixTrained:
    @pytest.mark.parametrize(
        "model",
        ["kuebiko-sft:s23i", "kuebiko-sft:s24ci65", "kuebiko-sft:s24bi50", "kuebiko-sft:n19"],
    )
    def test_exact_and_family_match(self, model: str) -> None:
        assert is_prefix_trained(model)

    @pytest.mark.parametrize("model", ["gemma4:26b", "kuebiko-sft:26b", "kuebiko-sft:s17"])
    def test_untrained_models_are_not_matched(self, model: str) -> None:
        assert not is_prefix_trained(model)

    def test_env_extra_tag_is_matched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "kuebiko-sft:tmp1")
        assert is_prefix_trained(" kuebiko-sft:tmp1 ")


class TestOrgIdentityRefusesFamily:
    @pytest.mark.asyncio
    async def test_s24_family_judge_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.config_loader import AppConfig
        from src.eventnews import org_identity as oi

        monkeypatch.setattr(
            oi, "resolve_step_model", lambda step, db_path=None: "kuebiko-sft:s24ci65"
        )

        async def must_not_run(*_: object, **__: object) -> dict[str, float]:
            raise AssertionError("SFT 系統で判定してはならない")

        monkeypatch.setattr(oi, "probe_choice", must_not_run)
        assert await oi.judge_name_pair("A", "a", "B", "b", config=AppConfig()) is None


def _pair(trained: tuple[str, ...]) -> PairModel:
    return PairModel(
        feature_names=("a",),
        threshold_llm_on=0.5,
        threshold_llm_off=0.5,
        _trees=(),
        trained_llm_models=trained,
    )


class TestPairLlmCheck:
    def test_empty_declaration_never_mismatches(self) -> None:
        assert not pair_llm_mismatch(_pair(()), "kuebiko-sft:s23i")

    def test_declared_and_actual_equal(self) -> None:
        assert not pair_llm_mismatch(_pair(("kuebiko-sft:s23i",)), " kuebiko-sft:s23i ")

    def test_declared_and_actual_differ(self) -> None:
        assert pair_llm_mismatch(_pair(("kuebiko-sft:s23i",)), "kuebiko-sft:s24ci65")

    def test_warning_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.tools.model_tiers as mt

        monkeypatch.setattr(mt, "resolve_step_model", lambda step, **_: "gemma4:26b")
        pair_model.warn_on_pair_llm_mismatch(_pair(("kuebiko-sft:s23i",)))

    def test_off_skips_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.tools.model_tiers as mt

        def boom(step: Step, **_: object) -> str:
            raise AssertionError("off では解決しない")

        monkeypatch.setattr(mt, "resolve_step_model", boom)
        monkeypatch.setenv("PAIR_MODEL_CHECK", "off")
        pair_model.warn_on_pair_llm_mismatch(_pair(("x",)))

    def test_real_pair_json_declares_unknown(self) -> None:
        """本物の JSON: 宣言は空 (不明) のまま、既存の読み込みが壊れていない。"""
        pair_model.load_model.cache_clear()
        model = pair_model.load_model()
        assert model is not None
        assert model.trained_llm_models == ()
