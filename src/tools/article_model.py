"""記事 1 件の正規化モデル (source 横断で共有)。

Phase X-1 で Inoreader 廃止と並行して inoreader_client.py から切り出し。
RSS / Grok / web scraper など全 source は本モデルに変換した上で
``run_pipeline()`` に渡す。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class Article(BaseModel):
    """1 件の記事を表す source-agnostic な正規化モデル。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    title: str
    url: str
    summary_html: str
    author: str | None = None
    published: datetime
    feed_title: str
    feed_url: str
    # Phase 3 (収集の深掘り): triage 前に先行抽出した本文 (trafilatura)。thin feed の
    # triage 精度向上のため、ここに本文があれば triage / 本処理がこれを優先利用する
    # (本処理での再抽出を回避)。通常の fetch 時点では None。
    body_text: str | None = None
