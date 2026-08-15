"""src.main のテスト (Step 8)。

各 I/O ツール (Inoreader / ContentExtractor / LLM / DiscordPublisher) を
``AsyncMock`` で差し替え、End-to-End フローをモック越しに検証する。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Literal, cast
from unittest.mock import AsyncMock

import jinja2
import pytest

from src.config_loader import (
    PipelineConfig,
    ProcessorConfig,
    SourceConfig,
)
from src.main import (
    PipelineRunResult,
    SummaryOutput,
    _build_briefing,
    _cap_vuln_importance,
    _find_pipeline,
    _normalize_iso_date,
    _normalize_temporal,
    _resolve_body,
    _strip_html,
    run_pipeline,
)
from src.tools.article_model import Article
from src.tools.content_extractor import ExtractionResult
from src.tools.discord_publisher import DiscordPublisher

# ---------- フィクスチャ ----------


def _article(
    article_id: str = "tag:google.com,2005:reader/item/1",
    title: str = "Sample article",
    url: str = "https://example.com/article",
    summary_html: str = "<p>fallback body</p>",
) -> Article:
    return Article(
        id=article_id,
        title=title,
        url=url,
        summary_html=summary_html,
        author="Author",
        published=datetime(2024, 1, 1, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.com/feed",
    )


def _summary_output(
    importance: str = "medium",
    category: str = "advisory",
    title_ja: str = "サンプル記事のタイトル",
) -> SummaryOutput:
    return SummaryOutput(
        title_ja=title_ja,
        bluf="新たな脆弱性が発見された。",
        importance=importance,  # type: ignore[arg-type]
        category=category,
        summary="本文の概要記述。" * 5,
        iocs=["1.2.3.4"],
        mitre_techniques=["T1566.001"],
        analyst_note="運用 SOC への展開を推奨。",
    )


def _extraction_success(text: str = "extracted body text " * 30) -> ExtractionResult:
    return ExtractionResult(
        url="https://example.com/article",
        title="Sample",
        text=text,
        language="en",
        success=True,
    )


def _extraction_failure(reason: str = "http_error_404") -> ExtractionResult:
    return ExtractionResult(
        url="https://example.com/article",
        success=False,
        failure_reason=reason,
    )


@pytest.fixture
def template() -> jinja2.Template:
    """テスト用に最小限のプロンプトテンプレ。"""
    env = jinja2.Environment(
        loader=jinja2.DictLoader({"briefing/summarizer.j2": "{{ article.title }}\n{{ body }}"}),
        autoescape=False,
        undefined=jinja2.StrictUndefined,
    )
    return env.get_template("briefing/summarizer.j2")


@pytest.fixture
def pipeline_cfg() -> PipelineConfig:
    return PipelineConfig(
        name="daily-briefing",
        source=SourceConfig(type="rss", max_articles=10),
        processor=ProcessorConfig(),
    )


@pytest.fixture
def app_cfg() -> Any:
    """AppConfig は frozen で生成が重いため、必要属性のみ持つ簡易オブジェクトで代用。"""

    class _Cfg:
        ollama_main_model = "gemma4:31b"

    return _Cfg()


def _build_publishers() -> dict[str, DiscordPublisher]:
    """4 チャンネル分の AsyncMock。"""
    return {ch: AsyncMock(spec=DiscordPublisher) for ch in ("alert", "brief", "watch", "ops")}


def _post_mock(pub: DiscordPublisher) -> AsyncMock:
    """AsyncMock(spec=DiscordPublisher) で差し替えた publisher の ``post`` を mock として
    読むための cast。fixture は production 型 (dict[..., DiscordPublisher]) で注釈して
    run_pipeline にそのまま渡すため、call_count 等の mock 属性はここを経由して参照する。"""
    return cast(AsyncMock, pub.post)


def _build_inoreader(articles: list[Article]) -> AsyncMock:
    inoreader = AsyncMock()
    inoreader.get_unread_articles.return_value = articles
    return inoreader


def _build_source(articles: list[Article], side_effect: object | None = None) -> AsyncMock:
    """Phase 2: ArticleSource を AsyncMock で代用 (fetch のみ実装)。"""
    source = AsyncMock()
    if side_effect is not None:
        source.fetch.side_effect = side_effect
    else:
        source.fetch.return_value = articles
    return source


def _build_extractor_with_results(results: list[ExtractionResult]) -> AsyncMock:
    extractor = AsyncMock()
    extractor.extract.side_effect = list(results)
    return extractor


def _stance_aware_gen(
    outputs: Sequence[object], axes: object | None = None
) -> Callable[..., Awaitable[object]]:
    """generate_structured の side_effect: summarizer(SummaryOutput) は outputs を順に返し
    (例外要素は raise)、統合判断分類器 (JudgmentOut) は固定 stance + (axes 指定時は intent 等)
    を返す。2026-07-26 に stance/analysis_axes/subject の 3 focused 分類器を JudgmentOut 1 つに
    統合したため、旧 AnalysisAxesOut/stance 分岐を JudgmentOut 1 本に集約 (axes 未指定でも
    JudgmentOut は返る = summarizer 抽出値の上に判断を載せる新既定挙動)。"""
    from src.cti.judgment_classifier import JudgmentOut

    _it = iter(list(outputs))

    async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
        if schema is SummaryOutput:
            item = next(_it)
            if isinstance(item, BaseException):
                raise item
            return item
        if schema is JudgmentOut:
            j = JudgmentOut(editorial_stance="factual_report")
            if axes is not None:
                j = j.model_copy(
                    update={
                        "intent": getattr(axes, "intent", "unknown"),
                        "confidence": getattr(axes, "confidence", "low"),
                        "technical": getattr(axes, "technical", None),
                        "event_date": getattr(axes, "event_date", None),
                        "event_date_basis": getattr(axes, "event_date_basis", None),
                        "compromise_date": getattr(axes, "compromise_date", None),
                        "i_infra": getattr(axes, "i_infra", False),
                    }
                )
            return j
        from types import SimpleNamespace

        return SimpleNamespace(editorial_stance="factual_report")

    return _gen


def _build_llm_with_outputs(outputs: list[SummaryOutput]) -> AsyncMock:
    llm = AsyncMock()
    llm.generate_structured.side_effect = _stance_aware_gen(outputs)
    return llm


# ---------- 単体ヘルパ ----------


class TestTitleGrounding:
    """和訳タイトルの接地検証 (監査 2026-08-01)。

    The Register の無関係 3 記事 (offbeat/bofh/personal-tech) が同一の幻覚見出し
    「ロシア、Signal 偽装フィッシング」で alert に 3 連投された事象への決定的ガード。
    幻覚見出しの特徴 = 原文 (原題+本文) に存在しない固有名詞 (英字トークン) を含む。
    カタカナ固有名詞は日英対応が検証不能のため対象外 (fail-open)。
    """

    def test_hallucinated_latin_token_detected(self) -> None:
        from src.pipeline.summary import ungrounded_title_tokens

        toks = ungrounded_title_tokens(
            "ロシア、Signal 偽装フィッシングを展開",
            source_text="BOFH: The PFY does something silly with printers again",
        )
        assert "signal" in toks

    def test_grounded_title_passes(self) -> None:
        from src.pipeline.summary import ungrounded_title_tokens

        toks = ungrounded_title_tokens(
            "Cisco IOS XE に重大な脆弱性",
            source_text="Critical vulnerability in Cisco IOS XE allows remote code execution",
        )
        assert toks == []

    def test_token_grounded_in_body_passes(self) -> None:
        from src.pipeline.summary import ungrounded_title_tokens

        toks = ungrounded_title_tokens(
            "Signal 利用者を標的とするフィッシング",
            source_text=("Hackers target messaging app users. The campaign abuses Signal linking."),
        )
        assert toks == []

    def test_pure_japanese_title_fails_open(self) -> None:
        from src.pipeline.summary import ungrounded_title_tokens

        # 英字トークンなし → 判定材料がないので通す (誤 fallback より見逃し許容)
        assert ungrounded_title_tokens("ロシアの攻撃活動が活発化", "anything else") == []

    def test_generic_latin_tokens_allowlisted(self) -> None:
        from src.pipeline.summary import ungrounded_title_tokens

        assert ungrounded_title_tokens("Web サイト改ざんの報告", "site defacement report") == []


class TestCapVulnImportance:
    """Phase B-cal2: vulnerability/advisory の high 過大評価ガード。"""

    def test_high_vuln_without_exploit_signal_is_downgraded(self) -> None:
        # Arrange: パッチ済みで実悪用言及の無い CVE を high と LLM が返した
        title = "Plesk の Linux 版に権限昇格の脆弱性 - 2月のリリースで修正済み"
        summary = "修正済みのため影響は限定的。"
        # Act
        result = _cap_vuln_importance("high", "vulnerability", title, summary)
        # Assert
        assert result == "medium"

    def test_high_vuln_with_exploit_signal_is_kept(self) -> None:
        # Arrange: 実悪用中の 0day
        result = _cap_vuln_importance(
            "high", "vulnerability", "Ivanti の 0day が実際に悪用されています", ""
        )
        # Assert
        assert result == "high"

    def test_kev_keyword_keeps_high(self) -> None:
        result = _cap_vuln_importance(
            "high", "advisory", "CISA adds CVE to KEV catalog", "known exploited"
        )
        assert result == "high"

    def test_poc_only_is_downgraded(self) -> None:
        # PoC 公開のみ (実悪用なし) は high に値しない
        result = _cap_vuln_importance(
            "high", "vulnerability", "Cisco の脆弱性に PoC Exploit が公開", "PoC のみ"
        )
        assert result == "medium"

    def test_non_vuln_category_is_not_capped(self) -> None:
        # apt は別軸 (actionability) なので guard 対象外
        result = _cap_vuln_importance("high", "apt", "新しい APT 活動", "")
        assert result == "high"

    def test_medium_is_unchanged(self) -> None:
        result = _cap_vuln_importance("medium", "vulnerability", "重大な脆弱性", "")
        assert result == "medium"

    def test_english_actively_exploited_keeps_high(self) -> None:
        result = _cap_vuln_importance(
            "high", "vulnerability", "Bug is being actively exploited in the wild", ""
        )
        assert result == "high"


class TestNormalizeTemporal:
    """時間軸レイヤ b/c: event_date / dwell の検証 (報道時刻と分離)。"""

    _REF = date(2026, 6, 27)  # 報道日 +1d 相当の上限

    def test_iso_date_valid_passes(self) -> None:
        assert _normalize_iso_date("2026-03-10", ceiling=self._REF) == "2026-03-10"

    def test_iso_date_month_only_becomes_first(self) -> None:
        assert _normalize_iso_date("2024-03", ceiling=self._REF) == "2024-03-01"

    def test_iso_date_future_rejected(self) -> None:
        assert _normalize_iso_date("2027-01-01", ceiling=self._REF) is None

    def test_iso_date_pre_2000_rejected(self) -> None:
        assert _normalize_iso_date("1998-05-01", ceiling=self._REF) is None

    def test_iso_date_garbage_rejected(self) -> None:
        assert _normalize_iso_date("3ヶ月前", ceiling=self._REF) is None
        assert _normalize_iso_date("2026-13-40", ceiling=self._REF) is None
        assert _normalize_iso_date(None, ceiling=self._REF) is None

    def test_full_temporal_kept(self) -> None:
        ev, basis, comp = _normalize_temporal(
            "2026-06-15", "disclosed", "2026-03-01", reference=self._REF
        )
        assert (ev, basis, comp) == ("2026-06-15", "disclosed", "2026-03-01")

    def test_invalid_basis_dropped_but_date_kept(self) -> None:
        ev, basis, comp = _normalize_temporal("2026-06-15", "guessed", None, reference=self._REF)
        assert ev == "2026-06-15"
        assert basis is None

    def test_compromise_after_event_dropped(self) -> None:
        # dwell が負になる抽出 (侵害日 > 検知日) は捨てる
        _ev, _b, comp = _normalize_temporal(
            "2026-03-01", "disclosed", "2026-06-15", reference=self._REF
        )
        assert comp is None

    def test_compromise_without_event_dropped(self) -> None:
        # event_date 不明だと dwell を測れないので compromise 単独は保持しない
        ev, _b, comp = _normalize_temporal(None, None, "2026-03-01", reference=self._REF)
        assert ev is None
        assert comp is None

    def test_basis_dropped_when_no_event_date(self) -> None:
        ev, basis, _c = _normalize_temporal(None, "occurred", None, reference=self._REF)
        assert ev is None
        assert basis is None


class TestStripHtml:
    def test_removes_tags(self) -> None:
        assert _strip_html("<p>hello <b>world</b></p>") == "hello world"

    def test_handles_entities(self) -> None:
        assert "Tom & Jerry" in _strip_html("<p>Tom &amp; Jerry</p>")

    def test_collapses_whitespace(self) -> None:
        assert _strip_html("<p>a\n\n  b\t\tc</p>") == "a b c"

    def test_empty_input(self) -> None:
        assert _strip_html("") == ""


class TestResolveBody:
    def test_uses_extracted_text_when_success(self) -> None:
        article = _article()
        ex = _extraction_success("extracted text content here")
        # 2026-07-27: (body, body_source, failure_reason) の 3-tuple を返す。
        body, source, reason = _resolve_body(article, ex)
        assert body == "extracted text content here"
        assert source == "full_extract"
        assert reason is None

    def test_falls_back_to_summary_html_on_failure(self) -> None:
        article = _article(summary_html="<p>fallback body</p>")
        ex = _extraction_failure()
        body, source, reason = _resolve_body(article, ex)
        assert body == "fallback body"
        # 全文失敗→feed 抜粋 fallback は無音でなく body_source='feed_summary' で構造化して残す。
        assert source == "feed_summary"

    def test_falls_back_when_extracted_text_blank(self) -> None:
        article = _article(summary_html="<p>fb</p>")
        ex = ExtractionResult(
            url="https://example.com/",
            text="   ",
            success=True,
            language="en",
        )
        body, source, _reason = _resolve_body(article, ex)
        assert body == "fb"
        assert source == "feed_summary"


class TestBuildBriefing:
    def test_maps_fields_correctly(self) -> None:
        article = _article(title="Cool article", url="https://x.com/")
        summary = _summary_output(
            importance="high",
            category="apt",
            title_ja="クールな記事の日本語訳",
        )
        msg = _build_briefing(article, summary)
        # 原タイトルは英語 (kana なし) なので title_ja が採用される
        assert msg.title == "クールな記事の日本語訳"
        assert msg.metadata.get("original_title") == "Cool article"
        assert msg.importance == "high"
        assert msg.category == "apt"
        # Phase 5T-O: Inoreader 経路は BLUF を生成しない (title と重複していたため)
        assert msg.bluf == ""
        assert msg.iocs == ["1.2.3.4"]
        assert msg.mitre_techniques == ["T1566.001"]
        assert msg.sources[0].url == "https://x.com/"

    def test_uses_title_ja_when_provided(self) -> None:
        """Phase 5J-2: LLM が title_ja を返した場合、Discord title に翻訳版を使う。"""
        article = _article(
            title="Microsoft Patches Critical RCE in Exchange",
            url="https://x.com/",
        )
        summary = SummaryOutput(
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            title_ja="Microsoft、Exchange の重大 RCE を修正",
        )
        msg = _build_briefing(article, summary)
        assert msg.title == "Microsoft、Exchange の重大 RCE を修正"
        # 原タイトルは metadata に保持
        assert msg.metadata.get("original_title") == "Microsoft Patches Critical RCE in Exchange"

    def test_chinese_title_gets_translated(self) -> None:
        """Phase 5J-2: 中国語タイトル (kana なし) は LLM 翻訳を採用 (Phase 5E のバグ修正)。"""
        article = _article(
            title="重要数据性质的再认识：级别概念 vs. 类别概念",
            url="https://x.com/",
        )
        summary = SummaryOutput(
            bluf="b",
            importance="low",
            category="policy",
            summary="s",
            title_ja="重要データ性質の再認識: 階層概念 vs. 分類概念",
        )
        msg = _build_briefing(article, summary)
        # 中国語は kana なし → LLM 翻訳を採用
        assert msg.title == "重要データ性質の再認識: 階層概念 vs. 分類概念"

    def test_japanese_title_with_kana_preserves_original(self) -> None:
        """Phase 5J-2: ひらがな/カタカナを含む真の日本語タイトルは原文保護。"""
        article = _article(
            title="マネーフォワード、GitHub への不正アクセスでビジネスカード情報流出の可能性",
            url="https://x.com/",
        )
        summary = SummaryOutput(
            bluf="b",
            importance="medium",
            category="breach",
            summary="s",
            # LLM が「微修正」した翻訳 — これを採用すると改変になる
            title_ja="マネーフォワードがGitHubから不正アクセス、ビジネスカード流出か",
        )
        msg = _build_briefing(article, summary)
        # 原タイトルが優先される
        assert msg.title == article.title

    def test_has_japanese_kana_helper(self) -> None:
        """Phase 5J-2: ひらがな/カタカナの存在で日本語特異性を判定 (中国語と区別)。"""
        from src.main import _has_japanese_kana

        # 真の日本語 (kana あり)
        assert _has_japanese_kana("これは日本語のタイトル") is True
        assert _has_japanese_kana("マネーフォワード、GitHubへの不正アクセス") is True
        assert _has_japanese_kana("APT41 が日本標的") is True
        # 中国語 (kana なし、CJK 漢字のみ) → False で翻訳対象
        assert _has_japanese_kana("重要数据性质的再认识") is False
        # 英語 → False で翻訳対象
        assert _has_japanese_kana("Microsoft Patches Critical RCE") is False
        assert _has_japanese_kana("") is False
        # 半角カナ
        assert _has_japanese_kana("ｶﾀｶﾅ") is True

    def test_drop_self_url_removes_article_url(self) -> None:
        """Phase 5J-2: article.url のホストと一致する URL/domain を IOC から除外。"""
        from src.main import _drop_self_url

        iocs = [
            "https://www.gbhackers.com/article-name/",
            "evil.malicious.example",
            "CVE-2024-99999",
            "gbhackers.com",
            "198.51.100.42",
        ]
        out = _drop_self_url(iocs, "https://www.gbhackers.com/article-name/")
        # gbhackers.com の URL は除外、悪性 host / CVE / IP は残る
        assert "evil.malicious.example" in out
        assert "CVE-2024-99999" in out
        assert "198.51.100.42" in out
        assert "gbhackers.com" not in out
        assert all("gbhackers.com" not in s for s in out)

    def test_drop_self_url_handles_empty(self) -> None:
        from src.main import _drop_self_url

        assert _drop_self_url([], "https://x.com/") == []
        assert _drop_self_url(["CVE-2024-1"], "") == ["CVE-2024-1"]


class TestFindPipeline:
    def test_finds_by_name(self, pipeline_cfg: PipelineConfig) -> None:
        result = _find_pipeline([pipeline_cfg], "daily-briefing")
        assert result is pipeline_cfg

    def test_missing_raises_value_error(self, pipeline_cfg: PipelineConfig) -> None:
        with pytest.raises(ValueError, match="存在しません"):
            _find_pipeline([pipeline_cfg], "nonexistent")


# ---------- run_pipeline (E2E with mocks) ----------


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_full_flow_routes_by_importance(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # 3 件の記事、それぞれ別の重要度になる
        articles = [
            _article(article_id="a-high", url="https://x.com/h"),
            _article(article_id="a-med", url="https://x.com/m"),
            _article(article_id="a-low", url="https://x.com/l"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results(
            [_extraction_success() for _ in articles],
        )
        llm = _build_llm_with_outputs(
            [
                _summary_output(importance="high"),
                _summary_output(importance="medium"),
                _summary_output(importance="low"),
            ],
        )
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )

        assert isinstance(result, PipelineRunResult)
        assert result.total_fetched == 3
        assert result.summarized == 3
        assert result.posted == 3
        assert result.marked_read == 0
        assert result.errors == []

        # Phase 5K: 中立 fixture (Japan/KEV/known_apt なし) は importance 単独では alert に
        # 行かない。breaking + advisory category は brief、low は watch に振り分け。
        # 3 件すべてが brief or watch に流れる (alert に行かないのが Phase 5K の意図)
        total_calls = (
            _post_mock(publishers["alert"]).call_count
            + _post_mock(publishers["brief"]).call_count
            + _post_mock(publishers["watch"]).call_count
        )
        assert total_calls == 3
        # Phase 5K: ops チャンネル (旧 system) への稼働 heartbeat
        assert _post_mock(publishers["ops"]).call_count == 1

    @pytest.mark.asyncio
    async def test_extract_failure_uses_summary_html_fallback(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [
            _article(
                article_id="a-1",
                # degenerate body ゲート (80 字未満は要約しない) を通る現実的な長さの summary
                summary_html=(
                    "<p>" + "RSS 配信元が要約として提供した実質的な本文断片。" * 5 + "</p>"
                ),
            ),
        ]
        source = _build_source(articles)
        # 抽出失敗
        extractor = _build_extractor_with_results([_extraction_failure()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
        )

        assert result.summarized == 1
        # LLM への prompt に fallback content が含まれている (先頭呼出 = summarizer。
        # 後続は stance / 分析軸 focused 分類器なので prompt が異なる)
        prompt_arg = llm.generate_structured.call_args_list[0].args[0]
        assert "RSS 配信元が要約として提供した実質的な本文断片" in prompt_arg

    @pytest.mark.asyncio
    async def test_one_article_failure_does_not_stop_others(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [
            _article(article_id="ok-1", url="https://x.com/1"),
            _article(article_id="bad-2", url="https://x.com/2"),
            _article(article_id="ok-3", url="https://x.com/3"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 3)

        # 2 件目だけ LLM が例外を投げる (stance call が list を消費しないよう schema-aware)
        llm = AsyncMock()
        llm.generate_structured.side_effect = _stance_aware_gen(
            [
                _summary_output(importance="medium"),
                RuntimeError("LLM failed"),
                _summary_output(importance="medium"),
            ]
        )
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
        )

        assert result.total_fetched == 3
        assert result.summarized == 2
        assert result.posted == 2
        assert result.marked_read == 0
        assert any("bad-2" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_dry_run_does_not_post_or_mark(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        articles = [_article(article_id="a-1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="high")])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=True,
        )

        # 投稿・既読化はしない
        for pub in publishers.values():
            _post_mock(pub).assert_not_called()

        # dry-run 結果
        assert result.dry_run is True
        assert result.posted == 0
        assert result.marked_read == 0
        assert result.summarized == 1

        # stdout にプレビューが出ている
        captured = capsys.readouterr()
        assert "BLUF:" in captured.out
        # Phase 5K: 中立 fixture は brief or watch (importance high → brief)
        assert "channel: brief" in captured.out

    @pytest.mark.asyncio
    async def test_max_articles_argument_overrides_pipeline_default(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        source = _build_source([])
        extractor = AsyncMock()
        llm = AsyncMock()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=_build_publishers(),
            template=template,
            max_articles=3,
        )
        source.fetch.assert_called_once()
        kwargs = source.fetch.call_args.kwargs
        assert kwargs["max_count"] == 3

    @pytest.mark.asyncio
    async def test_post_failure_is_recorded_but_continues(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [
            _article(article_id="a-1"),
            _article(article_id="a-2"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 2)
        llm = _build_llm_with_outputs(
            [_summary_output(importance="medium"), _summary_output(importance="medium")],
        )
        publishers = _build_publishers()
        # daily 投稿が 1 件目で失敗、2 件目は成功するよう side_effect で順序制御
        _post_mock(publishers["brief"]).side_effect = [RuntimeError("post boom"), None]

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
        )

        assert result.summarized == 2
        assert result.posted == 1
        # 既読化は posted のみ
        assert result.marked_read == 0
        assert any("post boom" in e for e in result.errors)


# ---------- Phase 2.6a: Grok ブランチ (LLM スキップ + 多 BriefingMessage 展開) ----------


class TestGrokBranch:
    @pytest.mark.asyncio
    async def test_grok_non_jsonl_article_is_skipped(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        """Grok JSONL 化後、markdown 形式の grok メールは取り込まない (warn + skip)。"""
        grok_article = Article(
            id="grok:abcdef:1",
            title="Daily CTI",
            url="https://grok.com/chat/uuid",
            summary_html="【全体概要】\n10件程度の事象が観測。\n",
            author="Grok",
            published=datetime(2024, 1, 1, tzinfo=UTC),
            feed_title="Grok",
            feed_url="https://grok.com/",
        )

        source = _build_source([grok_article])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = AsyncMock()
        llm.generate_structured.side_effect = AssertionError("LLM should be skipped for grok")
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )

        # 非 JSONL grok は briefing 0 件 → 投稿なし
        assert result.posted == 0
        assert result.summarized == 0
        llm.generate_structured.assert_not_called()


# ---------- Phase 3a: dedup ----------


class TestDedup:
    @pytest.mark.asyncio
    async def test_skips_already_seen_urls(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "dedup_main.db")

        articles = [
            _article(article_id="a-1", url="https://example.com/seen"),
            _article(article_id="a-2", url="https://example.com/fresh"),
        ]
        # 1 件目を seen 登録
        repo.mark_url_seen(
            url_hash=url_hash("https://example.com/seen"),
            url="https://example.com/seen",
        )
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="medium")])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
        )

        # 1 件は skip、もう 1 件だけ summarize/post される
        assert result.skipped_dup == 1
        assert result.summarized == 1
        assert result.posted == 1
        assert result.total_fetched == 2

    @pytest.mark.asyncio
    async def test_marks_url_seen_after_post(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "dedup_seen.db")
        url = "https://example.com/new"
        articles = [_article(article_id="a-1", url=url)]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
        )

        # 投稿成功した URL は seen に登録される
        assert repo.is_url_seen(url_hash(url)) is True


# ---------- Phase 5L-8: cross-channel dedup-skipped article の正しいハンドリング ----------


class TestCrossChannelDedupSkipPersistAndMarkRead:
    """Phase 5L-8 回帰テスト:

    - cross-channel dedup で skip された article が articles テーブルに
      ``status='skipped_duplicate'`` で永続化される (Bug 1)
    - skip された article が Inoreader mark_as_read 対象に含まれる (Bug 2)
    """

    @pytest.mark.asyncio
    async def test_cross_ch_dedup_skip_persists_and_marks_read(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import ArticleRecord, RunHistoryRepository

        repo = RunHistoryRepository(db_path=tmp_path / "ph5l8_dedup.db")

        # run 1 (前提): 同 dedup_key の article を 48h 以内に投稿済として作る
        # run_history.runs に親 run を作る必要があるため start_run 経由
        from datetime import UTC, datetime

        from src.storage.run_history import RunRecord

        prior_run_id = repo.start_run(
            RunRecord(
                started_at=datetime.now(UTC),
                pipeline="prior",
                triggered_by="manual",
                dry_run=False,
                status="succeeded",
            ),
        )
        repo.add_article(
            ArticleRecord(
                run_id=prior_run_id,
                article_id="prev-art",
                title="prev",
                url="https://example.com/prev",
                status="posted",
                posted_channel="brief",
                dedup_key="cve-2026-9999",
            ),
        )

        # run 2 (対象): 同じ dedup_key を持つ新 article (LLM 出力経由)
        new_url = "https://example.com/new"
        articles = [_article(article_id="new-art", url=new_url)]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        # LLM が dedup_key=cve-2026-9999 を出力 (Phase 5L-3 の routing_flags 経由)
        llm = _build_llm_with_outputs(
            [
                _summary_output(
                    title_ja="CVE-2026-9999 詳細",
                ),
            ],
        )
        # SummaryOutput.routing_flags に dedup_key を載せる
        # _build_llm_with_outputs の戻り値を上書きするのが面倒なので
        # _summary_output 用にカスタム fixture を作るより簡単な方法:
        # 記事タイトルに CVE-ID を含めることで _build_briefing の compute_dedup_key
        # フォールバックで cve-2026-9999 を生成させる
        articles_with_cve = [
            _article(
                article_id="new-art",
                url=new_url,
                title="Apache CVE-2026-9999 RCE",
            ),
        ]
        source = _build_source(articles_with_cve)
        publishers = _build_publishers()

        # 対象 run の親 run record も先に作る (FK 制約のため)
        target_run_id = repo.start_run(
            RunRecord(
                started_at=datetime.now(UTC),
                pipeline="daily-briefing",
                triggered_by="manual",
                dry_run=False,
                status="running",
            ),
        )
        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=target_run_id,
        )
        # 投稿は 0 件 (cross-channel dedup で skip)
        assert result.posted == 0

        # Bug 1 修正検証: articles テーブルに status='skipped_duplicate' で永続化
        rows = repo.list_articles(run_id=target_run_id)
        skipped_recs = [r for r in rows if r.article_id == "new-art"]
        assert len(skipped_recs) == 1
        assert skipped_recs[0].status == "skipped_duplicate"
        assert skipped_recs[0].dedup_key == "cve-2026-9999"


# ---------- Phase 3b: semantic dedup ----------


class TestSemanticDedup:
    @pytest.mark.asyncio
    async def test_skips_articles_with_similar_existing_embedding(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository
        from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "semantic.db")
        # 既存記事 (URL 違う、内容ほぼ同じ) の embedding を仕込んでおく
        existing_url = "https://en.example.com/breach-1"
        existing_h = url_hash(existing_url)
        repo.mark_url_seen(url_hash=existing_h, url=existing_url, title="EN breach")
        repo.add_article_embedding(
            url_hash=existing_h,
            url=existing_url,
            vector=[1.0, 0.0, 0.0, 0.0],
            model="m",
            title="EN breach",
        )

        # 新着記事 (URL 違う日本語版) の embedding は cosine ~ 0.99 に近い
        new_article = _article(
            article_id="ja-1",
            url="https://ja.example.com/breach-1",
            title="同じインシデントの日本語版",
        )

        embedder = AsyncMock(spec=EmbeddingClient)
        embedder.embed = AsyncMock(
            return_value=EmbeddingResponse(
                vector=(0.99, 0.01, 0.0, 0.0),
                model="m",
                dim=4,
            ),
        )
        source = _build_source([new_article])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        # Phase 5L-2: 2 段階閾値。cluster 0.9 を超える 0.99 はヒットさせて skip
        custom_proc = ProcessorConfig(
            similarity_threshold_hard=0.99,
            similarity_threshold_cluster=0.9,
            dedup_window_hours_hard=168,
            dedup_window_hours_cluster=48,
        )
        custom_pipeline = PipelineConfig(
            name=pipeline_cfg.name,
            source=pipeline_cfg.source,
            processor=custom_proc,
        )

        result = await run_pipeline(
            config=app_cfg,
            pipeline=custom_pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            embedder=embedder,
        )

        # cos 類似度 ~ 0.99 で cluster threshold (0.9) を超え skip される
        assert result.skipped_dup == 1
        assert result.summarized == 0
        assert result.posted == 0
        # embed は 1 回呼ばれた (記事自身の embedding)
        embedder.embed.assert_called_once()
        # 意味的重複で skip した記事も mark_as_read 対象に含まれる

    @pytest.mark.asyncio
    async def test_keeps_articles_below_threshold_and_persists_embedding(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository
        from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "semantic2.db")
        # 既存記事は完全に直交した embedding を持つ (cos = 0)
        existing_url = "https://other.example.com/x"
        existing_h = url_hash(existing_url)
        repo.mark_url_seen(url_hash=existing_h, url=existing_url, title="other")
        repo.add_article_embedding(
            url_hash=existing_h,
            url=existing_url,
            vector=[1.0, 0.0],
            model="m",
            title="other",
        )

        new_article = _article(
            article_id="fresh-1",
            url="https://example.com/fresh",
            title="fresh news",
        )
        embedder = AsyncMock(spec=EmbeddingClient)
        embedder.embed = AsyncMock(
            return_value=EmbeddingResponse(
                vector=(0.0, 1.0),  # 直交 → cos = 0
                model="m",
                dim=2,
            ),
        )
        source = _build_source([new_article])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        # 直交ベクトル (cos=0) で hard/cluster いずれもヒットさせない閾値
        custom_proc = ProcessorConfig(
            similarity_threshold_hard=0.5,
            similarity_threshold_cluster=0.4,
            dedup_window_hours_hard=168,
            dedup_window_hours_cluster=48,
        )
        custom_pipeline = PipelineConfig(
            name=pipeline_cfg.name,
            source=pipeline_cfg.source,
            processor=custom_proc,
        )
        result = await run_pipeline(
            config=app_cfg,
            pipeline=custom_pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            embedder=embedder,
        )

        assert result.skipped_dup == 0
        assert result.posted == 1
        # 投稿成功した記事の embedding が永続化されている
        new_h = url_hash(new_article.url)
        loaded = repo.get_embedding(new_h)
        assert loaded is not None
        assert pytest.approx(loaded.tolist()) == [0.0, 1.0]

    @pytest.mark.asyncio
    async def test_embedding_failure_keeps_article_via_graceful_degradation(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository
        from src.tools.embedding_client import (
            EmbeddingClient,
            EmbeddingError,
        )

        repo = RunHistoryRepository(db_path=tmp_path / "semantic3.db")
        article = _article(article_id="a-1", url="https://example.com/")

        embedder = AsyncMock(spec=EmbeddingClient)
        embedder.embed = AsyncMock(side_effect=EmbeddingError("ollama down"))
        source = _build_source([article])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            embedder=embedder,
        )

        # embedding が落ちても記事は処理される (graceful degradation)
        assert result.posted == 1
        assert result.skipped_dup == 0

    @pytest.mark.asyncio
    async def test_grok_article_skips_cluster_tier_bypass(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        """Phase 5T-L: Grok article は cluster tier dedup を bypass される。

        毎日同じテンプレで類似の article が必然的に発生するため、article 単位
        cluster 判定が誤発火する。hard tier (0.92) のみで判定する。
        """
        from src.storage.run_history import RunHistoryRepository
        from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "grok_bypass.db")
        existing_url = "https://grok.com/chat/old-report"
        existing_h = url_hash(existing_url)
        repo.mark_url_seen(url_hash=existing_h, url=existing_url, title="old grok")
        repo.add_article_embedding(
            url_hash=existing_h,
            url=existing_url,
            vector=[1.0, 0.0, 0.0, 0.0],
            model="m",
            title="old grok",
        )

        # 新着 Grok article。cluster 判定 (cosine ~ 0.85 > 0.82) には match
        # するが hard 判定 (0.92) には届かない → bypass で keep される想定
        new_grok = _article(
            article_id="grok:abc:1",
            url="https://grok.com/chat/new-report",
            title="新 Grok レポート",
        )

        embedder = AsyncMock(spec=EmbeddingClient)
        embedder.embed = AsyncMock(
            return_value=EmbeddingResponse(
                vector=(0.85, 0.5267, 0.0, 0.0),  # cosine ~ 0.85 vs 既存
                model="m",
                dim=4,
            ),
        )
        source = _build_source([new_grok])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        custom_proc = ProcessorConfig(
            similarity_threshold_hard=0.92,
            similarity_threshold_cluster=0.82,
            dedup_window_hours_hard=168,
            dedup_window_hours_cluster=48,
        )
        custom_pipeline = PipelineConfig(
            name=pipeline_cfg.name,
            source=pipeline_cfg.source,
            processor=custom_proc,
        )

        result = await run_pipeline(
            config=app_cfg,
            pipeline=custom_pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            embedder=embedder,
        )

        # Grok 経路は cluster bypass で skip されない (hard も該当しない)。
        # 本文が stub のため Grok parser 経由で posted=0 になるが、dedup の挙動
        # (skipped_dup) のみが本テストの対象。survivors として扱われたことを確認。
        assert result.skipped_dup == 0

    @pytest.mark.asyncio
    async def test_grok_article_still_caught_by_hard_tier(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        """Phase 5T-L: Grok でも hard tier (0.92) では skip される (真の content コピー)。"""
        from src.storage.run_history import RunHistoryRepository
        from src.tools.embedding_client import EmbeddingClient, EmbeddingResponse
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "grok_hard.db")
        existing_url = "https://grok.com/chat/old-report"
        existing_h = url_hash(existing_url)
        repo.mark_url_seen(url_hash=existing_h, url=existing_url, title="old grok")
        repo.add_article_embedding(
            url_hash=existing_h,
            url=existing_url,
            vector=[1.0, 0.0, 0.0, 0.0],
            model="m",
            title="old grok",
        )

        new_grok = _article(
            article_id="grok:abc:2",
            url="https://grok.com/chat/dup-report",
            title="ほぼ完全コピー",
        )

        embedder = AsyncMock(spec=EmbeddingClient)
        embedder.embed = AsyncMock(
            return_value=EmbeddingResponse(
                vector=(0.99, 0.141, 0.0, 0.0),  # cosine ~ 0.99 > hard 0.92
                model="m",
                dim=4,
            ),
        )
        source = _build_source([new_grok])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        custom_proc = ProcessorConfig(
            similarity_threshold_hard=0.92,
            similarity_threshold_cluster=0.82,
            dedup_window_hours_hard=168,
            dedup_window_hours_cluster=48,
        )
        custom_pipeline = PipelineConfig(
            name=pipeline_cfg.name,
            source=pipeline_cfg.source,
            processor=custom_proc,
        )

        result = await run_pipeline(
            config=app_cfg,
            pipeline=custom_pipeline,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            embedder=embedder,
        )

        # Grok でも hard tier は機能、真の content コピーは skip される。
        # skipped_dup=1 で dedup-stage で skip されたことを確認 (posted は当然 0)。
        assert result.skipped_dup == 1


# ---------- 重要度 → チャンネルマッピング (Phase 5I: SSoT は config_loader) ----------


class TestImportanceRouting:
    """Phase 5I: IMPORTANCE_TO_CHANNEL 定数廃止後、_default_importance_map() が SSoT。"""

    def test_all_importances_mapped(self) -> None:
        from src.config_loader import _default_importance_map

        m = _default_importance_map()
        assert set(m.keys()) == {"high", "medium", "low"}

    def test_priority_for_high(self) -> None:
        from src.config_loader import _default_importance_map

        assert _default_importance_map()["high"] == "alert"

    def test_daily_for_medium(self) -> None:
        from src.config_loader import _default_importance_map

        assert _default_importance_map()["medium"] == "brief"

    def test_research_for_low(self) -> None:
        from src.config_loader import _default_importance_map

        assert _default_importance_map()["low"] == "watch"


# ---------- Phase 5C: target_channel routing + system 通知 ----------


class TestTargetChannelOverride:
    """metadata['target_channel'] が importance_map より優先されることを検証。"""

    @pytest.mark.asyncio
    async def test_target_channel_in_metadata_routes_to_specified_channel(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        from src.main import _resolve_channel
        from src.tools.discord_publisher import BriefingMessage

        msg = BriefingMessage(
            title="t",
            bluf="b",
            importance="high",  # 重要度 high なら本来は priority
            category="china_apt",
            summary="s",
            metadata={"target_channel": "brief"},  # daily に明示指定
        )
        importance_map: dict[Literal["high", "medium", "low"], str] = {
            "high": "alert",
            "medium": "brief",
            "low": "watch",
        }
        # target_channel が importance_map より優先される
        assert _resolve_channel(msg, importance_map) == "brief"

    @pytest.mark.asyncio
    async def test_invalid_target_channel_falls_back_to_importance_map(self) -> None:
        from src.main import _resolve_channel
        from src.tools.discord_publisher import BriefingMessage

        msg = BriefingMessage(
            title="t",
            bluf="b",
            importance="medium",
            category="x",
            summary="s",
            metadata={"target_channel": "bogus"},
        )
        importance_map: dict[Literal["high", "medium", "low"], str] = {
            "high": "alert",
            "medium": "brief",
            "low": "watch",
        }
        assert _resolve_channel(msg, importance_map) == "brief"


class TestSystemNotification:
    """Phase 5C: system チャンネルへの稼働ステータス通知。"""

    @pytest.mark.asyncio
    async def test_success_posts_one_line_heartbeat(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [_article(article_id="a-1", url="https://x.com/1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="high")])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )
        # system チャンネルに 1 件の heartbeat
        assert _post_mock(publishers["ops"]).call_count == 1
        msg_arg = _post_mock(publishers["ops"]).call_args.args[0]
        assert msg_arg.importance == "low"
        assert "完了" in msg_arg.title or "完了" in msg_arg.bluf

    @pytest.mark.asyncio
    async def test_dry_run_skips_system_notification(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [_article(article_id="a-1", url="https://x.com/1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="high")])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=True,
        )
        # dry-run は system 通知しない
        assert _post_mock(publishers["ops"]).call_count == 0

    @pytest.mark.asyncio
    async def test_source_fetch_failure_posts_failure_notification(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # source.fetch が例外を上げる
        source = AsyncMock()
        source.fetch.side_effect = RuntimeError("network down")
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=AsyncMock(),
            llm=AsyncMock(),
            publishers=publishers,
            template=template,
            dry_run=False,
        )
        assert result.errors
        # 失敗時は @here 付きで system に通知
        assert _post_mock(publishers["ops"]).call_count == 1
        msg_arg = _post_mock(publishers["ops"]).call_args.args[0]
        assert "@here" in msg_arg.bluf
        assert msg_arg.importance == "high"

    @pytest.mark.asyncio
    async def test_disabled_routing_skips_system_notification(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        from src.config_loader import ChannelRouting

        articles = [_article(article_id="a-1", url="https://x.com/1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="high")])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            channel_routing=ChannelRouting(system_notify_enabled=False),
        )
        # system_notify_enabled=False なら通知しない
        assert _post_mock(publishers["ops"]).call_count == 0


# ---------- Phase 5D: sort + dedup + system color ----------


class TestOutcomePostedChannelPairing:
    """Phase 5J-1: sort で並び替えても outcome の posted_channel が正しい msg と対応すること。

    バグ再現: _sort_briefings_for_posting で priority→daily→research に並び替えた後、
    元順の article_outcomes を idx で引くと別 briefing の channel が記録される。
    """

    @pytest.mark.asyncio
    async def test_posted_channel_recorded_for_each_msg_after_sort(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # 元順: low → high → medium。sort 後: high (priority) → medium (daily) → low (research)
        articles = [
            _article(article_id="a-low", url="https://x.com/l"),
            _article(article_id="a-high", url="https://x.com/h"),
            _article(article_id="a-med", url="https://x.com/m"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results(
            [_extraction_success() for _ in articles],
        )
        llm = _build_llm_with_outputs(
            [
                _summary_output(importance="low"),
                _summary_output(importance="high"),
                _summary_output(importance="medium"),
            ],
        )
        publishers = _build_publishers()

        # ArticleRecord 永続化を spy するため RunHistoryRepository を mock
        from unittest.mock import MagicMock

        repo = MagicMock()
        repo.filter_unseen_hashes.return_value = set()
        # Phase 5L-4: cross-channel dedup mock を明示的に None に
        repo.find_recent_article_by_dedup_key.return_value = None

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=1,
        )
        # add_article 呼び出しから posted_channel を抽出 (article_id ごと)
        recorded: dict[str, str] = {}
        for call in repo.add_article.call_args_list:
            rec = call.args[0]
            recorded[rec.article_id] = rec.posted_channel
        # Phase 5K: 中立 fixture (Japan/KEV/known_apt なし) は importance + advisory
        # category により: high/medium → brief, low → watch
        assert recorded["a-high"] == "brief"
        assert recorded["a-med"] == "brief"
        assert recorded["a-low"] == "watch"


class TestSortBriefingsForPosting:
    """投稿順序を (priority → daily → research) で固定する (E)。"""

    def test_channel_order_is_priority_daily_research(self) -> None:
        from src.main import _sort_briefings_for_posting
        from src.tools.discord_publisher import BriefingMessage

        importance_map: dict[Literal["high", "medium", "low"], str] = {
            "high": "alert",
            "medium": "brief",
            "low": "watch",
        }
        msgs = [
            (
                "a-low",
                BriefingMessage(
                    title="t",
                    bluf="b",
                    importance="low",
                    category="x",
                    summary="s",
                    metadata={"target_channel": "watch"},
                ),
            ),
            (
                "a-high",
                BriefingMessage(
                    title="t",
                    bluf="b",
                    importance="high",
                    category="x",
                    summary="s",
                    metadata={"target_channel": "alert"},
                ),
            ),
            (
                "a-med",
                BriefingMessage(
                    title="t",
                    bluf="b",
                    importance="medium",
                    category="x",
                    summary="s",
                    metadata={"target_channel": "brief"},
                ),
            ),
        ]
        sorted_msgs = _sort_briefings_for_posting(msgs, importance_map)
        ids = [aid for aid, _ in sorted_msgs]
        assert ids == ["a-high", "a-med", "a-low"]

    def test_relevance_score_secondary_sort_within_channel(self) -> None:
        """同一 channel 内では relevance_score 降順。"""
        from src.main import _sort_briefings_for_posting
        from src.tools.discord_publisher import BriefingMessage

        importance_map: dict[Literal["high", "medium", "low"], str] = {
            "high": "alert",
            "medium": "brief",
            "low": "watch",
        }
        msgs = [
            (
                "a-low-rel",
                BriefingMessage(
                    title="t",
                    bluf="b",
                    importance="medium",
                    category="x",
                    summary="s",
                    metadata={"target_channel": "brief", "relevance_score": 3},
                ),
            ),
            (
                "a-high-rel",
                BriefingMessage(
                    title="t",
                    bluf="b",
                    importance="medium",
                    category="x",
                    summary="s",
                    metadata={"target_channel": "brief", "relevance_score": 9},
                ),
            ),
        ]
        sorted_msgs = _sort_briefings_for_posting(msgs, importance_map)
        assert [aid for aid, _ in sorted_msgs] == ["a-high-rel", "a-low-rel"]


class TestDedupBySourceUrl:
    """Phase 5D / H: cross-task の同一 X 投稿 URL を 1 件にまとめる。"""

    def test_state_apt_wins_over_x_early_signals(self) -> None:
        from src.main import _dedup_briefings_by_source_url
        from src.tools.discord_publisher import (
            BriefingIncident,
            BriefingMessage,
            Source,
        )

        url = "https://x.com/example/status/1"
        msg_xes = BriefingMessage(
            title="t",
            bluf="b",
            importance="low",
            category="vuln_chatter",
            summary="s",
            metadata={"grok_task_id": "x_early_signals"},
            incidents=[
                BriefingIncident(
                    heading="事象 1",
                    body="x",
                    sources=[Source(title="X", url=url, language="ja")],
                )
            ],
        )
        msg_state = BriefingMessage(
            title="t",
            bluf="b",
            importance="high",
            category="china_apt",
            summary="s",
            metadata={"grok_task_id": "state_apt"},
            incidents=[
                BriefingIncident(
                    heading="事象 1",
                    body="y",
                    sources=[Source(title="X", url=url, language="ja")],
                )
            ],
        )
        # xes が先、state_apt が後
        kept, dropped = _dedup_briefings_by_source_url(
            [
                ("a-xes", msg_xes),
                ("a-state", msg_state),
            ]
        )
        # state_apt が深さ優先で残る
        assert dropped == 1
        kept_ids = [aid for aid, _ in kept]
        assert "a-state" in kept_ids
        assert "a-xes" not in kept_ids


class TestSystemNotificationColors:
    """Phase 5D / 7: system 通知は 🟢 (成功) / 🔴 (失敗)。"""

    @pytest.mark.asyncio
    async def test_success_uses_green_circle(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [_article(article_id="a-1", url="https://x.com/1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="high")])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )
        msg_arg = _post_mock(publishers["ops"]).call_args.args[0]
        assert "🟢" in msg_arg.title  # 🟢

    @pytest.mark.asyncio
    async def test_failure_uses_red_circle(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        source = AsyncMock()
        source.fetch.side_effect = RuntimeError("network down")
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=AsyncMock(),
            llm=AsyncMock(),
            publishers=publishers,
            template=template,
            dry_run=False,
        )
        msg_arg = _post_mock(publishers["ops"]).call_args.args[0]
        assert "🔴" in msg_arg.title  # 🔴
        assert "@here" in msg_arg.bluf

    def test_system_message_uses_plaintext_only(self) -> None:
        """Phase 5D / 7: system 通知は plain-text content のみで投稿 (embed なし)。"""
        from unittest.mock import MagicMock, patch

        from src.tools.discord_publisher import BriefingMessage, DiscordPublisher

        msg = BriefingMessage(
            title="🟢 daily-briefing 完了",
            bluf="daily-briefing 完了 · 取得 3 / 投稿 3",
            importance="low",
            category="system",  # system 通知の semantic marker (channel 名 "ops" とは別概念)
            summary="daily-briefing 完了 · 取得 3 / 投稿 3",
            metadata={"target_channel": "ops"},
        )
        with patch("src.tools.discord_publisher.DiscordWebhook") as mock_wh:
            instance = MagicMock()
            mock_wh.return_value = instance
            pub = DiscordPublisher(webhook_url="https://discord.com/api/webhooks/x/y")
            pub._build_webhooks(msg)
            # set_content は呼ばれるが add_embed は呼ばれない (plain-text only)
            instance.set_content.assert_called_once()
            instance.add_embed.assert_not_called()


# ---------- SummaryOutput の検証 ----------


class TestSummaryOutput:
    def test_valid_payload(self) -> None:
        out = SummaryOutput(
            title_ja="日本語訳タイトル",
            bluf="x",
            importance="medium",
            category="apt",
            summary="y",
        )
        assert out.iocs == []
        assert out.mitre_techniques == []
        assert out.analyst_note is None

    def test_invalid_importance_rejected(self) -> None:
        with pytest.raises(Exception):  # noqa: B017, BLE001
            SummaryOutput(
                title_ja="t",
                bluf="x",
                importance="critical",  # type: ignore[arg-type]
                category="apt",
                summary="y",
            )

    def test_bluf_now_optional_phase_5t_o(self) -> None:
        """Phase 5T-O: bluf field は optional になった (Inoreader 経路で title と重複していたため)。
        空文字でも construct 可能 (旧テスト test_empty_bluf_rejected を反転)。"""
        out = SummaryOutput(
            title_ja="t",
            bluf="",
            importance="low",
            category="other",
            summary="y",
        )
        assert out.bluf == ""

    def test_bluf_default_empty(self) -> None:
        """Phase 5T-O: bluf を渡さなくても construct できる (default は空文字)。"""
        out = SummaryOutput(
            title_ja="t",
            importance="low",
            category="other",
            summary="y",
        )
        assert out.bluf == ""

    def test_title_ja_required(self) -> None:
        """Phase 5J-2: title_ja は required。LLM が省略すると validation error。"""
        with pytest.raises(Exception):  # noqa: B017, BLE001
            SummaryOutput(  # type: ignore[call-arg]  # title_ja 欠落を意図的に検証
                bluf="x",
                importance="low",
                category="other",
                summary="y",
            )

    def test_title_ja_in_required_schema(self) -> None:
        """Phase 5J-2: JSON Schema の required 配列に title_ja が含まれる。"""
        schema = SummaryOutput.model_json_schema()
        assert "title_ja" in schema.get("required", [])


# ---------- Phase 5A fix: run_id 経由の articles テーブル永続化 ----------


class TestArticlePersistenceWithRunId:
    """run_id を渡すと dashboard / 履歴用の articles テーブルに 1 行ずつ書く。"""

    @pytest.mark.asyncio
    async def test_writes_article_records_for_each_posted_briefing(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository, RunRecord

        repo = RunHistoryRepository(db_path=tmp_path / "articles.db")
        run_id = repo.start_run(
            RunRecord(
                started_at=datetime(2026, 5, 2, tzinfo=UTC),
                pipeline="daily-briefing",
                dry_run=False,
                triggered_by="manual",
            ),
        )

        articles = [
            _article(article_id="a-high", url="https://x.com/h"),
            _article(article_id="a-med", url="https://x.com/m"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 2)
        llm = _build_llm_with_outputs(
            [
                _summary_output(importance="high", category="apt"),
                _summary_output(importance="medium", category="advisory"),
            ],
        )
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=run_id,
        )

        records = repo.list_articles(run_id=run_id)
        assert len(records) == 2
        statuses = {r.article_id: r.status for r in records}
        assert statuses == {"a-high": "posted", "a-med": "posted"}
        # 重要度 / カテゴリも保存されているのでダッシュボードの集計が動く
        importances = {r.article_id: r.importance for r in records}
        assert importances == {"a-high": "high", "a-med": "medium"}
        # posted_channel も埋まっている (Phase 5K: 中立 fixture は brief 集約)
        channels = {r.article_id: r.posted_channel for r in records}
        assert channels == {"a-high": "brief", "a-med": "brief"}

    @pytest.mark.asyncio
    async def test_records_summarize_failure_for_failed_article(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from src.storage.run_history import RunHistoryRepository, RunRecord

        repo = RunHistoryRepository(db_path=tmp_path / "articles_fail.db")
        run_id = repo.start_run(
            RunRecord(
                started_at=datetime(2026, 5, 2, tzinfo=UTC),
                pipeline="daily-briefing",
                dry_run=False,
                triggered_by="manual",
            ),
        )

        articles = [
            _article(article_id="ok-1", url="https://x.com/1"),
            _article(article_id="bad-2", url="https://x.com/2"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 2)

        llm = AsyncMock()
        llm.generate_structured.side_effect = _stance_aware_gen(
            [_summary_output(importance="medium"), RuntimeError("LLM failed")]
        )
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=run_id,
        )

        records = {r.article_id: r for r in repo.list_articles(run_id=run_id)}
        assert records["ok-1"].status == "posted"
        assert records["bad-2"].status == "summarize_failed"
        assert records["bad-2"].failure_reason is not None
        assert "RuntimeError" in records["bad-2"].failure_reason

    @pytest.mark.asyncio
    async def test_no_run_id_skips_article_persistence(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        """run_id を渡さない場合は articles テーブルに何も書かない (CLI 互換)。"""
        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository(db_path=tmp_path / "noid.db")
        articles = [_article(article_id="a-1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=None,
        )

        assert repo.list_articles() == []


class TestPersistArticleEntitiesCVE:
    """Phase B-cal: CVE entity 永続バグの回帰テスト。

    briefing/summarizer.j2 は CVE を ``iocs`` 配列に出力するが、旧実装は ``mitre_techniques``
    しか見ず ``_classify_ioc_type`` にも cve 分岐が無かったため CVE が捨てられていた
    (監査: 322 記事中 cve entity=1)。iocs / summary 由来の CVE を拾えることを確認。
    """

    def test_cve_from_iocs_is_persisted(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.main import _persist_article_entities
        from src.storage.run_history import RunHistoryRepository
        from src.tools.discord_publisher import BriefingMessage

        repo = RunHistoryRepository(db_path=tmp_path / "cve.db")
        msg = BriefingMessage(
            title="t",
            importance="high",
            category="vulnerability",
            summary="本文では CVE-2026-99999 にも言及。",
            iocs=["CVE-2026-12345", "203.0.113.5", "evil.example.com"],
            mitre_techniques=["T1190"],
        )
        _persist_article_entities(dedup_repo=repo, article_id="a-cve", msg=msg)
        ents: dict[str, set[str]] = {}  # entity_type -> set(values)
        for et, v in repo.get_entities_by_article("a-cve"):
            ents.setdefault(et, set()).add(v)

        # iocs 内の CVE が entity_type='cve' で永続化される
        assert "CVE-2026-12345" in ents.get("cve", set())
        # iocs が CVE を curate 済みなら summary 本文の CVE は補完しない
        # (narrative summary が言及する過去事案 CVE の誤帰属を防ぐ。CVE 誤帰属 fix)。
        assert "CVE-2026-99999" not in ents.get("cve", set())
        # IP / domain / ttp も従来どおり
        assert "203.0.113.5" in ents.get("ioc_ip", set())
        assert "T1190" in ents.get("ttp", set())
        # CVE は ioc_domain に誤分類されない
        assert all("CVE-" not in v for v in ents.get("ioc_domain", set()))

    def test_cve_summary_fallback_only_when_iocs_empty(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """iocs/mitre が CVE を 1 件も拾えなかった時のみ summary regex で補完する。"""
        from src.main import _persist_article_entities
        from src.storage.run_history import RunHistoryRepository
        from src.tools.discord_publisher import BriefingMessage

        repo = RunHistoryRepository(db_path=tmp_path / "cve2.db")
        msg = BriefingMessage(
            title="t",
            importance="high",
            category="vulnerability",
            summary="本件の脆弱性 CVE-2026-55555 が悪用された。",
            iocs=["203.0.113.5"],  # CVE は iocs に無い
            mitre_techniques=["T1190"],
        )
        _persist_article_entities(dedup_repo=repo, article_id="a-cve2", msg=msg)
        cves = {v for et, v in repo.get_entities_by_article("a-cve2") if et == "cve"}
        # iocs が CVE を持たないので summary fallback が効く
        assert "CVE-2026-55555" in cves


class TestIntraBatchUrlDedup:
    """Phase B-cal: 同一 run 内で同 URL が複数 feed から入っても 1 件に絞る。"""

    def test_same_url_in_batch_deduped(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.main import _filter_duplicates
        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository(db_path=tmp_path / "batch.db")
        # 同一 URL を別 article_id (別 feed 購読を模擬) で 2 件 + 別 URL 1 件
        arts = [
            _article(article_id="a1", url="https://blog.example.com/post"),
            _article(article_id="a2", url="https://blog.example.com/post"),
            _article(article_id="a3", url="https://other.example.com/x"),
        ]
        survivors, skipped, skipped_ids = _filter_duplicates(arts, repo)
        assert skipped == 1
        assert "a2" in skipped_ids
        urls = {a.url for a in survivors}
        assert urls == {"https://blog.example.com/post", "https://other.example.com/x"}
        assert len(survivors) == 2


class TestTerminalFailureSeenMark:
    """Phase B: summarize/extract 失敗の URL を seen 登録し RSS 再取得ループを防ぐ。"""

    def test_summarize_failed_url_marked_seen(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.main import _persist_article_outcomes
        from src.storage.run_history import RunHistoryRepository, RunRecord
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "seen.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        art = _article(article_id="f1", url="https://feed.example.com/broken")
        _persist_article_outcomes(
            outcomes=[
                {
                    "article_id": "f1",
                    "msg": None,
                    "status": "summarize_failed",
                    "failure_reason": "x",
                }
            ],
            articles_by_id={"f1": art},
            dedup_repo=repo,
            run_id=run_id,
        )
        # 失敗 URL が seen 登録され、次回 dedup で除外される
        assert repo.is_url_seen(url_hash("https://feed.example.com/broken"))

    def test_post_failed_url_not_marked_seen(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """post_failed (transient) は seen 登録しない (再試行余地を残す)。"""
        from src.main import _persist_article_outcomes
        from src.storage.run_history import RunHistoryRepository, RunRecord
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "seen2.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        art = _article(article_id="f2", url="https://feed.example.com/discord-down")
        _persist_article_outcomes(
            outcomes=[
                {"article_id": "f2", "msg": None, "status": "post_failed", "failure_reason": "x"}
            ],
            articles_by_id={"f2": art},
            dedup_repo=repo,
            run_id=run_id,
        )
        assert not repo.is_url_seen(url_hash("https://feed.example.com/discord-down"))


class TestComposeDailyBrief:
    """朝刊/夕刊: daily brief 合成 (純粋関数)。morning=synthesis+PIR / evening=synthesis のみ。"""

    def test_morning_combines_narrative_and_pir(self) -> None:
        from src.main import _compose_daily_brief

        msg = _compose_daily_brief(
            slot="morning",
            narrative="**状況総括 headline**\n\nチェーン...",
            pir_body="📍 PIR Daily Focus — 2026-06-28\n### 中国系 APT ...",
            sources=[],
            period_label="2026-06-28",
            section_count=3,
        )
        assert msg is not None
        assert msg.title.startswith("朝ブリーフィング")  # 朝刊
        assert "状況総括 headline" in msg.summary
        assert "PIR Daily Focus" in msg.summary
        assert msg.metadata["target_channel"] == "brief"
        assert msg.metadata["brief_slot"] == "morning"
        assert msg.metadata["pir_section_count"] == 3

    def test_evening_synthesis_only(self) -> None:
        from src.main import _compose_daily_brief

        msg = _compose_daily_brief(
            slot="evening",
            narrative="**夕方の状況総括**\n\n更新...",
            pir_body="",  # 夕刊は PIR focus なし
            sources=[],
            period_label="2026-06-28",
            section_count=0,
        )
        assert msg is not None
        assert msg.title.startswith("夕ブリーフィング")  # 夕刊
        assert "夕方の状況総括" in msg.summary
        assert msg.metadata["brief_slot"] == "evening"

    def test_none_when_both_empty(self) -> None:
        from src.main import _compose_daily_brief

        assert (
            _compose_daily_brief(
                slot="morning",
                narrative="",
                pir_body="   ",
                sources=[],
                period_label="2026-06-28",
                section_count=0,
            )
            is None
        )


class TestComposeCompactSummary:
    """Discord 要点射影 (2026-07-12): 全文 push をやめ 要点 + Web 誘導の 1 通にする。"""

    def test_appends_web_link_when_base_url_resolved(self) -> None:
        from src.pipeline.runners import _compose_compact_summary

        text = _compose_compact_summary(
            narrative="**headline**\n\n■ 比重\n軍事軸が突出",
            pir_body="🔴 **中国 APT** (3 match) — 要点",
            base_url="https://ten-goal-but-extend.trycloudflare.com",
        )
        assert "**headline**" in text
        assert "中国 APT" in text
        assert "https://ten-goal-but-extend.trycloudflare.com/app/daily-brief" in text

    def test_falls_back_to_text_without_base_url(self) -> None:
        from src.pipeline.runners import _compose_compact_summary

        text = _compose_compact_summary(
            narrative="**headline**",
            pir_body="",
            base_url=None,
        )
        assert "https://" not in text
        assert "日次ブリーフ" in text  # リンク切れリスクゼロの文言 fallback

    def test_evening_without_pir_body(self) -> None:
        from src.pipeline.runners import _compose_compact_summary

        text = _compose_compact_summary(
            narrative="**夕方の状況**",
            pir_body="",
            base_url="https://cti.example.com",
        )
        assert text.startswith("**夕方の状況**")
        assert "https://cti.example.com/app/daily-brief" in text

    def test_high_threats_lead_before_synthesis(self) -> None:
        # 高脅威 Recall 安全網 (2026-07-16): 「act now」tier を synthesis/PIR より先頭に。
        from src.pipeline.runners import _compose_compact_summary

        text = _compose_compact_summary(
            narrative="**headline**",
            pir_body="🔴 **中国 APT**",
            high_threats="🔴 **本日の高脅威 (要確認 · 3 件)**\n・脅威A (APT)",
            base_url=None,
        )
        assert text.index("本日の高脅威") < text.index("headline") < text.index("中国 APT")


class TestWebOnlyDisposition:
    """通知再設計: channel push 属性による web-only disposition (push 抑止して DB 保存のみ)。

    制御点は channel レジストリの push 属性 (情報フローで編集)。投稿ループは push_map() を読み、
    push=False の tier を Discord push せず status='posted' で保存する。
    """

    @pytest.mark.asyncio
    async def test_push_false_channels_skip_push_but_keep_heartbeat(
        self,
        monkeypatch: pytest.MonkeyPatch,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # 中立 fixture (Japan/KEV/known_apt なし) は alert に行かず brief/watch に流れる。
        # それらを push=False にすると Discord push が全て抑止される (DB 保存は継続)。
        pm = {"alert": True, "brief": False, "watch": False, "japan_watch": False, "ops": True}
        monkeypatch.setattr("src.pipeline.orchestrator.push_map", lambda **_: pm)
        articles = [
            _article(article_id="a-high", url="https://x.com/h"),
            _article(article_id="a-med", url="https://x.com/m"),
            _article(article_id="a-low", url="https://x.com/l"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success() for _ in articles])
        llm = _build_llm_with_outputs(
            [
                _summary_output(importance="high"),
                _summary_output(importance="medium"),
                _summary_output(importance="low"),
            ],
        )
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )

        content_calls = (
            _post_mock(publishers["alert"]).call_count
            + _post_mock(publishers["brief"]).call_count
            + _post_mock(publishers["watch"]).call_count
        )
        assert content_calls == 0  # 全 article が web-only に抑止された
        assert result.posted == 0  # Discord push は 0
        assert result.summarized == 3  # ただし要約・保存はされている
        assert _post_mock(publishers["ops"]).call_count == 1  # heartbeat は従来どおり

    @pytest.mark.asyncio
    async def test_all_push_true_keeps_pushing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # 全 channel push=True (既定) → 従来どおり配信される。
        pm = {"alert": True, "brief": True, "watch": True, "japan_watch": True, "ops": True}
        monkeypatch.setattr("src.pipeline.orchestrator.push_map", lambda **_: pm)
        articles = [_article(article_id="a-low", url="https://x.com/l")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output(importance="low")])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
        )

        # 全 push=True → 従来どおり watch に push される
        assert result.posted == 1
        assert _post_mock(publishers["watch"]).call_count == 1


class TestCapVulnImportanceAuditP1:
    """監査 2026-07-05 P1: cap regex の誤発火/素通しと KEV カタログ非参照の修正。"""

    def test_hypothetical_akuyo_boilerplate_is_downgraded(self) -> None:
        # JVN/IPA 定型の仮定文「悪用された場合」で high が温存されていた (裸の「悪用」match)
        result = _cap_vuln_importance(
            "high",
            "vulnerability",
            "XX 製品に脆弱性",
            "本脆弱性が悪用された場合、任意のコードが実行されるおそれがあります。修正済み。",
        )
        assert result == "medium"

    def test_bare_english_exploited_hypothetical_is_downgraded(self) -> None:
        # "could be exploited" (仮定文) で high 温存されていた
        result = _cap_vuln_importance(
            "high",
            "vulnerability",
            "Vulnerability in X",
            "The flaw could be exploited to execute arbitrary code. A patch is available.",
        )
        assert result == "medium"

    def test_zdi_advisory_name_is_not_zero_day_signal(self) -> None:
        # 固有名詞「Zero Day Initiative」が 0day シグナル扱いされていた
        result = _cap_vuln_importance(
            "high",
            "advisory",
            "Zero Day Initiative advisory ZDI-26-123 for printer flaw",
            "Patched in latest firmware.",
        )
        assert result == "medium"

    def test_factual_akuyo_sarete_iru_keeps_high(self) -> None:
        result = _cap_vuln_importance("high", "vulnerability", "この脆弱性は既に悪用されている", "")
        assert result == "high"

    def test_kev_catalog_cve_keeps_high_without_keywords(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 本文に魔法語が無くても KEV 掲載 CVE を含むなら high 維持 (Recall 側の穴の修正)
        import src.tools.kev_client as kev_client

        monkeypatch.setattr(kev_client, "any_cve_on_kev", lambda cves: True)
        result = _cap_vuln_importance(
            "high",
            "vulnerability",
            "CVE-2026-12345 の修正がリリース",
            "落ち着いたパッチ解説記事。",
        )
        assert result == "high"

    def test_non_kev_cve_without_signal_still_downgraded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.tools.kev_client as kev_client

        monkeypatch.setattr(kev_client, "any_cve_on_kev", lambda cves: False)
        result = _cap_vuln_importance(
            "high", "vulnerability", "CVE-2026-99999 の修正", "パッチ解説。"
        )
        assert result == "medium"


class TestTransientFailureSeenMark:
    """監査 2026-07-05 P1: LLM の一時障害 (timeout/接続断) は seen 登録しない。

    Ollama 停止時間帯に fetch された記事が summarize_failed → 恒久 seen 化で
    永久ロストしていた。transient は次 run の RSS 窓で再取得させる。
    """

    def test_transient_llm_failure_not_marked_seen(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        from src.main import _persist_article_outcomes
        from src.storage.run_history import RunHistoryRepository, RunRecord
        from src.tools.url_normalizer import url_hash

        repo = RunHistoryRepository(db_path=tmp_path / "seen3.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        art = _article(article_id="f3", url="https://feed.example.com/ollama-down")
        _persist_article_outcomes(
            outcomes=[
                {
                    "article_id": "f3",
                    "msg": None,
                    "status": "summarize_failed",
                    "failure_reason": "LLMTimeoutError: Ollama 推論タイムアウト (300.0s)",
                    "transient_failure": True,
                }
            ],
            articles_by_id={"f3": art},
            dedup_repo=repo,
            run_id=run_id,
        )
        assert not repo.is_url_seen(url_hash("https://feed.example.com/ollama-down"))


class TestDiamondMetadataRegression:
    """監査 2026-07-05 P1: P4 コミットのインデント混入で rationale が remediation 存在時
    のみ永続化される退行 (96fde6c) の回帰固定。"""

    def test_rationale_persists_without_remediation(self) -> None:
        article = _article(title="APT article", url="https://x.com/apt")
        summary = SummaryOutput(
            title_ja="APT 記事",
            bluf="b",
            importance="high",
            category="apt",
            summary="s",
            diamond={
                "socio_political": {
                    "intent": "espionage",
                    "rationale": "中国系 APT が知財窃取",
                    "confidence": "high",
                },
                "technical": "spear phishing",
            },
            remediation=None,  # remediation が無くても rationale は保存される
        )
        msg = _build_briefing(article, summary)
        assert msg.metadata.get("socio_political_rationale") == "中国系 APT が知財窃取"
        assert msg.metadata.get("socio_political_intent") == "espionage"

    def test_remediation_still_persisted_when_present(self) -> None:
        article = _article(title="Vuln", url="https://x.com/vuln")
        summary = SummaryOutput(
            title_ja="脆弱性記事",
            bluf="b",
            importance="high",
            category="vulnerability",
            summary="s",
            remediation="1.10.1 へ更新しプロセスを再起動",
        )
        msg = _build_briefing(article, summary)
        assert msg.metadata.get("remediation") == "1.10.1 へ更新しプロセスを再起動"


class TestDegenerateBodyGate:
    """監査 #10 / 2026-07-05 実測病理: 読めていない本文から要約を作らせない。

    ボット対策画面のみの記事がタイトルだけの LLM 要約 → 配信 → 台帳開設 → headline
    占有まで到達した。ゴミ入力は境界 (要約前) で止める。
    """

    def test_block_page_detected(self) -> None:
        from src.main import _degenerate_body_reason

        body = (
            "This website uses a security service to protect against malicious bots. "
            "Please enable JavaScript and cookies to continue. " * 3
        )
        assert _degenerate_body_reason(body) == "block_page"

    def test_too_short_body_detected(self) -> None:
        from src.main import _degenerate_body_reason

        assert _degenerate_body_reason("Description:\nLeak Screenshot:") is not None
        assert _degenerate_body_reason("") is not None

    def test_healthy_body_passes(self) -> None:
        from src.main import _degenerate_body_reason

        assert _degenerate_body_reason("実質的な記事本文。" * 30) is None

    def test_long_article_mentioning_cloudflare_terms_passes(self) -> None:
        # Cloudflare 等について書かれた長文記事はブロック画面ではない
        from src.main import _degenerate_body_reason

        body = (
            "Cloudflare reported that attackers abuse checking your browser pages... "
            + "long analysis body. " * 200
        )
        assert _degenerate_body_reason(body) is None

    @pytest.mark.asyncio
    async def test_process_article_raises_on_degenerate_body(self) -> None:
        from src.main import DegenerateBodyError, _process_article

        article = _article(article_id="blocked", url="https://blocked.example/x")
        extractor = _build_extractor_with_results(
            [
                _extraction_success(
                    "Verify you are human. Enable JavaScript and cookies "
                    "to continue browsing this site safely."
                )
            ]
        )
        with pytest.raises(DegenerateBodyError):
            await _process_article(article, extractor, AsyncMock(), AsyncMock())


class TestGrokSubArticlePersistence:
    """2026-07-05 実測退行の回帰固定: Grok per-tweet briefing の DB 永続化。

    65c5dc4 (6/17 per-tweet 一意 sub_id 導入) が sub_id を articles_by_id_local に
    登録し忘れ、_persist_article_outcomes が全 sub を「記事不明」で skip — Grok tweet
    が articles テーブル (web UI/検索/entity 層) から 3 週間消えていた。6/28 の
    watch web-only 化以降は Discord にも出ないため完全不可視だった。
    """

    @pytest.mark.asyncio
    async def test_grok_sub_briefings_are_persisted(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        tmp_path: Any,
    ) -> None:
        from datetime import timedelta

        from src.storage.run_history import RunHistoryRepository, RunRecord

        # posted_at は「now 基準の相対時刻」にする (固定日付だと実時間経過で jsonl parser の
        # too-old filter に落ち、briefing 0 件 → 永続化テストが日付で壊れる)。
        recent_iso = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        jsonl = (
            '{"tweet_id":"9001","url":"https://x.com/test/status/9001",'
            '"author_handle":"@test","author_name":"Test",'
            f'"posted_at":"{recent_iso}","lang":"en",'
            '"text":"APT29 targets EU ministries with new backdoor",'
            '"is_retweet":false,"retweeted_tweet_id":null,"is_quote":false,'
            '"quoted_tweet_id":null,"quoted_text":null,"reply_to_tweet_id":null,'
            '"media_urls":[],"external_urls":[],'
            '"engagement":{"like":10,"retweet":5,"quote":0,"reply":0},'
            '"matched_theme":"B"}'
        )
        # JSONL は summary_html 経由で供給される (grok source が DOM 抽出を格納する場所)
        grok = _article(
            article_id="grok:persisttest:1",
            url="https://grok.com/chat/persist-test",
            title="Grok Report",
            summary_html=jsonl,
        )

        repo = RunHistoryRepository(db_path=tmp_path / "grok_persist.db")
        run_id = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="grok-briefing", dry_run=False)
        )
        source = _build_source([grok])
        extractor = _build_extractor_with_results([_extraction_success()])
        llm = _build_llm_with_outputs([_summary_output()])
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            dry_run=False,
            dedup_repo=repo,
            run_id=run_id,
        )
        assert result.summarized >= 1
        records = repo.list_articles(run_id=run_id)
        sub_ids = [r.article_id for r in records if "#" in r.article_id]
        assert sub_ids, (
            "Grok per-tweet briefing が articles に永続化されていない "
            f"(records={[r.article_id for r in records]})"
        )


# ---------- 時間予算 (soft deadline) / per-article timeout (2026-08-01) ----------


class TestRunPipelineTimeBudget:
    """timeout kill による成果全損ループの防止 (run 3520 対応)。

    - soft deadline: 予算超過で残記事を次 run へ繰越 (既読化せずリトライ権保持)
    - per-article timeout: 単一の病的記事が run の予算を専有しない
    """

    @pytest.mark.asyncio
    async def test_soft_deadline_defers_all_articles(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        # Arrange: fetch が予算 (0.01s) を確実に食い潰す
        import asyncio as _asyncio

        articles = [
            _article(article_id="a-1", url="https://x.com/1"),
            _article(article_id="a-2", url="https://x.com/2"),
            _article(article_id="a-3", url="https://x.com/3"),
        ]

        async def _slow_fetch(max_count: int | None = None) -> list[Article]:
            await _asyncio.sleep(0.1)
            return articles

        source = AsyncMock()
        source.fetch.side_effect = _slow_fetch
        extractor = _build_extractor_with_results([_extraction_success()] * 3)
        llm = _build_llm_with_outputs([_summary_output()] * 3)
        publishers = _build_publishers()

        # Act
        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            time_budget_seconds=0.01,
        )

        # Assert: 全記事が繰越、run 自体は正常完了 (errors なし)
        assert result.deferred_count == 3
        assert result.summarized == 0
        assert result.posted == 0
        assert result.errors == []
        # 繰越記事は LLM 処理に到達しない
        assert cast(AsyncMock, llm.generate_structured).call_count == 0

    @pytest.mark.asyncio
    async def test_generous_budget_processes_all(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
    ) -> None:
        articles = [
            _article(article_id="a-1", url="https://x.com/1"),
            _article(article_id="a-2", url="https://x.com/2"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 2)
        llm = _build_llm_with_outputs([_summary_output()] * 2)
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
            time_budget_seconds=3600.0,
        )

        assert result.deferred_count == 0
        assert result.summarized == 2
        assert result.posted == 2
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_per_article_timeout_marks_transient_and_continues(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Arrange: 1 件目の LLM 呼出が per-article timeout (0.05s) を超えて hang
        import asyncio as _asyncio

        monkeypatch.setenv("PER_ARTICLE_TIMEOUT_SECONDS", "0.05")
        articles = [
            _article(article_id="slow-1", url="https://x.com/1"),
            _article(article_id="ok-2", url="https://x.com/2"),
        ]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()] * 2)

        _fast = _stance_aware_gen([_summary_output()])
        _call_state = {"first": True}

        async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
            if _call_state["first"]:
                _call_state["first"] = False
                await _asyncio.sleep(1.0)  # timeout を確実に超える
            return await _fast(prompt, schema=schema, **kw)

        llm = AsyncMock()
        llm.generate_structured.side_effect = _gen
        publishers = _build_publishers()

        # Act
        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
        )

        # Assert: 1 件目は TimeoutError で失敗、2 件目は正常処理される
        assert result.summarized == 1
        assert result.posted == 1
        assert any("slow-1" in e and "TimeoutError" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_per_article_timeout_disabled_by_env_zero(
        self,
        template: jinja2.Template,
        pipeline_cfg: PipelineConfig,
        app_cfg: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # env 0 で無効化 → 遅い記事も待って正常処理する
        import asyncio as _asyncio

        monkeypatch.setenv("PER_ARTICLE_TIMEOUT_SECONDS", "0")
        articles = [_article(article_id="slow-1", url="https://x.com/1")]
        source = _build_source(articles)
        extractor = _build_extractor_with_results([_extraction_success()])

        _fast = _stance_aware_gen([_summary_output()])
        _call_state = {"first": True}

        async def _gen(prompt: object, schema: object = None, **kw: object) -> object:
            if _call_state["first"]:
                _call_state["first"] = False
                await _asyncio.sleep(0.1)
            return await _fast(prompt, schema=schema, **kw)

        llm = AsyncMock()
        llm.generate_structured.side_effect = _gen
        publishers = _build_publishers()

        result = await run_pipeline(
            config=app_cfg,
            pipeline=pipeline_cfg,
            source=source,
            extractor=extractor,
            llm=llm,
            publishers=publishers,
            template=template,
        )

        assert result.summarized == 1
        assert result.errors == []
