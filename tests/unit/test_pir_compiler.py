"""B2: PIR compiler の派生 signal sanitize テスト。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.pir.compiler import (
    _CompiledSignals,
    _CompilerOutput,
    _MatchBranch,
    _MatchCondition,
    compile_pir,
)


def _llm_returning(output: _CompilerOutput) -> AsyncMock:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(return_value=output)
    return llm


@pytest.mark.asyncio
async def test_compile_keeps_known_signals_drops_unknown(tmp_path: Path) -> None:
    out = _CompilerOutput(
        strong_signals=_CompiledSignals(keywords=["x"], signals=["kev", "bogus", "zero_day"]),
    )
    result = await compile_pir(
        pir_id="pir_test",
        title="t",
        description="d",
        llm=_llm_returning(out),
        config_dir=tmp_path,  # 空 dir → vocab fallback、signal sanitize は独立に動く
    )
    assert result.pir.strong_signals.signals == ["kev", "zero_day"]
    assert any("bogus" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_compile_empty_signals_ok(tmp_path: Path) -> None:
    out = _CompilerOutput(strong_signals=_CompiledSignals(keywords=["ransomware"]))
    result = await compile_pir(
        pir_id="pir_test",
        title="t",
        description="d",
        llm=_llm_returning(out),
        config_dir=tmp_path,
    )
    assert result.pir.strong_signals.signals == []


# ---- 照合条件 (DNF → ツリー、authoring 統一 2026-07-23) ----


def _branch(*conds: dict[str, object]) -> _MatchBranch:
    return _MatchBranch(conditions=[_MatchCondition.model_validate(c) for c in conds])


def test_dnf_single_condition_collapses_to_leaf() -> None:
    from src.pir.compiler import dnf_to_tree

    tree, warnings = dnf_to_tree(
        [_branch({"property": "category", "op": "in", "value": ["vulnerability"]})]
    )
    assert tree == {"property": "category", "op": "eq", "value": "vulnerability"}
    assert warnings == []


def test_dnf_and_or_shape_with_negate() -> None:
    from src.pir.compiler import dnf_to_tree

    tree, _ = dnf_to_tree(
        [
            _branch(
                {"property": "victim_country", "op": "in", "value": ["jp"]},
                {"property": "category", "op": "in", "value": ["geopolitical"], "negate": True},
            ),
            _branch({"property": "title", "op": "keyword_any", "value": ["日本", "日系"]}),
        ]
    )
    assert tree == {
        "any": [
            {
                "all": [
                    {"property": "victim_country", "op": "eq", "value": "jp"},
                    {"not": {"property": "category", "op": "eq", "value": "geopolitical"}},
                ]
            },
            {"property": "title", "op": "keyword_any", "value": ["日本", "日系"]},
        ]
    }


def test_dnf_drops_invalid_conditions_with_warning() -> None:
    from src.pir.compiler import dnf_to_tree

    tree, warnings = dnf_to_tree(
        [
            _branch(
                {"property": "bogus_prop", "op": "eq", "value": ["x"]},
                {"property": "is_ransomware", "op": "is_true"},
            )
        ]
    )
    assert tree == {"property": "is_ransomware", "op": "is_true"}
    assert any("bogus_prop" in w for w in warnings)


def test_dnf_op_recovers_from_single_allowed_op() -> None:
    """LLM が op を誤っても許可 op が 1 種なら回復する (keyword_any 等)。"""
    from src.pir.compiler import dnf_to_tree

    tree, _ = dnf_to_tree([_branch({"property": "text", "op": "in", "value": ["log4j"]})])
    assert tree == {"property": "text", "op": "keyword_any", "value": ["log4j"]}


def test_dnf_empty_returns_none() -> None:
    from src.pir.compiler import dnf_to_tree

    tree, _ = dnf_to_tree([])
    assert tree is None


@pytest.mark.asyncio
async def test_compile_builds_match_and_llm_judge(tmp_path: Path) -> None:
    out = _CompilerOutput(
        strong_signals=_CompiledSignals(keywords=["leak"]),
        match_branches=[
            _MatchBranch(
                conditions=[
                    _MatchCondition(property="text", op="keyword_any", value=["i-Soon", "leak"])
                ]
            )
        ],
        needs_subject_judge=True,
        judge_question="APT 実態の暴露が主題か",
    )
    result = await compile_pir(
        pir_id="pir_t", title="t", description="d", llm=_llm_returning(out), config_dir=tmp_path
    )
    assert result.pir.match == {
        "property": "text",
        "op": "keyword_any",
        "value": ["i-Soon", "leak"],
    }
    assert result.pir.llm_judge.enabled is True
    assert result.pir.llm_judge.question == "APT 実態の暴露が主題か"


@pytest.mark.asyncio
async def test_compile_warns_on_keyword_only_without_judge(tmp_path: Path) -> None:
    out = _CompilerOutput(
        match_branches=[
            _MatchBranch(
                conditions=[_MatchCondition(property="text", op="keyword_any", value=["漏洩"])]
            )
        ],
    )
    result = await compile_pir(
        pir_id="pir_t", title="t", description="d", llm=_llm_returning(out), config_dir=tmp_path
    )
    assert result.pir.llm_judge.enabled is False
    assert any("キーワードのみ" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_compile_without_branches_keeps_match_none(tmp_path: Path) -> None:
    """ツリーを作れなかったら match=None (フロントが既存を保全し silent 退行しない)。"""
    out = _CompilerOutput(strong_signals=_CompiledSignals(keywords=["x"]))
    result = await compile_pir(
        pir_id="pir_t", title="t", description="d", llm=_llm_returning(out), config_dir=tmp_path
    )
    assert result.pir.match is None
    assert result.pir.llm_judge.enabled is False
    assert any("照合条件を生成できません" in w for w in result.warnings)
