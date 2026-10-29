"""src.tools.decision_probe のテスト (S4)。

httpx.MockTransport で Ollama API をモックする (test_llm_client.py と同じ流儀)。
実 Ollama・DB には繋がない — モデル解決は ``MODEL_TIERS_CONFIG_DB=0`` で BUILTIN に固定する。
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from typing import Any

import httpx
import ollama
import pytest

from src.tools.decision_probe import (
    _extract_choice_probabilities,
    _is_json_biased,
    _json_biased_prompt,
    _normalize_token,
    probe_choice,
)
from src.tools.model_tiers import BUILTIN_MODEL_TIERS, Step, invalidate_model_tiers_cache

# ---------- fixtures / ヘルパ ----------


@pytest.fixture(autouse=True)
def _force_builtin_tiers(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    """DB を飛ばして BUILTIN (fast=gemma4:26b) に固定する。"""
    monkeypatch.setenv("MODEL_TIERS_CONFIG_DB", "0")
    invalidate_model_tiers_cache()
    yield
    invalidate_model_tiers_cache()


def _cfg(base_url: str = "http://localhost:11434") -> Any:
    class _Cfg:
        ollama_base_url = base_url

    return _Cfg()


def _logprobs_payload(
    chosen: tuple[str, float], top: list[tuple[str, float]]
) -> list[dict[str, object]]:
    token, logprob = chosen
    return [
        {
            "token": token,
            "logprob": logprob,
            "top_logprobs": [{"token": t, "logprob": lp} for t, lp in top],
        }
    ]


def _build_client(handler: Callable[[httpx.Request], httpx.Response]) -> ollama.AsyncClient:
    transport = httpx.MockTransport(handler)
    return ollama.AsyncClient(host="http://localhost:11434", transport=transport)


def _chat_response_handler(
    logprobs: list[dict[str, object]], content: str = "Yes"
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(
            200,
            json={
                "model": "gemma4:26b",
                "created_at": "2026-10-11T00:00:00Z",
                "message": {"role": "assistant", "content": content},
                "done": True,
                "logprobs": logprobs,
            },
        )

    return handler


def _generate_response_handler(
    logprobs: list[dict[str, object]], response_text: str = 'Yes"'
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/generate"
        return httpx.Response(
            200,
            json={
                "model": "kuebiko-sft:i58",
                "created_at": "2026-10-11T00:00:00Z",
                "response": response_text,
                "done": True,
                "logprobs": logprobs,
            },
        )

    return handler


# ---------- 純粋関数 ----------


class TestNormalizeToken:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("Yes", "yes"),
            ("yes.", "yes"),
            ("Yes,", "yes"),
            ("  No!  ", "no"),
            ("いいえ。", "いいえ"),
        ],
    )
    def test_strips_punctuation_and_case(self, token: str, expected: str) -> None:
        assert _normalize_token(token) == expected


class TestExtractChoiceProbabilities:
    def test_softmax_over_two_choices(self) -> None:
        candidates = [("Yes", -0.1), ("No", -3.0), ("Maybe", -5.0)]
        result = _extract_choice_probabilities(candidates, ("Yes", "No"))
        assert result is not None
        assert result["Yes"] > result["No"]
        assert pytest.approx(result["Yes"] + result["No"], abs=1e-9) == 1.0

    def test_picks_max_logprob_among_variants(self) -> None:
        # "Yes" の表記揺れが複数あるとき、最大の logprob (支配的な表記) を採用する。
        candidates = [("Yes", -4.0), ("yes.", -0.2), ("No", -0.3)]
        result = _extract_choice_probabilities(candidates, ("Yes", "No"))
        assert result is not None
        # yes. (-0.2) の方が Yes (-4.0) より高いので、Yes 側の確率は yes. 由来の値になる。
        assert result["Yes"] > result["No"]

    def test_missing_choice_takes_floor_of_candidates(self) -> None:
        """確信が強いと反対の選択肢が top から落ちる — None にせず候補の最小値を上限に使う。"""
        candidates = [("Yes", -0.0001), (" yes", -9.0), ("Maybe", -18.0)]
        probs = _extract_choice_probabilities(candidates, ("Yes", "No"))
        assert probs is not None
        assert probs["Yes"] > 0.9999
        assert 0.0 < probs["No"] < 1e-6
        assert abs(sum(probs.values()) - 1.0) < 1e-9

    def test_returns_none_when_no_choices_found(self) -> None:
        candidates = [("Maybe", -0.1)]
        assert _extract_choice_probabilities(candidates, ("Yes", "No")) is None

    def test_multi_token_choice_matches_by_first_token(self) -> None:
        """「いいえ」は「いい」+「え」に分かれる — 最初のトークンで照合する (10-11 実測)。

        完全一致だけで照合すると「いいえ」が見つからず floor 扱いになり、明白な「いいえ」が
        P(はい)≈0.88 に化けていた。
        """
        candidates = [("いい", 0.0), (" いい", -20.95), ("はい", -26.32), ("{{", -28.31)]
        probs = _extract_choice_probabilities(candidates, ("はい", "いいえ"))
        assert probs is not None
        assert probs["いいえ"] > 0.9999

    def test_token_prefix_of_several_choices_is_ignored(self) -> None:
        """複数の選択肢の先頭に当たるトークンはどちらの証拠にもしない。"""
        candidates = [("い", -0.1), ("はい", -5.0), ("x", -30.0)]
        probs = _extract_choice_probabilities(candidates, ("いいえ", "いいかも", "はい"))
        assert probs is not None
        assert probs["はい"] > 0.99


class TestJsonBiasedPrompt:
    def test_uses_gemma_raw_template_with_json_prefix(self) -> None:
        prompt = _json_biased_prompt("質問文", ("Yes", "No"))
        assert prompt.startswith("<start_of_turn>user\n")
        assert "<end_of_turn>\n<start_of_turn>model\n" in prompt
        assert prompt.endswith('{"answer": "')
        assert "質問文" in prompt
        assert "Yes または No" in prompt

    def test_is_json_biased_only_for_known_models(self) -> None:
        assert _is_json_biased("kuebiko-sft:i58") is True
        assert _is_json_biased("gemma4:26b") is False
        assert _is_json_biased("  kuebiko-sft:i58  ") is True

    def test_is_json_biased_reads_extra_models_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 評価中の一時タグ (SFT_TASK_PREFIX_MODELS) も raw + JSON 前置きの経路で測る
        monkeypatch.delenv("SFT_TASK_PREFIX_MODELS", raising=False)
        assert _is_json_biased("kuebiko-sft:x99") is False
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", " kuebiko-sft:x99 , other ")
        assert _is_json_biased("kuebiko-sft:x99") is True
        assert _is_json_biased("gemma4:26b") is False


# ---------- probe_choice (end-to-end、MockTransport) ----------


class TestProbeChoiceDefaultModel:
    @pytest.fixture(autouse=True)
    def _plain_default_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # chat 経路の検査: 既定 (BUILTIN の fast) が SFT になっても素のモデルで測る
        monkeypatch.setitem(BUILTIN_MODEL_TIERS, "fast", "gemma4:26b")

    @pytest.mark.asyncio
    async def test_returns_probabilities_via_chat_endpoint(self) -> None:
        logprobs = _logprobs_payload(("Yes", -0.05), [("Yes", -0.05), ("No", -3.0)])
        client = _build_client(_chat_response_handler(logprobs))

        result = await probe_choice(
            Step.THIN_BODY_JUDGE,
            "この本文は中身があるか",
            ("Yes", "No"),
            config=_cfg(),
            client=client,
        )

        assert result is not None
        assert result["Yes"] > 0.9
        assert pytest.approx(sum(result.values()), abs=1e-9) == 1.0

    @pytest.mark.asyncio
    async def test_returns_none_when_choices_absent_from_top_logprobs(self) -> None:
        logprobs = _logprobs_payload(("うーん", -0.1), [("うーん", -0.1), ("さあ", -2.0)])
        client = _build_client(_chat_response_handler(logprobs, content="うーん"))

        result = await probe_choice(
            Step.THIN_BODY_JUDGE,
            "この本文は中身があるか",
            ("Yes", "No"),
            config=_cfg(),
            client=client,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_logprobs_absent(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "gemma4:26b",
                    "created_at": "2026-10-11T00:00:00Z",
                    "message": {"role": "assistant", "content": "Yes"},
                    "done": True,
                },
            )

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_does_not_close_injected_client(self) -> None:
        logprobs = _logprobs_payload(("Yes", -0.05), [("Yes", -0.05), ("No", -3.0)])
        client = _build_client(_chat_response_handler(logprobs))

        await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )

        assert client._client.is_closed is False  # noqa: SLF001


class TestProbeChoiceJsonBiasedModel:
    @pytest.mark.asyncio
    async def test_uses_raw_generate_with_json_prefix(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # BUILTIN の fast ティア (gemma4:26b) を JSON-biased 扱いにして raw/generate 経路を
        # 通す (実モデル名に依存せず分岐のみ検証する)。
        monkeypatch.setattr(
            "src.tools.decision_probe._JSON_BIASED_MODELS", frozenset({"gemma4:26b"})
        )
        logprobs = _logprobs_payload(("Yes", -0.05), [("Yes", -0.05), ("No", -3.0)])
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            assert request.url.path == "/api/generate"
            import json as _json

            body = _json.loads(request.content)
            assert body["raw"] is True
            assert body["prompt"].endswith('{"answer": "')
            return httpx.Response(
                200,
                json={
                    "model": "gemma4:26b",
                    "created_at": "2026-10-11T00:00:00Z",
                    "response": 'Yes"',
                    "done": True,
                    "logprobs": logprobs,
                },
            )

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )

        assert seen_paths == ["/api/generate"]
        assert result is not None
        assert result["Yes"] > 0.9

    @pytest.mark.asyncio
    async def test_env_extra_model_uses_raw_generate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 評価中の一時タグ (SFT_TASK_PREFIX_MODELS) は chat でなく raw 経路で測られる
        monkeypatch.setattr(
            "src.tools.decision_probe.resolve_step_model",
            lambda step, db_path=None: "kuebiko-sft:s24x",
        )
        monkeypatch.setenv("SFT_TASK_PREFIX_MODELS", "kuebiko-sft:s24x")
        logprobs = _logprobs_payload(("Yes", -0.05), [("Yes", -0.05), ("No", -3.0)])
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            return httpx.Response(
                200,
                json={
                    "model": "kuebiko-sft:s24x",
                    "created_at": "2026-10-18T00:00:00Z",
                    "response": 'Yes"',
                    "done": True,
                    "logprobs": logprobs,
                },
            )

        result = await probe_choice(
            Step.THIN_BODY_JUDGE,
            "質問",
            ("Yes", "No"),
            config=_cfg(),
            client=_build_client(handler),
        )

        assert seen_paths == ["/api/generate"]
        assert result is not None


class TestProbeChoiceGuards:
    @pytest.mark.asyncio
    async def test_empty_choices_raises(self) -> None:
        with pytest.raises(ValueError):
            await probe_choice(Step.THIN_BODY_JUDGE, "質問", (), config=_cfg())

    @pytest.mark.asyncio
    async def test_returns_none_for_external_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "src.tools.decision_probe.resolve_step_model",
            lambda step, db_path=None: "anthropic:claude-sonnet-5",
        )
        result = await probe_choice(Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg())
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_forbidden_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "src.tools.decision_probe.resolve_step_model",
            lambda step, db_path=None: "qwen3:7b",
        )
        result = await probe_choice(Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg())
        assert result is None


class TestProbeChoiceErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_none_on_response_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "model not found"})

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_connect_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("boom")

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_generic_request_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RequestError("boom")

        client = _build_client(handler)
        result = await probe_choice(
            Step.THIN_BODY_JUDGE, "質問", ("Yes", "No"), config=_cfg(), client=client
        )
        assert result is None


def test_production_alias_s23i_uses_json_prefix() -> None:
    """本番名 s23i (i58 と同一 digest) にも JSON 前置きが効く (10-11 の取りこぼし防止)。"""
    from src.tools import decision_probe

    assert decision_probe._is_json_biased("kuebiko-sft:s23i")
    assert decision_probe._is_json_biased("kuebiko-sft:i58")
    assert not decision_probe._is_json_biased("gemma4:26b")
