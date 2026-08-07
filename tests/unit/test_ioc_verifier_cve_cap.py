"""IoC verifier の CVE 候補 cap (2026-08-01) の unit test。

CERT-FR 等の patch-roundup advisory は CVE 参照リストを数百件含み、全件 chunk verify で
1 記事 10+ LLM 呼出 (実測 6-8 分) になり run 全体の 1800s timeout を誘発した
(run 3520)。cap 超過分の CVE は LLM を呼ばず決定論的に drop する。
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from pydantic import BaseModel

from src.cti.ioc_extractor import ExtractedIocs
from src.cti.ioc_llm_verifier import IocVerifyOutput, verify_iocs_with_llm
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMResponse,
)

_T = TypeVar("_T", bound=BaseModel)


class _KeepAllLLM(LLMClient):
    """全候補 keep (decisions 空) を返し、呼出回数と prompt を記録する fake。"""

    def __init__(self) -> None:
        self.call_count = 0
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
        self.call_count += 1
        self.prompts.append(prompt)
        return cast(_T, IocVerifyOutput(decisions=[]))


def _cves(n: int) -> tuple[str, ...]:
    return tuple(f"CVE-2026-{10000 + i}" for i in range(n))


async def test_cve_overflow_dropped_without_llm(monkeypatch: Any) -> None:
    """cap 超過の CVE は LLM verify に載らず結果からも drop される。"""
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 5)
    monkeypatch.setattr(v, "_CVE_VERIFY_CAP_DEFAULT", 8)

    cves = _cves(20)
    domains = ("evil-a.example.com", "evil-b.example.com", "evil-c.example.com")
    body = " ".join(cves) + " " + " ".join(domains)
    extracted = ExtractedIocs(cves=cves, domains=domains)

    llm = _KeepAllLLM()
    result = await verify_iocs_with_llm(extracted, body, llm=llm, article_id="t")

    # 候補 = CVE 先頭 8 + domain 3 = 11 件 → chunk 5 で 3 呼出 (20 CVE 全件なら 5 呼出)
    assert llm.call_count == 3
    # cap 内の CVE は keep、超過分は決定論 drop
    assert set(result.cves) == set(cves[:8])
    # CVE 以外の型は cap の影響を受けない
    assert set(result.domains) == set(domains)
    # 超過 CVE が LLM の候補行 (value=...) として一切載っていない
    # (context 文字列への写り込みは判定対象外なので candidate 行のみ見る)
    joined = "\n".join(llm.prompts)
    assert "value='CVE-2026-10019'" not in joined
    assert "value='CVE-2026-10007'" in joined  # cap 内は候補に載る


async def test_cve_within_cap_all_verified(monkeypatch: Any) -> None:
    """cap 以下なら従来どおり全件 LLM verify (挙動不変)。"""
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 5)
    monkeypatch.setattr(v, "_CVE_VERIFY_CAP_DEFAULT", 8)

    cves = _cves(6)
    extracted = ExtractedIocs(cves=cves)
    llm = _KeepAllLLM()
    result = await verify_iocs_with_llm(extracted, " ".join(cves), llm=llm, article_id="t")

    assert set(result.cves) == set(cves)
    assert llm.call_count == 2  # 6 候補 / chunk 5 = 2 呼出


async def test_cve_cap_env_override(monkeypatch: Any) -> None:
    """env IOC_CVE_VERIFY_CAP で cap を上書きできる。"""
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 10)
    monkeypatch.setenv("IOC_CVE_VERIFY_CAP", "3")

    cves = _cves(10)
    extracted = ExtractedIocs(cves=cves)
    llm = _KeepAllLLM()
    result = await verify_iocs_with_llm(extracted, " ".join(cves), llm=llm, article_id="t")

    assert set(result.cves) == set(cves[:3])
    assert llm.call_count == 1


async def test_cve_cap_env_invalid_falls_back(monkeypatch: Any) -> None:
    """env が不正値なら既定 cap にフォールバックする。"""
    import src.cti.ioc_llm_verifier as v

    monkeypatch.setattr(v, "_VERIFY_CHUNK_SIZE", 10)
    monkeypatch.setattr(v, "_CVE_VERIFY_CAP_DEFAULT", 4)
    monkeypatch.setenv("IOC_CVE_VERIFY_CAP", "not-a-number")

    cves = _cves(6)
    extracted = ExtractedIocs(cves=cves)
    llm = _KeepAllLLM()
    result = await verify_iocs_with_llm(extracted, " ".join(cves), llm=llm, article_id="t")

    assert set(result.cves) == set(cves[:4])
