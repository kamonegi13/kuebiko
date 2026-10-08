"""src.tools.article_triage のテスト (Phase 3.1)。"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision


def _article(title: str = "Test", summary: str = "<p>summary</p>") -> Article:
    return Article(
        id="t:1",
        title=title,
        url="https://example.com/x",
        summary_html=summary,
        author=None,
        published=datetime.now(UTC),
        feed_title="Test Feed",
        feed_url="https://example.com/feed",
    )


@pytest.mark.asyncio
async def test_returns_high_importance_from_llm() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="国家APTの新キャンペーン"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("中国APT Volt Typhoon 新事象"))
    assert decision.importance == "high"
    assert "国家APT" in decision.reason


@pytest.mark.asyncio
async def test_returns_low_for_unrelated() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="low", reason="一般Tips"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("パスワード管理のヒント"))
    assert decision.importance == "low"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_medium() -> None:
    """LLM 失敗時は medium で graceful degradation (重要記事を捨てない)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.importance == "medium"
    assert "triage error" in decision.reason


@pytest.mark.asyncio
async def test_triage_flat_uses_flat_prompt_regardless_of_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """triage_flat は TRIAGE_FLAT env の値に関わらず常に平たい prompt を使う (2026-10-08、M4)。"""
    from src.tools.triage_flat_rubric import RUBRIC_VERSION

    monkeypatch.delenv("TRIAGE_FLAT", raising=False)
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason="x"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage_flat(_article("テスト記事"))
    assert decision.importance == "medium"
    prompt = llm.generate_structured.await_args.args[0]
    assert RUBRIC_VERSION in prompt


@pytest.mark.asyncio
async def test_triage_flat_failure_falls_back_to_medium() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
    triage = ArticleTriage(llm)
    decision = await triage.triage_flat(_article("X"))
    assert decision.importance == "medium"
    assert decision.error is True


@pytest.mark.asyncio
async def test_prompt_includes_title_and_body() -> None:
    """プロンプトにタイトル・概要が含まれる (LLM 入力の確認)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    await triage.triage(
        _article(
            title="CISA Warns of Telnet 0-day",
            summary="<p>The U.S. CISA issued a warning about CVE-2026-12345...</p>",
        ),
    )
    args = llm.generate_structured.call_args
    prompt = args[0][0]
    assert "CISA Warns of Telnet 0-day" in prompt
    assert "CVE-2026-12345" in prompt


@pytest.mark.asyncio
async def test_html_tags_stripped_from_preview() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="medium", reason=""),
    )
    triage = ArticleTriage(llm)
    await triage.triage(_article(summary="<p>本文</p><div>もう一つ</div>"))
    prompt = llm.generate_structured.call_args[0][0]
    # HTML タグは除去されている
    assert "<p>" not in prompt
    assert "<div>" not in prompt
    # 本文テキストは残る
    assert "本文" in prompt
    assert "もう一つ" in prompt


@pytest.mark.asyncio
async def test_llm_invoked_with_think_false() -> None:
    """thinking モード OFF で呼び出される (要約タスク同様、思考不要)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    await triage.triage(_article())
    args = llm.generate_structured.call_args
    assert args.kwargs.get("think") is False


# ---------- Phase 5P: triage 失敗の表面化 (error フラグ) ----------


def test_triage_decision_has_error_flag_default_false() -> None:
    decision = TriageDecision(importance="medium", reason="x")
    assert decision.error is False


@pytest.mark.asyncio
async def test_triage_returns_error_true_on_llm_failure() -> None:
    """LLM 失敗時は medium に倒すと同時に error=True を立てる (silent 失敗対策)。"""
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(side_effect=RuntimeError("ollama down"))
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.importance == "medium"
    assert decision.error is True
    assert "triage error" in decision.reason


@pytest.mark.asyncio
async def test_triage_returns_error_false_on_normal_path() -> None:
    llm = AsyncMock()
    llm.generate_structured = AsyncMock(
        return_value=TriageDecision(importance="high", reason="x"),
    )
    triage = ArticleTriage(llm)
    decision = await triage.triage(_article("X"))
    assert decision.error is False


