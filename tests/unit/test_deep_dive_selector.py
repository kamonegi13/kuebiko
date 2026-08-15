"""src.digest.deep_dive_selector のテスト (Phase 5T-T2)。

LLM rubric scoring + composite + selection の振る舞いを検証する。
LLMClient は AsyncMock で置換し、ネットワーク I/O は発生させない。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.digest.db_filter import DigestCandidate
from src.digest.deep_dive_selector import (
    DEFAULT_WEIGHTS,
    ScoredArticle,
    _compute_composite,
    _parse_llm_output,
    _render_prompt,
    select_deep_dive_articles,
)
from src.tools.llm_client import LLMClient, LLMResponse


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


def _llm_response(text: str) -> LLMResponse:
    return LLMResponse(text=text, model="test-model")


class TestParseOutput:
    def test_plain_json(self) -> None:
        text = '{"scored_articles": [{"id":"A","scores":{"pir":5,"roi":4,"timeliness":3,"novelty":2},"rationale":"r"}]}'  # noqa: E501
        parsed = _parse_llm_output(text)
        assert parsed[0]["id"] == "A"

    def test_with_fenced_block(self) -> None:
        text = (
            "前置きテキスト\n"
            "```json\n"
            '{"scored_articles": [{"id":"A","scores":{"pir":5,"roi":4,"timeliness":3,"novelty":2},"rationale":"r"}]}\n'  # noqa: E501
            "```\n"
            "末尾説明"
        )
        parsed = _parse_llm_output(text)
        assert len(parsed) == 1
        assert parsed[0]["id"] == "A"

    def test_invalid_json_returns_empty(self) -> None:
        assert _parse_llm_output("not json at all") == []

    def test_empty_text_returns_empty(self) -> None:
        assert _parse_llm_output("") == []

    def test_salvage_truncated_output(self) -> None:
        """Phase 5T-T2.1: 末尾切れの JSON でも先頭 entries を salvage する。"""
        # 末尾の最後の entry が "..." で途切れている (max_tokens 到達状況の再現)
        text = (
            "```json\n"
            '{"scored_articles": [\n'
            '{"id":"A","scores":{"pir":5,"roi":4,"timeliness":3,"novelty":2},"rationale":"r1"},\n'
            '{"id":"B","scores":{"pir":3,"roi":3,"timeliness":3,"novelty":3},"rationale":"r2"},\n'
            '{"id":"C","scores":{"pir":1,"roi":3,'  # truncated mid-entry
        )
        parsed = _parse_llm_output(text)
        ids = [e["id"] for e in parsed]
        assert "A" in ids
        assert "B" in ids
        # C は scores 不完全だが id+scores の最小条件を満たすかは regex 次第。
        # 重要なのは A/B が確実に救出されること。

    def test_salvage_typo_in_rationale_key(self) -> None:
        """Phase 5T-T2.1: 'CRationale' のような typo を含む entry も他は採用。"""
        text = (
            '{"scored_articles": [\n'
            '{"id":"A","scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"CRationale":"typo"},\n'
            '{"id":"B","scores":{"pir":3,"roi":3,"timeliness":3,"novelty":3},"rationale":"ok"}\n'
            "]}"
        )
        parsed = _parse_llm_output(text)
        ids = [e["id"] for e in parsed]
        assert "A" in ids and "B" in ids

    def test_salvage_unknown_score_key_ignored(self) -> None:
        """Phase 5T-T2.1: scores 内に 'way:2' のような未知 key があっても他 score 採用。"""
        text = (
            '{"scored_articles": [\n'
            '{"id":"A","scores":{"pir":4,"roi":3,"way":2,"novelty":3},"rationale":"r"}\n'
            "]}"
        )
        parsed = _parse_llm_output(text)
        assert len(parsed) == 1
        scores = parsed[0]["scores"]
        assert isinstance(scores, dict)
        assert scores.get("pir") == 4.0
        assert scores.get("roi") == 3.0
        # timeliness は欠損 (way ではなく) → selector 側で 0 として扱われる
        assert "timeliness" not in scores


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
        llm.generate = AsyncMock(
            return_value=_llm_response(
                """
                {"scored_articles": [
                  {"id":"A","scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"top"},
                  {"id":"B","scores":{"pir":3,"roi":3,"timeliness":3,"novelty":3},"rationale":"mid"},
                  {"id":"C","scores":{"pir":1,"roi":1,"timeliness":1,"novelty":1},"rationale":"low"}
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
        llm.generate = AsyncMock(
            return_value=_llm_response(
                '{"scored_articles": [{"id":"A","scores":{"pir":1,"roi":1,"timeliness":1,"novelty":1},"rationale":"low"}]}',  # noqa: E501
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

        def _score_chunk(**kwargs: object) -> LLMResponse:
            prompt = str(kwargs.get("prompt", ""))
            present = [i for i in ids if i in prompt]
            entries = ",".join(
                f'{{"id":"{i}","scores":{{"pir":5,"roi":5,"timeliness":5,"novelty":5}},'
                f'"rationale":"r"}}'
                for i in present
            )
            return _llm_response(f'{{"scored_articles":[{entries}]}}')

        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=_score_chunk)
        result = await select_deep_dive_articles(
            llm=llm, candidates=cands, composite_threshold=2.5, max_select=12
        )
        assert llm.generate.await_count == 3  # 3 チャンクに分割して呼ばれる
        scored_ids = {s.candidate.article_id for s in result}
        assert scored_ids == set(ids)  # 全 5 件が採点・統合され閾値通過

    async def test_max_select_caps_results(self) -> None:
        cands = [_make_candidate(f"A{i}") for i in range(6)]
        scored_json = (
            '{"scored_articles": ['
            + ",".join(
                [
                    f'{{"id":"A{i}","scores":{{"pir":5,"roi":5,"timeliness":5,"novelty":5}},"rationale":"r"}}'
                    for i in range(6)
                ],
            )
            + "]}"
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(return_value=_llm_response(scored_json))
        result = await select_deep_dive_articles(
            llm=llm,
            candidates=cands,
            max_select=3,
        )
        assert len(result) == 3

    async def test_score_out_of_range_clipped(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=_llm_response(
                '{"scored_articles": [{"id":"A","scores":{"pir":99,"roi":-5,"timeliness":3,"novelty":3},"rationale":"out"}]}',  # noqa: E501
            ),
        )
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        assert len(result) == 1
        # pir は 5 にクリップ、roi は 0 にクリップ
        assert result[0].pir == 5.0
        assert result[0].roi == 0.0

    async def test_unknown_id_in_llm_response_ignored(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=_llm_response(
                '{"scored_articles": ['
                '{"id":"A","scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"},'
                '{"id":"GHOST","scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"}'
                "]}",
            ),
        )
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        ids = [s.candidate.article_id for s in result]
        assert ids == ["A"]

    async def test_malformed_llm_output_returns_empty(self) -> None:
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(return_value=_llm_response("garbage"))
        result = await select_deep_dive_articles(llm=llm, candidates=cands)
        assert result == []

    async def test_thinking_mode_is_disabled(self) -> None:
        """Gemma 4 26B の thinking mode は digest を空応答化するため OFF 固定。"""
        cands = [_make_candidate("A")]
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=_llm_response(
                '{"scored_articles": [{"id":"A","scores":{"pir":5,"roi":5,"timeliness":5,"novelty":5},"rationale":"r"}]}',  # noqa: E501
            ),
        )
        await select_deep_dive_articles(llm=llm, candidates=cands)
        kwargs = llm.generate.await_args.kwargs
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
        assert "現在の PIR" in out
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
        assert "PIR 未登録" in out

    async def test_select_injects_real_pir_config(self) -> None:
        # select_deep_dive_articles が config/delivery/pir.yaml から
        # pir_context を構築し prompt に注入する
        # (死にコードでない保証)。LLM をモックし prompt を捕捉。
        from src.pir.integration import invalidate_cache

        invalidate_cache()
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(return_value=_llm_response('{"scored_articles": []}'))
        await select_deep_dive_articles(llm=llm, candidates=[_make_candidate("a1")])
        prompt = llm.generate.call_args.kwargs["prompt"]
        assert "現在の PIR" in prompt
        assert "中国系 APT" in prompt  # 実 pir.yaml の代表 title
