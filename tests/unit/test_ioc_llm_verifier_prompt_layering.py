"""ioc_llm_verifier の層分け不変量テスト (python_block kind、group 4 の 1 本目、2026-08-21)。

.j2 系の block 方式 (tests/unit/test_grounded_blocks.py 等) と同じ核心を持つ:
**seed の blocks で合成した結果が legacy (frozen) 実装の出力と byte 一致**すること。
python_block は skeleton を持たない Python 所有プロンプトのため、skeleton_slots の
代わりに ``SLOT_IDS`` (対象モジュールの固定表) で slot を検証する。

既存の verify_iocs_with_llm / CVE cap のテスト (test_ioc_verifier_cve_cap.py) は
一切変更しない — 本ファイルは追加のみ。
"""

from __future__ import annotations

from typing import TypeVar, cast

import pytest
from pydantic import BaseModel

import src.cti.ioc_llm_verifier as v
from src.cti.ioc_extractor import ExtractedIocs
from src.cti.ioc_llm_verifier import (
    SAMPLE_TARGETS,
    SLOT_IDS,
    IocVerifyOutput,
    compose_from_blocks,
    verify_iocs_with_llm,
)
from src.prompts.prompt_store import (
    build_prompt_environment,
    build_python_block_source,
    compose_python_block,
    invalidate_prompt_cache,
    load_seed_rubric,
    python_block_sample_targets,
    python_block_slot_ids,
    validate_python_block_rubric,
)
from src.prompts.registry import PromptSpec, get_spec
from src.prompts.rubric_model import RubricExample
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)

_T = TypeVar("_T", bound=BaseModel)

_ALL_EXTRACTOR_TYPES = frozenset(
    {
        "ipv4",
        "ipv6",
        "domain",
        "url",
        "md5",
        "sha1",
        "sha256",
        "email",
        "cve",
        "mitre_tech",
        "mitre_group",
    }
)


def _spec() -> PromptSpec:
    spec = get_spec("ioc_llm_verifier")
    assert spec is not None
    return spec


def _mixed_targets(n: int) -> list[dict[str, str]]:
    """cve/ipv4/domain を巡回する n 件の候補 (chunk 境界検証用)。"""
    types = ("cve", "ipv4", "domain")
    out: list[dict[str, str]] = []
    for i in range(n):
        t = types[i % len(types)]
        if t == "cve":
            value = f"CVE-2026-{20000 + i}"
        elif t == "ipv4":
            value = f"203.0.113.{i % 256}"
        else:
            value = f"evil-{i}.kuebiko.example"
        out.append({"type": t, "value": value, "context": f"文脈 {i} (test)。"})
    return out


_EMPTY: list[dict[str, str]] = []
_SMALL_MIXED = _mixed_targets(3)
_CHUNK_BOUNDARY_40 = _mixed_targets(40)  # _VERIFY_CHUNK_SIZE と同数