class TestFilterByTriageRejectedSplit:
    """_filter_by_triage の rejected/枠あふれ分離 (2026-07-12 リサイクル根治)。

    importance 不足 = 評価済み・不採用 → rejected (呼び出し側が URL 既読化する終端状態)。
    max_keep の枠あふれ = 評価は keep 水準 → rejected に含めない (次 run のリトライ権)。
    """

    @staticmethod
    def _a(aid: str, title: str) -> Article:
        return Article(
            id=aid,
            title=title,
            url=f"https://example.com/{aid}",
            summary_html="<p>s</p>",
            author=None,
            published=datetime.now(UTC),
            feed_title="Test Feed",
            feed_url="https://example.com/feed",
        )

    @pytest.mark.asyncio
    async def test_low_importance_goes_to_rejected(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a("a-high", "high article"), self._a("a-low", "low article")]
        decisions = {
            "high article": TriageDecision(importance="high", reason="x"),
            "low article": TriageDecision(importance="low", reason="x"),
        }
        llm = AsyncMock()

        async def _gen(prompt: str, *a: object, **k: object) -> TriageDecision:
            for t, d in decisions.items():
                if t in prompt:
                    return d
            return TriageDecision(importance="low", reason="?")

        llm.generate_structured = AsyncMock(side_effect=_gen)
        survivors, skipped, skipped_ids, _err, rejected, _shadow = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=10
        )
        assert [a.id for a in survivors] == ["a-high"]
        assert skipped == 1
        assert skipped_ids == ["a-low"]
        assert [r.article_id for r in rejected] == ["a-low"]
        # 落選は理由つきで返す (呼び出し側が triage_rejections に記録する、2026-10-02)
        assert rejected[0].importance == "low"
        assert rejected[0].reason == "x"
        assert rejected[0].feed_url == "https://example.com/feed"

    @pytest.mark.asyncio
    async def test_max_keep_overflow_is_skipped_but_not_rejected(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a(f"a-{i}", f"high article {i}") for i in range(3)]
        llm = AsyncMock()
        llm.generate_structured = AsyncMock(
            return_value=TriageDecision(importance="high", reason="x"),
        )
        survivors, skipped, skipped_ids, _err, rejected, _shadow = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=2
        )
        assert len(survivors) == 2
        assert skipped == 1  # 枠あふれは skip には数える (既読化はしない)
        assert len(skipped_ids) == 1
        assert rejected == []  # 評価は keep 水準 → リトライ権を保持


class TestIngestRuleV2:
    """INGEST_RULE_V2 (2026-10-08、M4): flat triage >= medium OR 取り込みヒント発火。

    既定 (env 未設定) では importance 不足の記事は従来どおり rejected のまま
    (docs/importance_relevance_redesign.md §6b)。
    """

    @staticmethod
    def _a(aid: str, title: str) -> Article:
        return TestFilterByTriageRejectedSplit._a(aid, title)

    @pytest.mark.asyncio
    async def test_default_off_rejects_low_japan_article(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline.filters import _filter_by_triage

        monkeypatch.delenv("INGEST_RULE_V2", raising=False)
        arts = [self._a("jp", "日本の重要インフラ企業への攻撃")]
        llm = AsyncMock()
        llm.generate_structured = AsyncMock(
            return_value=TriageDecision(importance="low", reason="平たい判定で low"),
        )
        survivors, _s, _i, _e, rejected, _shadow = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=10
        )
        assert survivors == []
        assert [r.article_id for r in rejected] == ["jp"]

    @pytest.mark.asyncio
    async def test_enabled_keeps_low_japan_article_via_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline.filters import _filter_by_triage

        monkeypatch.setenv("INGEST_RULE_V2", "1")
        arts = [
            self._a("jp", "日本の重要インフラ企業への攻撃"),
            self._a("noise", "新しいパスワード管理アプリのレビュー"),
        ]
        llm = AsyncMock()
        llm.generate_structured = AsyncMock(
            return_value=TriageDecision(importance="low", reason="平たい判定で low"),
        )
        survivors, _s, _i, _e, rejected, _shadow = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=10
        )
        assert [a.id for a in survivors] == ["jp"]
        assert [r.article_id for r in rejected] == ["noise"]

    @pytest.mark.asyncio
    async def test_enabled_still_respects_max_keep_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.pipeline.filters import _filter_by_triage

        monkeypatch.setenv("INGEST_RULE_V2", "1")
        arts = [
            self._a("jp1", "日本の重要インフラ企業への攻撃 第一報"),
            self._a("jp2", "日本の重要インフラ企業への攻撃 第二報"),
        ]
        llm = AsyncMock()
        llm.generate_structured = AsyncMock(
            return_value=TriageDecision(importance="low", reason="平たい判定で low"),
        )
        survivors, _s, _i, _e, rejected, _shadow = await _filter_by_triage(
            arts, llm, keep_importance={"high", "medium"}, max_keep=1
        )
        assert len(survivors) == 1
        assert [r.article_id for r in rejected] == ["jp2"]


