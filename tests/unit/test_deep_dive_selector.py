"""src.digest.deep_dive_selector のテスト (Phase 5T-T2)。

LLM rubric scoring + composite + selection の振る舞いを検証する。
LLMClient は AsyncMock で置換し、ネットワーク I/O は発生させない。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.digest.db_filter import DigestCandidate
from src.digest.deep_dive_selector import (
    DEFAULT_WEIGHTS,
    ScoredArticle,
    _compute_composite,
    _render_prompt,
    select_deep_dive_articles,
)
from src.tools.llm_client import LLMClient


def _make_candidate(article_id: str, **overrides: object) -> DigestCandidate:
    base: dict[str, object] = {
        "article_id": article_id,
        "title": f"Title {article_id}",
        "url": f"https://example.com/{article_id}",
        "feed_title": "Feed",
        "importance": "medium",
        "category": "apt",
        "posted_channel": "watch",
        "created_at": "2026-05-18T00:00:00+00:00",
        "summary": "x" * 300,
        "dedup_key": None,
    }
    base.update(overrides)
    return DigestCandidate(**base)  # type: ignore[arg-type]


def _structured(text: str) -> Any:
    """採点は構造化出力になった (2026-09-21)。JSON 文字列から wire モデルを組む。

    テストの意図 (どんな採点が返ったとき何が起きるか) は変えず、**呼出の形だけ**
    本番に合わせる。自由文のままだと本番と違う経路を測ることになる。
    """
    from src.digest.deep_dive_selector import _WireRubricOutput

    return _WireRubricOutput.model_validate(json.loads(text))


class TestCompositeWeighting:
    def test_default_weights_sum_to_one(self) -> None:
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_compute_composite_matches_formula(self) -> None:
        scores = {"pir": 5.0, "roi": 4.0, "timeliness": 3.0, "novelty": 2.0}
        expected = 5 * 0.4 + 4 * 0.3 + 3 * 0.2 + 2 * 0.1
        assert _compute_composite(scores) == pytest.approx(expected)


@pytest.mark.asyncio
class TestSelectFlow:
    async def test_empty_candidates_returns_empty(self) -> None:
        llm = AsyncMock(spec=LLMClient)
        result = await select_deep_dive_articles(llm=llm, candidates=[])
        assert result == []
        llm.generate.assert_not_awaited()

    async def test_selects_top_by_composite(self) -> None:
        cands = [_make_candidate("A"), _make_candidate("B"), _make_candidate("C")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=_structured(
                """
                {"scored_articles": [
                  {"no":1,"scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"top"},
                  {"no":2,"scores":{"pir":3,"roi":3,"timeliness":3,"novelty":3},"rationale":"mid"},
                  {"no":3,"scores":{"pir":1,"roi":1,"timeliness":1,"novelty":1},"rationale":"low"}
                ]}
                """,
            ),
        )
        result = await select_deep_dive_articles(
            llm=llm,
            candidates=cands,
            composite_threshold=2.5,
            max_select=5,
        )
        ids = [s.candidate.article_id for s in result]
        # C は composite=1.0 で閾値未満
        assert ids == ["A", "B"]

    async def test_zero_articles_when_all_below_threshold(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=_structured(
                '{"scored_articles": [{"no":1,"scores":{"pir":1,"roi":1,"timeliness":1,"novelty":1},"rationale":"low"}]}',  # noqa: E501
            ),
        )
        result = await select_deep_dive_articles(
            llm=llm,
            candidates=cands,
            composite_threshold=2.5,
        )
        assert result == []

    async def test_chunk_all_scores_every_candidate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # chunk-all: 全候補を bounded なバッチに分けて採点し、統合する (1件も落とさない)。
        import src.digest.deep_dive_selector as sel

        monkeypatch.setattr(sel, "RUBRIC_CHUNK_SIZE", 2)  # 5 候補 → 3 チャンク
        cands = [_make_candidate(f"Z{i}") for i in range(5)]
        ids = [c.article_id for c in cands]

        def _score_chunk(**kwargs: object) -> Any:
            prompt = str(kwargs.get("prompt", ""))
            present = [i for i in ids if i in prompt]
            entries = ",".join(
                f'{{"no":{present.index(i) + 1},'
                f'"scores":{{"pir":5,"roi":5,"timeliness":5,"novelty":5}},"rationale":"r"}}'
                for i in present
            )
            return _structured(f'{{"scored_articles":[{entries}]}}')

        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(side_effect=_score_chunk)
        result = await select_deep_dive_articles(
            llm=llm, candidates=cands, composite_threshold=2.5, max_select=12
        )
        assert llm.generate_structured.await_count == 3  # 3 チャンクに分割して呼ばれる
        scored_ids = {s.candidate.article_id for s in result}
        assert scored_ids == set(ids)  # 全 5 件が採点・統合され閾値通過

    async def test_max_select_caps_results(self) -> None:
        cands = [_make_candidate(f"A{i}") for i in range(6)]
        scored_json = (
            '{"scored_articles": ['
            + ",".join(
                [
                    f'{{"no":{i + 1},"scores":'
                    f'{{"pir":5,"roi":5,"timeliness":5,"novelty":5}},"rationale":"r"}}'
                    for i in range(6)
                ],
            )
            + "]}"
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(return_value=_structured(scored_json))
        result = await select_deep_dive_articles(
            llm=llm,
            candidates=cands,
            max_select=3,
        )
        assert len(result) == 3

    async def test_score_out_of_range_clipped(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=_structured(
                '{"scored_articles": [{"no":1,"scores":{"pir":99,"roi":-5,"timeliness":3,"novelty":3},"rationale":"out"}]}',  # noqa: E501
            ),
        )
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        assert len(result) == 1
        # pir は 5 にクリップ、roi は 0 にクリップ
        assert result[0].pir == 5.0
        assert result[0].roi == 0.0

    async def test_out_of_range_number_ignored(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=_structured(
                '{"scored_articles": ['
                '{"no":1,"scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"},'
                '{"no":99,"scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"}'
                "]}",
            ),
        )
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        ids = [s.candidate.article_id for s in result]
        assert ids == ["A"]

    async def test_unusable_llm_output_returns_empty(self) -> None:
        """採点が 1 件も取れなければ 0 件 (救済に失敗した構造化出力はこの形になる)。"""
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(return_value=_structured('{"scored_articles": []}'))
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        assert result == []

    async def test_thinking_mode_is_disabled(self) -> None:
        """Gemma 4 26B の thinking mode は digest を空応答化するため OFF 固定。"""
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(
            return_value=_structured(
                '{"scored_articles": [{"no":1,"scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"}]}',  # noqa: E501
            ),
        )
        await select_deep_dive_articles(llm=llm, candidates=cands)
        kwargs = llm.generate_structured.await_args.kwargs
        assert kwargs.get("think") is False


class TestScoredArticleShape:
    def test_dataclass_is_frozen(self) -> None:
        cand = _make_candidate("A")
        s = ScoredArticle(
            candidate=cand,
            pir=5.0,
            roi=4.0,
            timeliness=3.0,
            novelty=2.0,
            composite=3.7,
            rationale="r",
        )
        from dataclasses import FrozenInstanceError

        with pytest.raises(FrozenInstanceError):
            s.composite = 0.0  # type: ignore[misc]


class TestPirContextInjection:
    """段5: deep dive rubric に実 PIR を注入し pir 軸を PIR 駆動化。"""

    def test_render_includes_pir_titles(self) -> None:
        out = _render_prompt(
            items=[_make_candidate("a1")],
            recent_briefs=[],
            past_selected_keys=[],
            pir_context=[
                {
                    "id": "pir_cn",
                    "title": "中国系 APT 動向",
                    "description": "Volt Typhoon 等の検知",
                },
                {"id": "pir_jp", "title": "日本標的の攻撃", "description": "JP 標的 active threat"},
            ],
        )
        assert "現在の SIR" in out
        assert "中国系 APT 動向" in out
        assert "Volt Typhoon 等の検知" in out
        assert "日本標的の攻撃" in out

    def test_render_empty_pir_context_falls_back(self) -> None:
        out = _render_prompt(
            items=[_make_candidate("a1")],
            recent_briefs=[],
            past_selected_keys=[],
            pir_context=[],
        )
        assert "SIR 未登録" in out

    async def test_select_injects_real_pir_config(self) -> None:
        # select_deep_dive_articles が config/delivery/pir.yaml から
        # pir_context を構築し prompt に注入する
        # (死にコードでない保証)。LLM をモックし prompt を捕捉。
        from src.pir.integration import invalidate_cache

        invalidate_cache()
        llm = AsyncMock(spec=LLMClient)
        llm.generate_structured = AsyncMock(return_value=_structured('{"scored_articles": []}'))
        await select_deep_dive_articles(llm=llm, candidates=[_make_candidate("a1")])
        prompt = llm.generate_structured.call_args.kwargs["prompt"]
        assert "現在の SIR" in prompt
        assert "中国系 APT" in prompt  # 実 pir.yaml の代表 title
