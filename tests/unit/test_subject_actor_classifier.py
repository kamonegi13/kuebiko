"""主題 focused 分類器 (subject_actor_classifier) のテスト。

候補外を返さない構造ゲート (誤帰属防止の核心) と、安全側フォールバックを検証する。
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from src.cti.actor_normalizer import ActorAlias
from src.cti.subject_actor_classifier import _SubjectOut, classify_subject_actor
from src.tools.llm_client import LLMClient


def _llm(subject_id: str, confidence: str = "high") -> LLMClient:
    llm = AsyncMock()
    llm.generate_structured.return_value = _SubjectOut(
        subject_actor_id=subject_id, confidence=confidence
    )
    return cast(LLMClient, llm)


_CANDS = [
    ActorAlias(id="qilin", canonical="Qilin"),
    ActorAlias(id="apt29", canonical="APT29"),
]


async def test_returns_candidate_subject() -> None:
    pid, conf = await classify_subject_actor(
        _llm("qilin", "high"), title="t", body="body about qilin", candidates=_CANDS
    )
    assert pid == "qilin"
    assert conf == "high"


async def test_out_of_candidate_is_rejected() -> None:
    # 候補外 (幻覚) は構造ゲートで棄却 → 主題なし
    pid, conf = await classify_subject_actor(
        _llm("lazarus", "high"), title="t", body="b", candidates=_CANDS
    )
    assert pid == ""
    assert conf == "low"


async def test_empty_subject_is_none() -> None:
    pid, _ = await classify_subject_actor(_llm("", "low"), title="t", body="b", candidates=_CANDS)
    assert pid == ""


async def test_no_candidates_skips_llm() -> None:
    # 候補が空なら LLM を呼ばず即 ('', 'low')
    llm = AsyncMock()
    pid, conf = await classify_subject_actor(
        cast(LLMClient, llm), title="t", body="b", candidates=[]
    )
    assert (pid, conf) == ("", "low")
    llm.generate_structured.assert_not_called()


async def test_empty_body_skips() -> None:
    pid, _ = await classify_subject_actor(_llm("qilin"), title="t", body="", candidates=_CANDS)
    assert pid == ""


async def test_llm_failure_is_safe() -> None:
    llm = AsyncMock()
    llm.generate_structured.side_effect = RuntimeError("boom")
    pid, conf = await classify_subject_actor(
        cast(LLMClient, llm), title="t", body="b", candidates=_CANDS
    )
    assert (pid, conf) == ("", "low")


async def test_invalid_confidence_normalized() -> None:
    pid, conf = await classify_subject_actor(
        _llm("qilin", "garbage"), title="t", body="b", candidates=_CANDS
    )
    assert pid == "qilin"
    assert conf == "low"
