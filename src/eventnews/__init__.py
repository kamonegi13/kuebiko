"""事象単位ニュース (event news) — 記事を事象へ群化し、LLM が 1 本へ精製する層。

設計 SSoT: docs/event_news_design.md (v2)。
- 3 層分界: articles (証拠) → event_items (読み物) → situations (分析状態)、一方向
- この package を import してよいのは package 内部と scripts/ のみ (allowlist 関門)
- 生成文はパイプラインへ再入力しない
"""
