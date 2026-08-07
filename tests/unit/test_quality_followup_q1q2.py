"""Q1 (Spotlight MITRE 接地) / Q2 (IoC verifier 全件 chunk 判定) の unit test。

- Q1: RunHistoryRepository.entities_for_articles が候補記事の TTP を引ける
- Q2: verify_iocs_with_llm が chunk size 超の候補も全件判定する (旧: 超過分は未検証 keep)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from src.cti.ioc_extractor import ExtractedIocs
from src.cti.ioc_llm_verifier import IocDecision, IocVerifyOutput, verify_iocs_with_llm
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)

_T = TypeVar("_T", bound=BaseModel)


# ───────── Q1: entities_for_articles ─────────
def test_entities_for_articles_returns_ttp_map(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "ent.db")
    repo.add_article_entities("artA", [("ttp", "T1566"), ("ttp", "T1059"), ("actor", "apt28")])
    repo.add_article_entities("artB", [("ttp", "T1486")])
    out = repo.entities_for_articles(["artA", "artB", "artC"], entity_type="ttp")
    assert set(out["artA"]) == {"T1566", "T1059"}
    assert out["artB"] == ["T1486"]
    assert "artC" not in out  # entity なし
    # actor は ttp query に混ざらない
    assert all("apt28" not in v for v in out.values())


def test_entities_for_articles_empty_input(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "ent2.db")
    assert repo.entities_for_articles([], entity_type="ttp") == {}


# ───────── Q2: verifier 全件 chunk 判定 ─────────
class _DropLocalZeroLLM(LLMClient):
    @property
    def model(self) -> str:  # LLMClient 抽象契約 (監査 P6 で model を必須化)
        return "fake"

    """各 chunk の local index 0 を drop する fake (chunk ごとに呼ばれる)。"""

    call_count = 0

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
        self.call_count += 1
        # fake は常に IocVerifyOutput を返す (schema は IocVerifyOutput 固定の前提)
        return cast(
            _T,
            IocVerifyOutput(decisions=[IocDecision(id=0, action="drop", reason="test")]),
        )


async def test_verifier_chunks_all_candidates(monkeypatch: Any) -> None:
    # chunk size を 40 から 5 に下げ、12 候補 (= 3 chunk) を作る
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 5)
    domains = tuple(f"d{i:02d}.example.com" for i in range(12))
    body = " ".join(domains)  # context 抽出用 (値が本文に出現)
    extracted = ExtractedIocs(domains=domains)

    llm = _DropLocalZeroLLM()
    result = await verify_iocs_with_llm(extracted, body, llm=llm, article_id="t")

    # 3 chunk (5+5+2) 全てが LLM 判定された
    assert llm.call_count == 3
    # 各 chunk の local index 0 (= global 0, 5, 10) が drop され残り 9 件
    survivors = set(result.domains)
    assert "d00.example.com" not in survivors  # chunk1 local0
    assert "d05.example.com" not in survivors  # chunk2 local0 (旧実装なら未検証 keep されていた)
    assert "d10.example.com" not in survivors  # chunk3 local0
    assert len(survivors) == 9


async def test_verifier_failure_keeps_batch(monkeypatch: Any) -> None:
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 5)

    class _FailLLM(LLMClient):
        @property
        def model(self) -> str:  # LLMClient 抽象契約 (監査 P6 で model を必須化)
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
            raise RuntimeError("llm down")

    domains = ("a.example.com", "b.example.com")
    extracted = ExtractedIocs(domains=domains)
    result = await verify_iocs_with_llm(
        extracted, " ".join(domains), llm=_FailLLM(), article_id="t"
    )
    # LLM 失敗時はその batch を全 keep (graceful degradation)
    assert set(result.domains) == set(domains)
