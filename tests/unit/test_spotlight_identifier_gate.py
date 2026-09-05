"""spotlight 識別子関門のテスト (2026-09-06、narrative の v1 移行条件)。

背景: SFT v1 の spotlight 実測で固有名詞の低頻度破損 ("Xinjiuwei"→"Xinjirazu"、
".hl.cn"→".hl.conn") を確認。事象ニュースには識別子関門があるが spotlight には
無かった。事象ニュースと同じ対称照合 (extract_identifiers を両側に適用) を
プロンプト実文字列を参照側として適用する。

3 段で固定する (charset 修正 2026-09-05 と同じ構図):
1. 判定器そのもの (対称照合が破損識別子を拾い、接地済みは拾わないこと)
2. **配線** — generate_spotlight が関門を通し、不支持識別子があれば具体値を
   示して 1 回だけ書き直させること (旗 SPOTLIGHT_IDENTIFIER_GATE=0 で無効化)
3. 再試行後も残る場合は保存を止めず記録する (週次成果物の可用性を優先)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from src.spotlight.identifier_check import (
    find_unsupported_identifiers,
    render_identifier_feedback,
)

_REFERENCE = (
    "Silver Fox は偽ドメイン (fake-download[.]hl.cn) を使い ValleyRAT を配布した。"
    "RingCentral を装う CVE-2026-12345 の悪用も観測。詳細は Nanjing Xinjiuwei 社の関与。"
)


class TestFindUnsupportedIdentifiers:
    def test_grounded_identifiers_pass(self) -> None:
        generated = "CVE-2026-12345 を悪用し、hl.cn 配下のドメインで ValleyRAT を配布。"
        assert find_unsupported_identifiers(generated, _REFERENCE) == ()

    def test_fabricated_cve_flagged(self) -> None:
        generated = "CVE-2026-99999 の悪用が続く。"
        flagged = find_unsupported_identifiers(generated, _REFERENCE)
        assert [i.raw for i in flagged] == ["CVE-2026-99999"]

    def test_corrupted_proper_noun_flagged(self) -> None:
        # 語中大文字型の破損 (RingCentral → RingCRntal) は対称照合で拾える
        generated = "RingCRntal を装う攻撃。"
        flagged = find_unsupported_identifiers(generated, _REFERENCE)
        assert [i.raw for i in flagged] == ["RingCRntal"]

    def test_short_acronyms_not_flagged(self) -> None:
        # EDR / C2 / IoC 等の短い一般略語は参照不在でも拾わない (誤検出抑制)
        generated = "EDR と IoC の監視、C2 通信の遮断を推奨する。"
        assert find_unsupported_identifiers(generated, _REFERENCE) == ()

    def test_defanged_reference_matches_refanged_output(self) -> None:
        # 参照側が defang 表記でも正規化で一致する
        generated = "fake-download.hl.cn への通信を監視。"
        assert find_unsupported_identifiers(generated, _REFERENCE) == ()

    def test_empty_generated_is_empty(self) -> None:
        assert find_unsupported_identifiers("", _REFERENCE) == ()


class TestRenderFeedback:
    def test_lists_offending_values(self) -> None:
        flagged = find_unsupported_identifiers("CVE-2026-99999 と EvilCorpX の活動。", _REFERENCE)
        text = render_identifier_feedback(flagged)
        assert "CVE-2026-99999" in text
        assert "EvilCorpX" in text


# ---------- 配線 (generate_spotlight が関門を通すこと) ----------


@dataclass
class _FakeMatch:
    article_id: str
    title: str = "t"
    url: str = "https://example.com/a"
    feed_title: str = "feed"
    importance: str = "high"
    created_at: str = "2026-09-01T00:00:00+00:00"
    summary: str = "CVE-2026-12345 の悪用。"
    feed_url: str = ""


class _FakeLLM:
    """1 回目は捏造 CVE 入り、2 回目は接地済みの出力を返す fake。"""

    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate_structured(self, prompt: str, *, schema: Any, **_kw: Any) -> Any:
        self.prompts.append(prompt)
        outlook = (
            "CVE-2026-99999 が悪用されている。" if len(self.prompts) == 1 else "悪用が続いている。"
        )
        return schema(
            headline="CVE-2026-12345 の悪用",
            key_events=[{"index": i + 1} for i in range(5)],
            outlook=outlook,
        )


@dataclass
class _FakePir:
    id: str = "pir_test"
    title: str = "test"
    enabled: bool = True
    spotlight: Any = field(default_factory=lambda: type("S", (), {"enabled": True})())


@pytest.fixture()
def _patched_generator(monkeypatch: pytest.MonkeyPatch) -> _FakeLLM:
    import src.spotlight.generator as gen

    matches = [_FakeMatch(article_id=f"a{i}") for i in range(6)]
    monkeypatch.setattr(gen, "evaluate_pir_matches", lambda *a, **k: matches)
    monkeypatch.setattr(gen, "_build_prompt", lambda *a, **k: "PROMPT: CVE-2026-12345 のみ許可")
    monkeypatch.setattr(gen, "_load_previous_spotlight", lambda **k: None)
    monkeypatch.setattr(gen, "_build_ledger_context", lambda *a, **k: [])

    class _FakeRepo:
        def entities_for_articles(self, *a: Any, **k: Any) -> dict[str, list[str]]:
            return {}

    monkeypatch.setattr(gen, "RunHistoryRepository", _FakeRepo)
    return _FakeLLM()


@pytest.mark.asyncio
async def test_generate_spotlight_retries_on_unsupported_identifier(
    _patched_generator: _FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.spotlight.generator import generate_spotlight

    monkeypatch.delenv("SPOTLIGHT_IDENTIFIER_GATE", raising=False)
    llm = _patched_generator
    record = await generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]  # Pir 相当の最小 stub
        llm=llm,  # type: ignore[arg-type]
        assessment=object(),  # type: ignore[arg-type]  # _build_prompt patch 済で未使用
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert record is not None
    # 1 回目の捏造 CVE を検知して具体値つきで 1 回だけ書き直させる
    assert len(llm.prompts) == 2
    assert "CVE-2026-99999" in llm.prompts[1]
    assert "CVE-2026-99999" not in record.outlook


@pytest.mark.asyncio
async def test_generate_spotlight_gate_disabled_by_flag(
    _patched_generator: _FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.spotlight.generator import generate_spotlight

    monkeypatch.setenv("SPOTLIGHT_IDENTIFIER_GATE", "0")
    llm = _patched_generator
    record = await generate_spotlight(
        _FakePir(),  # type: ignore[arg-type]  # Pir 相当の最小 stub
        llm=llm,  # type: ignore[arg-type]
        assessment=object(),  # type: ignore[arg-type]  # _build_prompt patch 済で未使用
        now=datetime(2026, 9, 6, tzinfo=UTC),
    )
    assert record is not None
    assert len(llm.prompts) == 1  # rollback: 従来挙動 (検査なし)
