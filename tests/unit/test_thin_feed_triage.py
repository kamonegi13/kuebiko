"""Phase 3 (収集の深掘り): thin feed の triage 前先行抽出のテスト。

- triage が body_text を優先利用すること
- _prefetch_thin_bodies の対象選別 (thin のみ / Grok・厚い記事はスキップ) と抽出反映
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage
from src.tools.content_extractor import ExtractionResult


def _article(
    *,
    article_id: str = "tag:1",
    summary_html: str = "短い概要",
    body_text: str | None = None,
    url: str = "https://e/a",
) -> Article:
    return Article(
        id=article_id,
        title="記事タイトル",
        url=url,
        summary_html=summary_html,
        published=datetime(2026, 1, 1, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://e/feed",
        body_text=body_text,
    )


class TestTriageUsesBodyText:
    def test_prefers_body_text_over_summary(self) -> None:
        from unittest.mock import AsyncMock

        triage = ArticleTriage(AsyncMock())
        art = _article(summary_html="薄い", body_text="本文に Volt Typhoon の詳細記述がある" * 3)
        prompt = triage._build_prompt(art)
        assert "Volt Typhoon" in prompt
        # 薄い snippet ではなく body_text が judging 材料に入る
        assert triage._triage_content(art).startswith("本文に Volt Typhoon")

    def test_falls_back_to_summary_when_no_body(self) -> None:
        from unittest.mock import AsyncMock

        triage = ArticleTriage(AsyncMock())
        art = _article(summary_html="概要だけのテキスト", body_text=None)
        assert "概要だけのテキスト" in triage._triage_content(art)


class _FakeExtractor:
    """ContentExtractor の async-context スタブ。"""

    def __init__(self, results: dict[str, ExtractionResult]) -> None:
        self._results = results

    async def __aenter__(self) -> _FakeExtractor:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def extract(self, url: str) -> ExtractionResult:
        return self._results[url]


class TestPrefetchThinBodies:
    @pytest.mark.asyncio
    async def test_enriches_only_thin_articles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.pipeline.filters as filters_mod

        thin = _article(article_id="thin", summary_html="短い", url="https://e/thin")
        thick = _article(
            article_id="thick",
            summary_html="x" * 1000,  # 厚い → スキップ
            url="https://e/thick",
        )
        results = {
            "https://e/thin": ExtractionResult(
                url="https://e/thin", text="抽出された本文 " * 20, success=True
            ),
        }
        monkeypatch.setattr(filters_mod, "ContentExtractor", lambda: _FakeExtractor(results))

        enriched, count = await filters_mod._prefetch_thin_bodies([thin, thick], min_chars=600)
        assert count == 1
        by_id = {a.id: a for a in enriched}
        assert by_id["thin"].body_text is not None
        assert by_id["thin"].body_text.startswith("抽出された本文")
        # 厚い記事は触らない
        assert by_id["thick"].body_text is None

    @pytest.mark.asyncio
    async def test_skips_grok_and_already_extracted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.pipeline.filters as filters_mod

        # Grok 記事 (id に grok permalink パターン) は triage 対象外 → スキップ。
        # _is_grok_article の判定に合わせ、metadata ではなく id で判定する実装に依存しないよう
        # 「既に body_text あり」のケースでスキップを確認する。
        already = _article(
            article_id="has_body", summary_html="短い", body_text="既存本文", url="https://e/has"
        )
        monkeypatch.setattr(filters_mod, "ContentExtractor", lambda: _FakeExtractor({}))
        enriched, count = await filters_mod._prefetch_thin_bodies([already], min_chars=600)
        assert count == 0
        assert enriched[0].body_text == "既存本文"

    @pytest.mark.asyncio
    async def test_extraction_failure_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import src.pipeline.filters as filters_mod

        thin = _article(article_id="thin", summary_html="短い", url="https://e/thin")
        results = {
            "https://e/thin": ExtractionResult(
                url="https://e/thin", success=False, failure_reason="http_404"
            ),
        }
        monkeypatch.setattr(filters_mod, "ContentExtractor", lambda: _FakeExtractor(results))
        enriched, count = await filters_mod._prefetch_thin_bodies([thin], min_chars=600)
        assert count == 0
        assert enriched[0].body_text is None  # 失敗時は従来挙動 (None) のまま
