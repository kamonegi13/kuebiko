"""PIR Daily Focus pipeline (E1 後継、旧 research-digest の置き換え)。

各 enabled PIR の直近 24h match を 1 view に集約。Spotlight (週次 narrative)
の daily 簡易版として、朝 06:30 JST に brief ch へ 1 post。

- Spotlight: 1 PIR の戦術-作戦レベル深掘り (週次、LLM narrative)
- Daily Focus: 全 enabled PIR の 24h 状況一覧 (毎日、LLM 1-2 文 / PIR)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import jinja2

from src.config_loader import AppConfig, DiscordChannel, PipelineConfig
from src.logging_config import get_logger
from src.pir.evaluator import PirMatch, evaluate_pir_matches
from src.pir.loader import load_pir_config
from src.pir.models import Pir
from src.tools.discord_publisher import (
    BriefingMessage,
    DiscordPublisher,
    Source,
)
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

# 投稿先 (固定: brief ch、朝の通読用)
DAILY_FOCUS_TARGET_CHANNEL: DiscordChannel = "brief"

# 各 PIR section に含める article 件数
_MAX_ARTICLES_PER_PIR = 3

# LLM が "本日の要点" 1-2 文生成 (per-PIR で 1 call)
_PROMPT_TEMPLATE = "pir_daily_focus.j2"
_LLM_MAX_TOKENS = 256
_LLM_TEMPERATURE = 0.3


@dataclass(frozen=True)
class PirFocusSection:
    """1 PIR 分の daily focus section。"""

    pir: Pir
    matches: list[PirMatch]  # importance 順 + recency でソート済、上限 _MAX_ARTICLES_PER_PIR 件
    total_match_count: int  # フィルタ前の全 match 件数
    llm_summary: str  # LLM 生成 1-2 文 (空文字なら未生成)


@dataclass(frozen=True)
class DailyFocusRunResult:
    """pir-daily-focus run の結果。"""

    sections: list[PirFocusSection] = field(default_factory=list)
    posted: bool = False
    skipped_no_matches: bool = False
    errors: list[str] = field(default_factory=list)


_IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}


def _sort_matches(matches: list[PirMatch]) -> list[PirMatch]:
    """importance 高い順 → created_at 新しい順。"""
    return sorted(
        matches,
        key=lambda m: (
            _IMPORTANCE_ORDER.get((m.importance or "").lower(), 99),
            -_parse_ts(m.created_at),
        ),
    )


def _parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _filter_meaningful_matches(matches: list[PirMatch]) -> list[PirMatch]:
    """importance が medium/high のもののみ採用 (low は noise として除外)。"""
    return [m for m in matches if (m.importance or "").lower() in ("high", "medium")]


def _today_label(tz_name: str = "Asia/Tokyo") -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d")


def _build_prompt(pir: Pir, matches: list[PirMatch]) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(Path("prompts"))),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(_PROMPT_TEMPLATE)
    return template.render(pir=pir, matches=matches)


async def _generate_llm_summary(
    llm: LLMClient,
    pir: Pir,
    matches: list[PirMatch],
) -> str:
    """LLM に 1-2 文要点を生成させる。失敗時は空文字。"""
    prompt = _build_prompt(pir, matches)
    try:
        response = await llm.generate(
            prompt=prompt,
            temperature=_LLM_TEMPERATURE,
            max_tokens=_LLM_MAX_TOKENS,
            think=False,
        )
        return response.text.strip()
    except Exception as e:  # noqa: BLE001
        _log.warning("daily_focus_llm_failed", pir_id=pir.id, error=str(e))
        return ""


def _format_match_line(m: PirMatch) -> str:
    imp = (m.importance or "low").lower()
    icon = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(imp, "·")
    title = (m.title or m.article_id).strip()
    feed = m.feed_title or "source"
    # S1: 出典の信頼度マーカー (朝の通読で SNS/一次を一目で。tier は feed メタから決定的)。
    from src.cti.source_basis import classify_source_tier

    tier = classify_source_tier(m.feed_title or "", m.feed_url or "")
    marker = {"social": " ⚠SNS(要裏取り)", "official": " ✓一次"}.get(tier, "")
    return f"  {icon} [{imp}]{marker} [{title}]({m.url}) — {feed}"


def _format_section(section: PirFocusSection) -> str:
    """1 PIR 分の Markdown section。"""
    head = f"### {section.pir.title}  ({section.total_match_count} match"
    if section.total_match_count > len(section.matches):
        head += f", top {len(section.matches)}"
    head += ")"
    lines = [head]
    for m in section.matches:
        lines.append(_format_match_line(m))
    if section.llm_summary:
        lines.append(f"  💡 {section.llm_summary}")
    return "\n".join(lines)


def _format_full_digest(sections: list[PirFocusSection], period_label: str) -> str:
    """全 PIR section を結合して 1 つの digest 本文に。"""
    header = (
        f"📍 **PIR Daily Focus — {period_label}** (last 24h)\n"
        f"各 PIR 領域で直近 24h に match した article (importance ≥ medium) を集約。\n"
    )
    body = "\n\n".join(_format_section(s) for s in sections)
    return f"{header}\n{body}"


# Discord 要点射影 (2026-07-12): full digest (記事 3 行 + 💡) は朝刊の 77% を占め毎朝
# 4-5 連投の主犯だった。Discord には 1 PIR = 1 行のみ、記事リンクは Web 全文で読む。
_COMPACT_GIST_MAX = 160  # 1 行要点の上限 (llm_summary は 1-2 文 ≈ 100-150 字想定)


def format_compact_digest(sections: list[PirFocusSection], period_label: str) -> str:
    """Discord 用の PIR 要点射影: 1 PIR = 1 行 (決定論、LLM 追加呼出なし)。"""
    if not sections:
        return ""
    lines = [f"📍 **PIR Daily Focus — {period_label}** (last 24h, {len(sections)} 領域)"]
    for s in sections:
        imps = {(m.importance or "low").lower() for m in s.matches}
        icon = "🔴" if "high" in imps else ("🟡" if "medium" in imps else "⚪")
        gist = (s.llm_summary or (s.matches[0].title if s.matches else "") or "").strip()
        if len(gist) > _COMPACT_GIST_MAX:
            gist = gist[: _COMPACT_GIST_MAX - 1] + "…"
        line = f"{icon} **{s.pir.title}** ({s.total_match_count} match)"
        if gist:
            line += f" — {gist}"
        lines.append(line)
    return "\n".join(lines)


async def collect_pir_focus_sections(
    *,
    llm: LLMClient,
    lookback_hours: int = 24,
    max_pirs_with_llm: int = 25,
) -> list[PirFocusSection]:
    """全 enabled PIR を loop して match を集計、各 PIR に LLM 1-2 文要約を付加。

    Args:
        llm: Ollama client (per-PIR で 1 call)
        lookback_hours: PIR match の振り返り期間 (default 24h)
        max_pirs_with_llm: LLM 要約を付ける PIR 上限 (cost 安全弁)

    Returns:
        match が >=1 件の PIR のみを含む list (空 PIR は除外)
    """
    cfg = load_pir_config()
    sections: list[PirFocusSection] = []

    enabled = [p for p in cfg.priorities if p.enabled]
    _log.info(
        "daily_focus_pirs_total",
        enabled_count=len(enabled),
        lookback_hours=lookback_hours,
    )

    for pir in enabled:
        all_matches = evaluate_pir_matches(
            pir,
            lookback_hours=lookback_hours,
            limit=2000,
        )
        meaningful = _filter_meaningful_matches(all_matches)
        if not meaningful:
            continue
        sorted_matches = _sort_matches(meaningful)
        top = sorted_matches[:_MAX_ARTICLES_PER_PIR]

        # LLM 要約 (上限超えなら skip)
        llm_summary = ""
        if len(sections) < max_pirs_with_llm:
            llm_summary = await _generate_llm_summary(llm, pir, top)

        sections.append(
            PirFocusSection(
                pir=pir,
                matches=top,
                total_match_count=len(meaningful),
                llm_summary=llm_summary,
            ),
        )

    _log.info(
        "daily_focus_sections_built",
        section_count=len(sections),
        with_llm=sum(1 for s in sections if s.llm_summary),
    )
    return sections


async def run_pir_daily_focus(
    *,
    config: AppConfig,
    pipeline: PipelineConfig,
    llm: LLMClient,
    publishers: dict[DiscordChannel, DiscordPublisher],
    dry_run: bool = False,
) -> DailyFocusRunResult:
    """pir-daily-focus pipeline 本体。"""
    _ = config
    lookback = pipeline.source.digest_lookback_hours or 24
    period_label = _today_label()

    sections = await collect_pir_focus_sections(
        llm=llm,
        lookback_hours=lookback,
    )

    if not sections:
        _log.info("daily_focus_skip_empty", pipeline=pipeline.name)
        return DailyFocusRunResult(
            sections=[],
            posted=False,
            skipped_no_matches=True,
            errors=[],
        )

    body = _format_full_digest(sections, period_label)

    # Discord embed 用 sources (上位 5 件、参照リンク)
    sources: list[Source] = []
    for s in sections:
        for m in s.matches:
            sources.append(
                Source(title=m.feed_title or "source", url=m.url, language="auto"),
            )
            if len(sources) >= 5:
                break
        if len(sources) >= 5:
            break

    message = BriefingMessage(
        title=f"PIR Daily Focus ({period_label})",
        bluf=f"{len(sections)} PIR で match あり",
        importance="medium",
        category="pir_daily_focus",
        summary=body,
        sources=sources,
        metadata={
            "target_channel": DAILY_FOCUS_TARGET_CHANNEL,
            "digest_kind": "pir_daily_focus",
            "pir_section_count": len(sections),
            "routing_reason": "digest_pipeline:pir_daily_focus",
            "routing_rule_id": "DIGEST",
        },
    )

    if dry_run:
        _log.info(
            "daily_focus_dry_run",
            pipeline=pipeline.name,
            section_count=len(sections),
            body_chars=len(body),
        )
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[],
        )

    publisher = publishers.get(DAILY_FOCUS_TARGET_CHANNEL)
    if publisher is None:
        _log.error(
            "daily_focus_publisher_missing",
            pipeline=pipeline.name,
            channel=DAILY_FOCUS_TARGET_CHANNEL,
        )
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[f"publisher_missing:{DAILY_FOCUS_TARGET_CHANNEL}"],
        )

    try:
        await publisher.post(message)
    except Exception as e:  # noqa: BLE001
        _log.error("daily_focus_post_failed", pipeline=pipeline.name, error=str(e))
        return DailyFocusRunResult(
            sections=sections,
            posted=False,
            skipped_no_matches=False,
            errors=[f"discord_post: {type(e).__name__}: {e}"],
        )

    _log.info(
        "daily_focus_posted",
        pipeline=pipeline.name,
        section_count=len(sections),
        body_chars=len(body),
    )
    return DailyFocusRunResult(
        sections=sections,
        posted=True,
        skipped_no_matches=False,
        errors=[],
    )


__all__: list[str] = [
    "DAILY_FOCUS_TARGET_CHANNEL",
    "DailyFocusRunResult",
    "PirFocusSection",
    "collect_pir_focus_sections",
    "run_pir_daily_focus",
]
