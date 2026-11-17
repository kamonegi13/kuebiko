"""組織名の別名の判定の s26 対応 (旗の既定 0 では本番の挙動が不変であることの固定)。"""

from __future__ import annotations

import hashlib

import pytest

from src.config_loader import AppConfig
from src.eventnews import org_identity as oi
from src.tools import decision_probe
from src.tools.model_tiers import Step
from src.tools.task_prefix import prefix_for

S26 = "kuebiko-sft:s26"
MARKER = "[task: org_identity]\n"
#: 旗を足す前の main (8968701b) の same_org_question の sha256 (固定入力)
GOLDEN_ORG_QUESTION = "19cfe68fdeb0858371bc87fe14da6ca3fea1770c8eaa2cd8d9f029fa122b228b"
GOLDEN_EVENT_QUESTION = "0e51da59d70b0281daf7e94bbe2f55ad76a65e360416bd5a7d8b8c26ed078322"
_ARGS = ("名A", "文A。", "名B", "文B。")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (oi.SFT_JUDGE_ENV, oi.ORG_QUESTION_V2_ENV, "SFT_TASK_PREFIX_MODELS"):
        monkeypatch.delenv(name, raising=False)


class TestOrgQuestionFlag:
    @pytest.mark.parametrize("value", [None, "0", "off", "bogus"])
    def test_default_wording_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, value: str | None
    ) -> None:
        if value is not None:
            monkeypatch.setenv(oi.ORG_QUESTION_V2_ENV, value)
        q = oi.same_org_question(*_ARGS)
        assert hashlib.sha256(q.encode()).hexdigest() == GOLDEN_ORG_QUESTION

    def test_v2_adds_parent_and_operator_criteria(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(oi.ORG_QUESTION_V2_ENV, "1")
        q = oi.same_org_question(*_ARGS)
        assert oi.OPUS_SAME_ORG_CRITERIA in q
        assert "親会社と子会社" in q and "別の組織" in q
        assert q != oi.same_org_question(*_ARGS, v2=False)

    def test_event_question_default_unchanged(self) -> None:
        assert (
            hashlib.sha256(oi.same_event_question(*_ARGS).encode()).hexdigest()
            == GOLDEN_EVENT_QUESTION
        )

    def test_v2_caution_matches_opus_criteria_for_both_questions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oi.ORG_QUESTION_V2_ENV, "1")
        for q in (oi.same_event_question(*_ARGS), oi.same_org_question(*_ARGS)):
            assert "名前そのものが同じ組織を指す" not in q
            assert "親会社・子会社・運営会社とそのサービス名" in q
            assert "別々の被害者" in q


class TestMarker:
    def test_marker_is_empty_until_s26_is_enabled(self) -> None:
        for model in (None, S26, "kuebiko-sft:s24ci65", "kuebiko-sft:s23i", "gemma4:26b"):
            assert prefix_for(Step.ORG_IDENTITY_JUDGE, model) == ""

    def test_marker_applies_only_to_s26_family_once_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", f"{S26},kuebiko-sft:s24ci65")
        assert prefix_for(Step.ORG_IDENTITY_JUDGE) == MARKER
        assert prefix_for(Step.ORG_IDENTITY_JUDGE, S26) == MARKER
        assert prefix_for(Step.ORG_IDENTITY_JUDGE, "kuebiko-sft:s24ci65") == ""

    def test_probe_prompt_carries_marker_inside_user_turn(self) -> None:
        prompt = decision_probe._json_biased_prompt("Q?", ("Yes", "No"), MARKER)
        assert prompt.startswith(f"<start_of_turn>user\n{MARKER}Q?")
        plain = decision_probe._json_biased_prompt("Q?", ("Yes", "No"))
        assert plain.startswith("<start_of_turn>user\nQ?")


class TestSftJudgeFlag:
    @staticmethod
    def _wire(monkeypatch: pytest.MonkeyPatch, model: str) -> list[str]:
        monkeypatch.setattr(oi, "resolve_step_model", lambda step, db_path=None: model)
        monkeypatch.setattr(oi, "effective_threshold", lambda *a, **k: 0.0)
        questions: list[str] = []

        async def fake(
            step: object, question: str, choices: tuple[str, ...], **_: object
        ) -> dict[str, float]:
            questions.append(question)
            return {"Yes": 0.99, "No": 0.01}

        monkeypatch.setattr(oi, "probe_choice", fake)
        return questions

    @pytest.mark.asyncio
    async def test_flag_off_refuses_s26(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", S26)
        questions = self._wire(monkeypatch, S26)
        assert await oi.judge_name_pair(*_ARGS[:2], *_ARGS[2:], config=AppConfig()) is None
        assert questions == []

    @pytest.mark.asyncio
    async def test_flag_on_allows_s26_but_not_other_sft(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(oi.SFT_JUDGE_ENV, "1")
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", S26)
        questions = self._wire(monkeypatch, S26)
        margin = await oi.judge_name_pair(*_ARGS, config=AppConfig())
        assert margin is not None and margin > 0 and len(questions) == 2

        questions = self._wire(monkeypatch, "kuebiko-sft:s23i")
        assert await oi.judge_name_pair(*_ARGS, config=AppConfig()) is None
        assert questions == []