class TestGolden:
    """核心不変量: seed の blocks で合成した結果が legacy (frozen) 実装と byte 一致。"""

    @pytest.mark.parametrize(
        "targets",
        [_EMPTY, _SMALL_MIXED, _CHUNK_BOUNDARY_40],
        ids=["empty", "small_mixed", "chunk_boundary_40"],
    )
    def test_seed_composition_is_byte_identical_to_legacy(
        self, targets: list[dict[str, str]]
    ) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        blocks = {s.field_id: s.body for s in rubric.sections}
        assert compose_from_blocks(blocks, targets) == v._build_prompt_legacy(targets)  # noqa: SLF001

    def test_seed_covers_every_slot(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        assert validate_python_block_rubric(spec, rubric) == []
        assert {s.field_id for s in rubric.sections} == set(SLOT_IDS)

    def test_sample_targets_cover_every_extractor_type(self) -> None:
        """保存前検証のサンプル候補が _flatten_with_context の全 type を網羅する。"""
        assert {t["type"] for t in SAMPLE_TARGETS} == _ALL_EXTRACTOR_TYPES
        assert all(t["value"].strip() and t["context"].strip() for t in SAMPLE_TARGETS)


class TestValidation:
    def test_missing_block_is_an_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(
            update={"sections": [s for s in rubric.sections if s.field_id != "criteria"]}
        )
        errors = validate_python_block_rubric(spec, broken)
        assert any("criteria" in e for e in errors)

    def test_unknown_block_is_an_error_not_silent(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        extra = rubric.sections[0].model_copy(update={"field_id": "no_such_slot"})
        broken = rubric.model_copy(update={"sections": [*rubric.sections, extra]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("no_such_slot" in e for e in errors)

    def test_duplicate_block_id_is_an_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        dup = rubric.sections[0]
        broken = rubric.model_copy(update={"sections": [*rubric.sections, dup]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("重複" in e for e in errors)

    def test_examples_are_rejected(self) -> None:
        """python_block 方式は few-shot examples を使わない (block 方式と同じ制約)。"""
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        broken = rubric.model_copy(update={"examples": [RubricExample(label="x", json_text="{}")]})
        errors = validate_python_block_rubric(spec, broken)
        assert any("examples" in e for e in errors)


class TestUnregisteredPromptId:
    """dispatch 表 (1 本目のみ) に無い prompt_id は fail-closed で扱われる。"""

    def _fake_spec(self) -> PromptSpec:
        real = _spec()
        return real.__class__(
            prompt_id="no_such_python_block_prompt",
            title="存在しない",
            config_key="no_such_python_block_prompt_rubric",
            env_flag="NO_SUCH_COMPOSER",
            seed_path=real.seed_path,
            legacy_path=real.legacy_path,
            kind="python_block",
            env_style="none",
            skeleton_path=None,
        )

    def test_slot_ids_and_sample_targets_are_empty(self) -> None:
        fake = self._fake_spec()
        assert python_block_slot_ids(fake) == ()
        assert python_block_sample_targets(fake) == []

    def test_compose_raises_value_error(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        fake = self._fake_spec()
        with pytest.raises(ValueError, match="合成関数が未登録"):
            compose_python_block(fake, rubric, [])

    def test_validate_reports_missing_slot_definition(self) -> None:
        spec = _spec()
        rubric = load_seed_rubric(spec)
        assert rubric is not None
        fake = self._fake_spec()
        errors = validate_python_block_rubric(fake, rubric)
        assert any("slot 定義が見つかりません" in e for e in errors)


class TestStoreFallback:
    def test_env_flag_zero_falls_back_to_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        monkeypatch.setenv(spec.env_flag, "0")
        try:
            assert build_python_block_source(spec, SAMPLE_TARGETS) is None
            assert v._build_prompt(SAMPLE_TARGETS) == v._build_prompt_legacy(SAMPLE_TARGETS)  # noqa: SLF001
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    def test_composer_enabled_by_default_produces_a_result(self) -> None:
        spec = _spec()
        invalidate_prompt_cache(spec.prompt_id)
        try:
            result = build_python_block_source(spec, SAMPLE_TARGETS)
            assert result is not None
            # seed = legacy byte 一致なので、実際に区別できるのは build が None でないことだけ。
            assert result == v._build_prompt_legacy(SAMPLE_TARGETS)  # noqa: SLF001
        finally:
            invalidate_prompt_cache(spec.prompt_id)

    def test_build_prompt_wrapper_matches_legacy_when_seed_untouched(self) -> None:
        """_build_prompt (公開エントリポイント) が seed=legacy byte 一致の下では区別なく動く。"""
        invalidate_prompt_cache("ioc_llm_verifier")
        try:
            assert v._build_prompt(SAMPLE_TARGETS) == v._build_prompt_legacy(SAMPLE_TARGETS)  # noqa: SLF001
        finally:
            invalidate_prompt_cache("ioc_llm_verifier")


class TestUIEditReflection:
    """UI 編集相当 (block 文字列の変更) が合成結果へ実際に反映されることの実証。"""

    def test_edited_block_changes_the_composed_prompt(self) -> None:
        spec = _spec()
        seed = load_seed_rubric(spec)
        assert seed is not None
        edited = seed.model_copy(
            update={
                "sections": [
                    s.model_copy(update={"body": "編集後の判定基準テキスト (test)"})
                    if s.field_id == "criteria"
                    else s
                    for s in seed.sections
                ]
            }
        )
        out = compose_python_block(spec, edited, [])
        assert "編集後の判定基準テキスト (test)" in out
        assert "KEEP: 攻撃で実際に観測" not in out
        # code 所有部分 (見出し) は編集の影響を受けない
        assert "## 判定基準" in out
        assert "## 候補リスト" in out


class TestEnvStyleAndEnvironmentGuard:
    def test_env_style_is_none(self) -> None:
        assert _spec().env_style == "none"

    def test_build_prompt_environment_rejects_none_style(self) -> None:
        spec = _spec()
        with pytest.raises(ValueError, match="env_style=none"):
            build_prompt_environment(spec, spec.legacy_path)


class _CapturingLLM(LLMClient):
    """全候補 keep を返し、実際に送信されたプロンプトを記録する fake。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def model(self) -> str:
        return "fake"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> _T:
        self.prompts.append(prompt)
        return cast(_T, IocVerifyOutput(decisions=[]))


class TestEndToEndWiring:
    """本番経路 (verify_iocs_with_llm) が層分けされた _build_prompt を実際に使うことの実証。"""

    async def test_verify_iocs_with_llm_uses_the_layered_prompt(self) -> None:
        extracted = ExtractedIocs(cves=("CVE-2026-99999",))
        llm = _CapturingLLM()
        await verify_iocs_with_llm(
            extracted, "本文中の CVE-2026-99999 (test)。", llm=llm, article_id="t"
        )
        assert llm.prompts, "LLM が呼ばれていません"
        prompt = llm.prompts[0]
        assert "## 判定基準" in prompt
        assert "KEEP: 攻撃で実際に観測" in prompt  # criteria block 由来
        assert "全候補 (上の番号 0〜N-1)" in prompt  # rules block 由来
        assert "value='CVE-2026-99999'" in prompt  # code 所有の候補リスト生成


class TestComposeFailureIsStructurallyGraceful:
    """合成経路がどこで raise しても legacy に落ちる (契約: prompt 組み立ては crash 点にしない)。

    下層 (build_python_block_source) が各所で例外を握る事実に依存せず、
    _build_prompt 自体の関門で保証されることを monkeypatch で固定する。
    """

    def test_composed_path_raising_falls_back_to_legacy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange: 合成経路全体を例外で置き換える
        def _boom(targets: list[dict[str, str]]) -> str | None:
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(v, "_composed_prompt", _boom)
        targets = _mixed_targets(3)

        # Act
        prompt = v._build_prompt(targets)

        # Assert: legacy 出力と byte 一致 (縮退の内容まで正しい)
        assert prompt == v._build_prompt_legacy(targets)

    async def test_verify_iocs_survives_compose_failure_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        def _boom(targets: list[dict[str, str]]) -> str | None:
            raise RuntimeError("store unavailable")

        monkeypatch.setattr(v, "_composed_prompt", _boom)
        extracted = ExtractedIocs(cves=("CVE-2026-99999",))
        llm = _CapturingLLM()

        # Act
        result = await verify_iocs_with_llm(
            extracted, "本文中の CVE-2026-99999 (test)。", llm=llm, article_id="t"
        )

        # Assert: pipeline へ例外が漏れず、legacy プロンプトで verify が続行される
        assert result.cves == ("CVE-2026-99999",)
        assert llm.prompts and "## 判定基準" in llm.prompts[0]
