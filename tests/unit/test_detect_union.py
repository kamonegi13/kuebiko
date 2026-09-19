"""detect の和集合 (LLM の選定 + ML の上位) — 2026-09-19、SYNTHESIS §51。

replay 5 日の実測: LLM のみ 回収 8 / ML のみ 10 / 和集合 17、精度は 53→55% で落ちない。
両腕が共通して開いた記事は 31 件中 1 件 = 視点が直交しており、和集合のコストは足し算になる。
"""

from __future__ import annotations

import pytest

from src.synthesis.grounded.detect_ml import union_additions, union_top_k
from src.synthesis.grounded.incremental import (
    DetectedClaim,
    DetectResult,
    merge_union_claims,
)


def _res(
    *claims: tuple[str, tuple[str, ...]],
    rejected: tuple[tuple[str, str], ...] = (),
    overflow: int = 0,
) -> DetectResult:
    return DetectResult(
        open=tuple(DetectedClaim(claim=c, domain="cyber", article_ids=ids) for c, ids in claims),
        rejected=rejected,
        overflow=overflow,
    )


def test_union_additions_skips_opened_and_rollups_then_takes_top_k() -> None:
    scores = {"a": 0.99, "roll": 0.98, "b": 0.9, "c": 0.8, "d": 0.7}

    got = union_additions(scores, top_k=2, already_opened={"a"}, excluded={"roll"})

    assert got == ["b", "c"]  # 既開設と勧告を除いてから上位 2 件
    assert union_additions(scores, top_k=0, already_opened=set(), excluded=set()) == []


def test_merge_drops_claims_that_reuse_an_already_opened_article() -> None:
    base = _res(("LLM の claim", ("a1", "a2")))
    extra = _res(("同じ記事", ("a2",)), ("別の記事", ("b1",)))

    merged = merge_union_claims(base, extra, cap=12)

    assert [c.claim for c in merged.open] == ["LLM の claim", "別の記事"]
    assert merged.overflow == 0


def test_merge_respects_cap_and_counts_overflow() -> None:
    base = _res(*[(f"c{i}", (f"a{i}",)) for i in range(3)])
    extra = _res(*[(f"m{i}", (f"b{i}",)) for i in range(4)])

    merged = merge_union_claims(base, extra, cap=5)

    assert len(merged.open) == 5  # base 3 + extra 2
    assert merged.overflow == 2  # 落とした 2 件は黙らせない


def test_merge_concatenates_rejected_for_the_audit_log() -> None:
    base = _res(("x", ("a",)), rejected=(("r1", "既知"),))
    extra = _res(("y", ("b",)), rejected=(("r2", "主張なし"),))

    merged = merge_union_claims(base, extra, cap=12)

    assert merged.rejected == (("r1", "既知"), ("r2", "主張なし"))


def test_union_top_k_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DETECT_ML_UNION", raising=False)
    assert union_top_k() == 4
    monkeypatch.setenv("DETECT_ML_UNION", "0")
    assert union_top_k() == 0
    monkeypatch.setenv("DETECT_ML_UNION", "zzz")
    assert union_top_k() == 4
