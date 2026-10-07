"""TRIAGE_FLAT (s23 の平たい triage prompt) のテスト (2026-10-08)。

docs/importance_relevance_redesign.md §6 の 2026-10-08 利用者決定: s23 の triage は
関連性 (日本・SIR・注視国) を外し、平たい深刻さの見込みだけを学習する。既定 (env 未設定 / 0)
では現行挙動 (PIR_DRIVEN_TRIAGE 分岐) を一切変えないことを固定する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TypeVar

import pytest
from pydantic import BaseModel

from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage
from src.tools.llm_client import LLMClient, LLMResponse
from src.tools.triage_flat_rubric import RUBRIC_VERSION, build_flat_triage_prompt

_T = TypeVar("_T", bound=BaseModel)


def _article(title: str = "重大インフラへの不正アクセス事案") -> Article:
    return Article(
        id="t:1",
        title=title,
        url="https://example.com/x",
        summary_html="<p>テスト概要</p>",
        author=None,
        published=datetime.now(UTC),
        feed_title="Test Feed",
        feed_url="https://example.com/feed",
    )


class _UnusedLLM(LLMClient):
    """本テストは prompt 組み立てのみ検証するため呼ばれない。"""

    @property
    def model(self) -> str:
        return "unused"

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 0,
        think: bool | None = None,
    ) -> LLMResponse:
        raise AssertionError("呼ばれない想定")

    async def generate_structured(
        self,
        prompt: str,
        schema: type[_T],
        system: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 0,
        think: bool | None = None,
        max_attempts: int = 1,
    ) -> _T:
        raise AssertionError("呼ばれない想定")


class TestFlagOffKeepsCurrentBehaviour:
    """TRIAGE_FLAT 未設定 / 0 なら既存の _build_prompt 分岐だけが効く (挙動不変)。"""

    def test_env_unset_uses_existing_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TRIAGE_FLAT", raising=False)
        monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "0")  # legacy hardcoded 分岐に固定
        triage = ArticleTriage(_UnusedLLM())
        prompt = triage._build_prompt(_article())  # noqa: SLF001
        assert prompt == triage._build_prompt_legacy_hardcoded(_article())  # noqa: SLF001
        assert "平たい" not in prompt
        assert RUBRIC_VERSION not in prompt

    def test_env_explicit_zero_uses_existing_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRIAGE_FLAT", "0")
        monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "0")
        triage = ArticleTriage(_UnusedLLM())
        prompt = triage._build_prompt(_article())  # noqa: SLF001
        assert prompt == triage._build_prompt_legacy_hardcoded(_article())  # noqa: SLF001


class TestFlagOnUsesFlatRubric:
    def test_flat_prompt_has_no_relevance_boosting_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TRIAGE_FLAT", "1")
        triage = ArticleTriage(_UnusedLLM())
        prompt = triage._build_prompt(_article())  # noqa: SLF001

        # 関連性 (PIR/SIR/日本優先・注視国) を煽る文言が一切無いこと。
        for forbidden in (
            "SIR",
            "PIR",
            "日本標的",
            "日本企業",
            "日本国内組織",
            "注視国",
            "最優先で検知",
            "特に日本対象なら最優先",
        ):
            assert forbidden not in prompt, forbidden

        assert RUBRIC_VERSION in prompt
        assert prompt == build_flat_triage_prompt(
            feed="Test Feed",
            title="重大インフラへの不正アクセス事案",
            body_preview="テスト概要",
        )

    def test_flat_prompt_contains_title_and_feed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TRIAGE_FLAT", "1")
        triage = ArticleTriage(_UnusedLLM())
        article = _article()
        prompt = triage._build_prompt(article)  # noqa: SLF001
        assert article.title in prompt
        assert article.feed_title in prompt

    def test_flat_prompt_never_promises_high_for_non_cyber(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非サイバーの軍事・地政学・外交は high にしない不変条件 (09-29 利用者決定) を保持。"""
        monkeypatch.setenv("TRIAGE_FLAT", "1")
        triage = ArticleTriage(_UnusedLLM())
        prompt = triage._build_prompt(_article())  # noqa: SLF001
        assert "サイバー要素のない軍事・外交・宇宙の" in prompt

    def test_flag_truthy_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        triage = ArticleTriage(_UnusedLLM())
        for value in ("1", "true", "yes", "on"):
            monkeypatch.setenv("TRIAGE_FLAT", value)
            prompt = triage._build_prompt(_article())  # noqa: SLF001
            assert RUBRIC_VERSION in prompt


def test_build_flat_triage_prompt_is_deterministic() -> None:
    a = build_flat_triage_prompt(feed="f", title="t", body_preview="b")
    b = build_flat_triage_prompt(feed="f", title="t", body_preview="b")
    assert a == b
