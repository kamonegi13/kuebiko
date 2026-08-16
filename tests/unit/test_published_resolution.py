"""placeholder 記事への公開日の充当と、その妥当性ガードのテスト (2026-08-16)。

核心の不変量:
- 実公開日が判れば置き換えて旗を下ろす
- 判らない / 妥当域を外れるなら **代用値のまま旗も立てたまま** にする
  (確信をもって誤るより「判らない」を保つ)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.pipeline.briefing import MAX_BACKDATE_DAYS, _resolve_published
from src.tools.article_model import Article
from src.tools.content_extractor import ExtractionResult

INGESTED = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def _article(*, placeholder: bool) -> Article:
    return Article(
        id="scraper:abc",
        title="t",
        url="https://example.com/a",
        summary_html="",
        published=INGESTED,
        published_is_placeholder=placeholder,
        feed_title="f",
        feed_url="https://example.com/",
    )


def _extraction(published: datetime | None) -> ExtractionResult:
    return ExtractionResult(
        url="https://example.com/a", success=True, text="body", published_date=published
    )


class TestResolvePublished:
    def test_plausible_date_replaces_and_clears_flag(self) -> None:
        real = INGESTED - timedelta(days=3)

        out = _resolve_published(_article(placeholder=True), _extraction(real))

        assert out.published == real
        assert out.published_is_placeholder is False

    def test_non_placeholder_is_never_touched(self) -> None:
        """source が実公開日を持っていた記事 (RSS 等) は抽出で上書きしない。"""
        out = _resolve_published(
            _article(placeholder=False), _extraction(INGESTED - timedelta(days=900))
        )

        assert out.published == INGESTED
        assert out.published_is_placeholder is False

    def test_missing_date_keeps_placeholder(self) -> None:
        out = _resolve_published(_article(placeholder=True), _extraction(None))

        assert out.published == INGESTED
        assert out.published_is_placeholder is True

    def test_naive_extracted_date_is_treated_as_utc(self) -> None:
        naive = (INGESTED - timedelta(days=2)).replace(tzinfo=None)

        out = _resolve_published(_article(placeholder=True), _extraction(naive))

        assert out.published == INGESTED - timedelta(days=2)


class TestPlausibilityGuard:
    def test_future_date_is_rejected(self) -> None:
        future = INGESTED + timedelta(days=30)

        out = _resolve_published(_article(placeholder=True), _extraction(future))

        assert out.published == INGESTED
        assert out.published_is_placeholder is True, "棄却時は旗を残す"

    def test_absurdly_old_date_is_rejected(self) -> None:
        """実測: Push Security の記事が 1,863 日前と抽出された (誤読)。"""
        misparse = INGESTED - timedelta(days=1863)

        out = _resolve_published(_article(placeholder=True), _extraction(misparse))

        assert out.published == INGESTED
        assert out.published_is_placeholder is True

    def test_boundary_just_inside_is_accepted(self) -> None:
        edge = INGESTED - timedelta(days=MAX_BACKDATE_DAYS - 1)

        out = _resolve_published(_article(placeholder=True), _extraction(edge))

        assert out.published == edge

    def test_small_clock_skew_is_tolerated(self) -> None:
        """TZ 表記ゆれで数時間先になる程度は誤りとしない。"""
        skewed = INGESTED + timedelta(hours=6)

        out = _resolve_published(_article(placeholder=True), _extraction(skewed))

        assert out.published == skewed
