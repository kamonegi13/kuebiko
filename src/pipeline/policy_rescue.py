"""サイバー政策の救済 (2026-10-10)。

平たい triage (関連性を見ない) は、重要インフラ防護・暗号・AI 安全性などの政策・法制度の記事を
low にして落とす。これらはサイバー情勢に繋がる情報なので、素の 26B に「話題の種類」だけを
閉じた選択肢で判定させ、cyber_policy なら medium で拾う (high にはしない)。
サイバー要素のない軍事・地政学 (noncyber_geopolitics) は救わない (09-29 の利用者決定)。
学習済みの triage に欄を足すと崩れるため、別の素モデルの分類器にしている。
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from src.logging_config import get_logger
from src.tools.article_model import Article
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

POLICY_RESCUE_MARK = "[サイバー政策の救済] "
_CONCURRENCY = 2
_MAX_TOKENS = 200
#: 影子モードで 1 run に判定する上限 (本処理の遅延を防ぐ)。on では全件
_SHADOW_MAX_PER_RUN = 30

Topic = Literal["cyber_event", "cyber_policy", "noncyber_geopolitics", "other"]

_PROMPT = (
    "あなたは CTI アナリストです。次の記事の**話題の種類**を 1 つ選んでください。"
    "重要度や自分との関係ではなく、記事が何について書かれているかだけで決めます。\n\n"
    "- cyber_event: 攻撃・侵害・脆弱性・マルウェア・摘発など、サイバーの事象を報じている\n"
    "- cyber_policy: サイバーセキュリティ・重要インフラ防護・暗号・AI の安全性や悪用・"
    "技術安全保障に関する政策・法制度・戦略・計画・業界の取り組み・標準・資金\n"
    "- noncyber_geopolitics: サイバーの要素がない軍事・外交・地政学・国内政治\n"
    "- other: 上のいずれでもない (経済・文化・科学・製品など)\n\n"
    "フィード: {feed}\nタイトル: {title}\n\n"
    "topic と reason (1 文) を返す。"
)


class TopicDecision(BaseModel):
    topic: Topic
    reason: str


_ENV = "CYBER_POLICY_RESCUE"
SHADOW_LOG = Path("data/policy_rescue_shadow.jsonl")


def policy_rescue_mode() -> Literal["off", "shadow", "on"]:
    """既定 off。shadow = 判定して記録するだけで取り込みは変えない。影子を 7 日以上見てから on。"""
    raw = os.environ.get(_ENV, "0").strip().lower()
    if raw == "1":
        return "on"
    return "shadow" if raw == "shadow" else "off"


def _append_shadow(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    try:
        SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_LOG.open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    except OSError as e:
        _log.warning("policy_rescue_shadow_write_failed", error=str(e)[:120])


async def rescue_cyber_policy(
    decisions: list[tuple[Article, str, bool, str]],
    llm: LLMClient,
    *,
    shadow: bool = False,
) -> tuple[list[tuple[Article, str, bool, str]], set[str]]:
    """low と判定された記事のうち、話題が cyber_policy のものを medium に引き上げる。
    判定に失敗したら元の判定のまま。shadow=True なら判定を記録するだけで返り値は元のまま。"""
    targets = [i for i, (_a, imp, err, _r) in enumerate(decisions) if imp == "low" and not err]
    if shadow:
        targets = targets[:_SHADOW_MAX_PER_RUN]
    if not targets:
        return decisions, set()
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(i: int) -> tuple[int, TopicDecision | None]:
        article = decisions[i][0]
        async with sem:
            try:
                d = await llm.generate_structured(
                    _PROMPT.format(feed=article.feed_title or "", title=article.title or ""),
                    schema=TopicDecision,
                    think=False,
                    max_tokens=_MAX_TOKENS,
                )
            except Exception as e:  # noqa: BLE001 — 救済の失敗は元の判定を残す
                _log.warning("policy_rescue_failed", article_id=article.id, error=str(e)[:120])
                return i, None
            return i, d

    out = list(decisions)
    rescued: set[str] = set()
    shadow_rows: list[dict[str, str]] = []
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for i, d in await asyncio.gather(*(_one(i) for i in targets)):
        if d is None:
            continue
        article = out[i][0]
        shadow_rows.append(
            {
                "at": stamp,
                "article_id": article.id,
                "feed": article.feed_title or "",
                "title": (article.title or "")[:160],
                "topic": d.topic,
                "reason": d.reason[:200],
            }
        )
        if d.topic != "cyber_policy":
            continue
        out[i] = (article, "medium", False, POLICY_RESCUE_MARK + d.reason)
        rescued.add(article.id)
    _log.info("policy_rescue", candidates=len(targets), rescued=len(rescued), shadow=shadow)
    if shadow:
        _append_shadow(shadow_rows)
        return decisions, set()
    return out, rescued
