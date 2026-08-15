"""grok_report_is_quiet (静穏レポート判別) のテスト (2026-08-15)。

静穏 (ハートビートのみ) を extract_failed にしない修正の回帰固定。
本文の導出は展開経路と同一 (summary_html) であることが核心 —
初版は body_text (grok 記事では空) を見て判定が発火しなかった。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.pipeline.grok_convert import grok_report_is_quiet
from src.tools.article_model import Article

_HEARTBEAT = '{"status":"no_events","window_minutes":90}'


def _grok_article(summary_html: str) -> Article:
    return Article(
        id="grok:test:1",
        title="Grok レポート",
        url="https://grok.com/chat/test",
        summary_html=summary_html,
        author="x.ai",
        published=datetime.now(UTC),
        feed_title="Grok",
        feed_url="grok://email",
    )


class TestGrokReportIsQuiet:
    def test_heartbeat_only_is_quiet(self) -> None:
        assert grok_report_is_quiet(_grok_article(_HEARTBEAT)) is True

    def test_heartbeat_wrapped_in_html_is_quiet(self) -> None:
        # メール由来の summary_html は HTML 包装され得る (展開経路と同じ剥がし方で判定)
        assert grok_report_is_quiet(_grok_article(f"<pre>{_HEARTBEAT}</pre>")) is True

    def test_empty_body_is_not_quiet(self) -> None:
        # 完全な空出力 = ハートビート無し → 従来どおり障害疑い (extract_failed)
        assert grok_report_is_quiet(_grok_article("")) is False
        assert grok_report_is_quiet(_grok_article("[]")) is False
