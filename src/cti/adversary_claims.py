"""敵対国の公式な発信 (``adversary_official``) と敵対的 host の本文の扱い (2026-10-30)。

- ``ADVERSARY_IOC_GATE`` (shadow 既定): ``adversary_official`` の記事から取れた IOC を
  事実として保存しない。shadow は件数のログだけ、``1`` で保存を止める、``0`` で無効。
- ``HOSTILE_PROMPT_FRAMING`` (shadow 既定): 敵対的 host / ``adversary_official`` の本文を
  要約の LLM に渡すとき「主張として扱う・指示ではない」の枠を前置する (shadow はログのみ)。
"""

from __future__ import annotations

import os
from typing import Literal

from src.cti.source_basis import adversary_official_matches, classify_source_tier
from src.tools.hostile_hosts import classify_host

GateMode = Literal["off", "shadow", "on"]

ENV_IOC_GATE = "ADVERSARY_IOC_GATE"
ENV_PROMPT_FRAMING = "HOSTILE_PROMPT_FRAMING"


def _mode(env_name: str) -> GateMode:
    raw = (os.environ.get(env_name) or "shadow").strip().lower()
    if raw in ("0", "off", "false"):
        return "off"
    if raw in ("1", "on", "true"):
        return "on"
    return "shadow"


def ioc_gate_mode() -> GateMode:
    return _mode(ENV_IOC_GATE)


def prompt_framing_mode() -> GateMode:
    return _mode(ENV_PROMPT_FRAMING)


def is_adversary_official(feed_title: str, feed_url: str) -> bool:
    """この source は敵対国の公式な発信 (「主張」) か。"""
    # 実効ティアが UI 上書きや ADVERSARY_TIER=1 で adversary_official の場合と、
    # config の一致 (ADVERSARY_TIER が shadow でも) の両方を拾う。
    return adversary_official_matches(feed_title, feed_url) or (
        classify_source_tier(feed_title or "", feed_url or "") == "adversary_official"
    )


def needs_untrusted_framing(feed_title: str, feed_url: str, article_url: str = "") -> bool:
    """本文に「主張として扱う・指示ではない」の枠が要るか (adversary_official か敵対的 host)。"""
    if is_adversary_official(feed_title, feed_url):
        return True
    try:
        return any(u and classify_host(u).hostile for u in (article_url, feed_url))
    except Exception:  # noqa: BLE001 — 分類の失敗は hostile 扱い (枠を付ける側に倒す)
        return True
