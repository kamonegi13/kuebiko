"""Grok JSONL TweetRecord → BriefingMessage 変換 + Discord routing。

slot 1 (X Native Signal): theme A-F の per-tweet record を Discord channel に routing。
slot 2 (JP East Asia Signal): theme J1-J6 同様。

各 TweetRecord は 1 つの BriefingMessage に変換される (1 tweet = 1 投稿)。
複数 source からの同一事案は pipeline 下流の dedup_seen_urls で対応。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.grok.jsonl_parser import TweetRecord
from src.logging_config import get_logger
from src.tools.discord_publisher import (
    BriefingMessage,
    Source,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class ThemeRouting:
    """1 theme の routing 設定。"""

    code: str  # "A"-"F" or "J1"-"J6"
    label: str
    importance: str  # "high" / "medium" / "low"
    channel: str  # "alert" / "brief" / "watch" / "japan_watch" 等
    badge: str


# Slot 1: X Native Signal (global, A-F)
_SLOT1_ROUTING: dict[str, ThemeRouting] = {
    "A": ThemeRouting(
        code="A",
        label="Vendor 事前 teaser / 初出言及",
        importance="high",
        channel="alert",
        badge="🔬",
    ),
    "B": ThemeRouting(
        code="B",
        label="リークサイト掲載速報",
        importance="medium",
        channel="watch",
        badge="🏴‍☠️",
    ),
    "C": ThemeRouting(
        code="C",
        label="法執行アクション速報",
        importance="high",
        channel="alert",
        badge="👮",
    ),
    "D": ThemeRouting(
        code="D",
        label="被害組織 自己開示",
        importance="high",
        channel="alert",
        badge="🚨",
    ),
    "E": ThemeRouting(
        code="E",
        label="Scan / honeypot 異常観測",
        importance="medium",
        channel="brief",
        badge="📡",
    ),
    "F": ThemeRouting(
        code="F",
        label="Active exploitation 確証",
        importance="high",
        channel="alert",
        badge="🔥",
    ),
}

# Slot 2: JP / East Asia Signal (J1-J6)
_SLOT2_ROUTING: dict[str, ThemeRouting] = {
    "J1": ThemeRouting(
        code="J1",
        label="日本企業 breach/incident",
        importance="high",
        channel="japan_watch",
        badge="🇯🇵",
    ),
    "J2": ThemeRouting(
        code="J2",
        label="JPCERT/IPA cyber 注意喚起",
        importance="high",
        channel="alert",
        badge="📢",
    ),
    "J3": ThemeRouting(
        code="J3",
        label="日本人研究者 technical 解析",
        importance="medium",
        channel="watch",
        badge="🔬",
    ),
    "J4": ThemeRouting(
        code="J4",
        label="DPRK APT 動向",
        importance="high",
        channel="alert",
        badge="🇰🇵",
    ),
    "J5": ThemeRouting(
        code="J5",
        label="中国系 APT 動向",
        importance="high",
        channel="alert",
        badge="🇨🇳",
    ),
    "J6": ThemeRouting(
        code="J6",
        label="東アジア (TW/HK/KR) cyber 事件",
        importance="medium",
        channel="brief",
        badge="🌏",
    ),
}

# theme code → ThemeRouting 全 lookup
_ALL_ROUTING: dict[str, ThemeRouting] = {**_SLOT1_ROUTING, **_SLOT2_ROUTING}


def get_routing(theme: str) -> ThemeRouting | None:
    """theme code から ThemeRouting を取得 (未知の theme は None)。"""
    return _ALL_ROUTING.get(theme)


def _make_dedup_key(record: TweetRecord) -> str:
    """dedup_seen_urls 用の key を生成。

    優先:
    1. tweet 内に含まれる external_url (canonical URL)
    2. tweet URL 自体
    """
    if record.external_urls:
        return record.external_urls[0]
    return record.url


def _format_summary(record: TweetRecord, routing: ThemeRouting) -> str:
    """BriefingMessage.summary 用の本文を整形。

    Discord 表示で見やすいように:
    - 著者 + posted_at
    - 原文 (text)
    - external_urls / media_urls (短い list で末尾に)
    """
    posted_short = record.posted_at[:19] if len(record.posted_at) >= 19 else record.posted_at
    display_name = record.author_name or record.author_handle
    parts = [
        f"**{display_name}** ({record.author_handle}) — {posted_short} UTC",
        "",
        record.text,
    ]
    if record.is_quote and record.quoted_text:
        parts.append("")
        parts.append(f"> Quoted: {record.quoted_text[:200]}")
    if record.external_urls:
        parts.append("")
        parts.append("URLs:")
        for url in record.external_urls[:3]:
            parts.append(f"- {url}")
    if record.engagement.signal_score >= 50:
        parts.append("")
        parts.append(
            f"📊 engagement: {record.engagement.like} like / "
            f"{record.engagement.retweet} RT / {record.engagement.reply} reply",
        )
    return "\n".join(parts)


# title に載せる観測テキストの最大長 (digest のリンク文/embed title で読める範囲)
_TITLE_SNIPPET_MAX = 90

# 「内容を持つ」文字 (英数 / ひらがな / カタカナ / CJK / 半角カナ)。
# これを 1 つも含まないトークン = 絵文字・記号のみ = ノイズ。
_WORD_CHAR = re.compile(r"[0-9A-Za-z぀-ヿ㐀-鿿ｦ-ﾟ]")


def _is_noise_prefix_token(token: str) -> bool:
    """title snippet の先頭から落とすノイズトークンか判定。

    - ハッシュタグ (#/＃) / メンション (@) で始まる → ノイズ (本文中のタグは残す)
    - 内容文字を 1 つも含まない (絵文字・記号のみ) → ノイズ
    """
    if token[:1] in ("#", "＃", "@"):
        return True
    return not _WORD_CHAR.search(token)


def _clean_observation_text(text: str) -> str:
    """title snippet 用に観測テキスト先頭のノイズを除去する。

    X 投稿は "#Cyberattack alert" や "🚨🚨" のような見出し/装飾で始まることが
    あり、そのままだと title 先頭が無内容になる。先頭の連続するハッシュタグ /
    メンション / 絵文字のみトークンを落とし、本文を前に出す。
    本文中のタグは保持。全部ノイズなら原文 (空白正規化) に fallback。
    """
    one_line = " ".join(text.split())
    tokens = one_line.split(" ")
    idx = 0
    while idx < len(tokens) and _is_noise_prefix_token(tokens[idx]):
        idx += 1
    cleaned = " ".join(tokens[idx:]).strip()
    return cleaned or one_line


def _make_title(record: TweetRecord, routing: ThemeRouting) -> str:
    """BriefingMessage.title 用の見出し。

    観測内容 (tweet text) を主役にする。daily-focus digest は title だけを
    リンク文として表示するため、セクションラベルのみだと「何の事象か」が
    伝わらない。badge + code で領域を、text snippet で内容を、handle で
    出典を示す。

    例: "🇯🇵[J1] 芸術文化ホールで個人情報漏洩の可能性、Web サイト改ざん被害 (@piyokango)"
    text が空 (RT のみ等) の場合は従来どおりラベル見出しに fallback。
    """
    handle = record.author_handle
    if not handle.startswith("@"):
        handle = f"@{handle}"
    text_line = _clean_observation_text(record.text)
    if text_line:
        snippet = text_line[:_TITLE_SNIPPET_MAX]
        if len(text_line) > _TITLE_SNIPPET_MAX:
            snippet = snippet.rstrip() + "…"
        return f"{routing.badge}[{routing.code}] {snippet} ({handle})"
    return f"{routing.badge}[{routing.code}] {routing.label} — {handle}"


def _make_bluf(record: TweetRecord) -> str:
    """BLUF 1 行。tweet text の最初の 80 chars を取り、改行を空白に置換。"""
    one_line = record.text.replace("\n", " ").strip()
    if len(one_line) > 80:
        return one_line[:77] + "..."
    return one_line


def tweet_to_briefing(record: TweetRecord) -> BriefingMessage | None:
    """1 TweetRecord を 1 BriefingMessage に変換。

    matched_theme が未知の場合は None (skip)。
    """
    routing = get_routing(record.matched_theme)
    if routing is None:
        _log.warning(
            "jsonl_unknown_theme",
            tweet_id=record.tweet_id,
            theme=record.matched_theme,
        )
        return None

    sources = [
        Source(
            title=record.author_name or record.author_handle,
            url=record.url,
            language=record.lang or "auto",
        ),
    ]
    # external_urls も第 2 source として追加 (最大 3)
    for url in record.external_urls[:2]:
        sources.append(Source(title=record.author_handle, url=url, language="auto"))

    return BriefingMessage(
        title=_make_title(record, routing),
        bluf=_make_bluf(record),
        importance=routing.importance,  # type: ignore[arg-type]
        category=f"grok_x_signal_{routing.code.lower()}",
        summary=_format_summary(record, routing),
        sources=sources,
        metadata={
            "target_channel": routing.channel,
            "routing_reason": f"grok_jsonl:theme_{routing.code}",
            "routing_rule_id": "GROK_JSONL",
            "dedup_key": _make_dedup_key(record),
            "tweet_id": record.tweet_id,
            "author_handle": record.author_handle,
            "posted_at": record.posted_at,
            "lang": record.lang,
            "engagement_score": record.engagement.signal_score,
            "matched_theme": record.matched_theme,
        },
    )


def records_to_briefings(records: list[TweetRecord]) -> list[BriefingMessage]:
    """TweetRecord list を BriefingMessage list に変換。

    matched_theme が未知の record は skip。
    """
    briefings: list[BriefingMessage] = []
    for r in records:
        msg = tweet_to_briefing(r)
        if msg is not None:
            briefings.append(msg)
    _log.info(
        "jsonl_to_briefings_complete",
        input_count=len(records),
        output_count=len(briefings),
    )
    return briefings


__all__: list[str] = [
    "ThemeRouting",
    "get_routing",
    "records_to_briefings",
    "tweet_to_briefing",
]
