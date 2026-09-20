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


def test_detect_schema_caps_every_array_so_the_grammar_can_close() -> None:
    """配列に上限が無いと Gemma 4 は閉じられない (2026-09-20、ollama#15502)。

    本番実測では直近 14 日で出力上限到達が 13 回あり、replay では 3 腕すべてで
    「開設ゼロ」が**判断ではなく途中切れ**だった。Ollama は maxItems を文法へ
    コンパイルするので、上限を宣言しておけばモデルが続けたくても閉じる。
    """
    from src.synthesis.grounded.incremental import _DETECT_OPEN_MAX, _WireDetectResult

    schema = _WireDetectResult.model_json_schema()
    props = schema["properties"]

    assert props["open"]["maxItems"] >= _DETECT_OPEN_MAX  # 下流の overflow 判定を潰さない
    assert props["rejected"]["maxItems"] > 0
    claim = schema["$defs"]["_WireOpenClaim"]["properties"]
    assert claim["article_ids"]["maxItems"] >= 7  # 同一事象の正当な群化 (実測最大 7) を通す