class TestGeoRescue:
    """地政学の救済 (2026-10-02)。

    s21 は教師データで「一般地政学 = low」を学習しており、地政学の SIR を足しても
    「サイバー要素なし → low」の癖が判定基準より強く効く (落ちた注視国の地政学 60 件中
    medium に上がったのは 21 件、素の 26B は 42 件)。理由が地政学・軍事・外交の落選だけを
    救済用のモデルで判定し直す。high にはしない (09-29 の利用者決定)。
    """

    @staticmethod
    def _a(aid: str, title: str) -> Article:
        return TestFilterByTriageRejectedSplit._a(aid, title)

    @staticmethod
    def _llm(decide: dict[str, TriageDecision]) -> AsyncMock:
        llm = AsyncMock()

        async def _gen(prompt: str, *a: object, **k: object) -> TriageDecision:
            for t, d in decide.items():
                if t in prompt:
                    return d
            return TriageDecision(importance="low", reason="?")

        llm.generate_structured = AsyncMock(side_effect=_gen)
        return llm

    @pytest.mark.asyncio
    async def test_geo_low_is_rejudged_and_capped_to_medium(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [
            self._a("geo", "PLA drills around Huangyan Dao"),
            self._a("noise", "Museum visit"),
            self._a("cyber", "Ransomware hits hospital"),
        ]
        primary = self._llm(
            {
                "PLA drills": TriageDecision(importance="low", reason="一般軍事報道"),
                "Museum": TriageDecision(importance="low", reason="文化行事"),
                "Ransomware": TriageDecision(importance="medium", reason="x"),
            }
        )
        rescue = self._llm(
            {
                "PLA drills": TriageDecision(importance="high", reason="中国軍の行動"),
                "Museum": TriageDecision(importance="medium", reason="?"),
            }
        )

        survivors, _skipped, _ids, _err, rejected, _shadow = await _filter_by_triage(
            arts, primary, keep_importance={"high", "medium"}, max_keep=10, rescue_llm=rescue
        )

        # 地政学を理由に落ちた記事だけ判定し直す (文化行事は対象外 = 救済のモデルを呼ばない)
        assert [a.id for a in survivors] == ["cyber", "geo"]
        assert [r.article_id for r in rejected] == ["noise"]
        prompts = [c.args[0] for c in rescue.generate_structured.await_args_list]
        assert len(prompts) == 1 and "PLA drills" in prompts[0]

    @pytest.mark.asyncio
    async def test_rescued_articles_rank_after_original_keeps(self) -> None:
        """枠あふれで押し出されるのは救済した記事の方。"""
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a("geo", "Russian Navy near Japan"), self._a("cyber", "APT hits JP")]
        primary = self._llm(
            {
                "Russian Navy": TriageDecision(importance="low", reason="軍事・地政学"),
                "APT": TriageDecision(importance="medium", reason="x"),
            }
        )
        rescue = self._llm({"Russian Navy": TriageDecision(importance="medium", reason="露軍")})

        survivors, _s, _i, _e, rejected, _shadow = await _filter_by_triage(
            arts, primary, keep_importance={"high", "medium"}, max_keep=1, rescue_llm=rescue
        )

        assert [a.id for a in survivors] == ["cyber"]
        assert rejected == []  # 救済で keep 水準になった = 枠あふれ (次 run で再評価)

    @pytest.mark.asyncio
    async def test_without_rescue_llm_behaviour_is_unchanged(self) -> None:
        from src.pipeline.filters import _filter_by_triage

        arts = [self._a("geo", "PLA drills")]
        primary = self._llm({"PLA drills": TriageDecision(importance="low", reason="軍事")})

        survivors, _s, _i, _e, rejected, _shadow = await _filter_by_triage(
            arts, primary, keep_importance={"high", "medium"}, max_keep=10
        )

        assert survivors == []
        assert [r.article_id for r in rejected] == ["geo"]
