"""検証・プレビュー・LLM dry-run で共有する固定サンプル記事。

実運用の記事を使うと「編集内容の影響」と「記事の違い」が混ざるため、プロンプトの
比較には常に同一の入力を使う。CLAUDE.md §4 / CLAUDE.local.md の規律に従い、実在の
組織名・実インシデント・実ドメインは書かない (fixture ドメインは kuebiko.example)。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.tools.article_model import Article

SAMPLE_BODY = (
    "A threat actor tracked as SAMPLE-ACTOR deployed new command-and-control "
    "infrastructure targeting manufacturing suppliers, according to a report "
    "published this week.\n\n"
    "The operators exploited an internet-facing application flaw (CVE-2026-00000) "
    "to obtain initial access, then used a signed remote monitoring tool for "
    "persistence. Analysts observed HTTPS beaconing to c2.kuebiko.example and "
    "credential dumping on domain controllers.\n\n"
    "The vendor released version 1.2.3 with a fix and recommends blocking the "
    "published indicators until the update is applied."
)

SAMPLE_ARTICLE = Article(
    id="sample-article-0001",
    title="Threat actor deploys new C2 infrastructure against manufacturing suppliers",
    url="https://news.kuebiko.example/articles/sample-c2-infrastructure",
    summary_html="<p>Sample article used for prompt preview and validation.</p>",
    author=None,
    published=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    feed_title="Kuebiko Sample Feed",
    feed_url="https://news.kuebiko.example/feed.xml",
    body_text=SAMPLE_BODY,
)
