"""digest 用 LLM 生成 wrapper (Phase 5T-J)。

src/digest/db_filter.py で取得した article リストを LLM に渡して、
動的セクション分けされた Markdown digest を生成する。

LLM プロンプト (prompts/digest/weekly_recap.j2 等) は
セクション固定でなく LLM 動的判定 (今週の重点を反映)。
"""

from __future__ import annotations

from pathlib import Path

import jinja2

from src.config_loader import load_app_config
from src.digest.db_filter import DigestCandidate
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient, LLMResponse

_log = get_logger(__name__)

PROMPTS_DIR = Path("prompts")
# Phase 5T-J 検証: F1 (30 件入力) で 4563 chars 出力時に 2000 tokens 上限
# でセクション末尾が文中切断された。4000 tokens (~9000 chars 相当) に拡張。
DIGEST_MAX_TOKENS = 4_000
DIGEST_TEMPERATURE = 0.25
# 件数駆動 (2026-07-07): recap 本文は 1 回の LLM 呼出で選定全件を個別深掘りするため、
# 出力予算を件数に応じ拡張し各記事の厚みを保つ (固定 4000 だと件数増で 1 件あたりが痩せる)。
# floor=DIGEST_MAX_TOKENS、per-item ~800tok、ceiling で青天井を防ぐ (生成時間/可読性)。
_DIGEST_TOKENS_PER_ITEM = 800
_DIGEST_MAX_TOKENS_CEILING = 12_000


def _digest_max_tokens(item_count: int) -> int:
    """選定件数に応じた digest 出力予算 (各記事の深掘りの厚みを保つ)。"""
    scaled = min(_DIGEST_MAX_TOKENS_CEILING, item_count * _DIGEST_TOKENS_PER_ITEM)
    return max(DIGEST_MAX_TOKENS, scaled)


def _resolve_link_url(candidate: DigestCandidate, guild_id: str) -> str:
    """Phase 5T-K: digest 項目の link 用 URL を決定。

    優先順:
        1. guild_id + discord_channel_id + discord_message_id が揃えば Discord 直接 URL
        2. そうでなければ元記事 URL (fallback)
    """
    if guild_id and candidate.discord_channel_id and candidate.discord_message_id:
        return (
            f"https://discord.com/channels/{guild_id}/"
            f"{candidate.discord_channel_id}/{candidate.discord_message_id}"
        )
    return candidate.url


def _build_jinja_env() -> jinja2.Environment:
    """digest プロンプト用 Jinja2 環境。"""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
        autoescape=False,  # markdown 出力のためエスケープ無効
        keep_trailing_newline=True,
    )


def _render_prompt(
    template_name: str,
    *,
    candidates: list[DigestCandidate],
    period_label: str,
) -> str:
    """テンプレート + 候補リストから LLM プロンプトを組み立てる。

    Phase 5T-K: 各記事の URL を Discord post URL に変換 (guild_id 設定時)。
    """
    cfg = load_app_config()
    guild_id = cfg.discord_guild_id or ""
    env = _build_jinja_env()
    template = env.get_template(template_name)
    items = [
        {
            "title": c.title,
            "url": _resolve_link_url(c, guild_id),
            "feed": c.feed_title,
            "importance": c.importance or "",
            "category": c.category or "",
            # Phase 5T-P: summary を渡して digest に厚みを持たせる
            # (旧レコードで NULL の場合は空文字、プロンプト側で fallback)
            "summary": (c.summary or "")[:600],  # 過大 token 抑制
        }
        for c in candidates
    ]
    return template.render(items=items, period_label=period_label, total=len(items))


async def generate_digest(
    *,
    llm: LLMClient,
    candidates: list[DigestCandidate],
    template_name: str,
    period_label: str,
    think: bool | None = None,
) -> str:
    """LLM に digest 生成を依頼して Markdown 本文を返す。

    候補 0 件なら空 Article を返さず、本関数の呼び出し側 (runner) で
    skip 判定する想定。本関数は最低 1 件の候補が前提。

    Args:
        llm: LLM クライアント
        candidates: digest 集約対象 article のリスト
        template_name: Jinja2 テンプレート名 (例: "digest/weekly_recap.j2")
        period_label: digest の対象期間ラベル (例: "今日 (2026-05-17)")
        think: LLM thinking モード ON/OFF/既定
    """
    if not candidates:
        raise ValueError("candidates が空です (呼び出し側で skip 判定すること)")

    prompt = _render_prompt(
        template_name,
        candidates=candidates,
        period_label=period_label,
    )
    max_tokens = _digest_max_tokens(len(candidates))
    _log.info(
        "digest_llm_request",
        template=template_name,
        candidate_count=len(candidates),
        prompt_chars=len(prompt),
        max_tokens=max_tokens,
    )
    response: LLMResponse = await llm.generate(
        prompt=prompt,
        temperature=DIGEST_TEMPERATURE,
        max_tokens=max_tokens,
        think=think,
    )
    digest_text = (response.text or "").strip()
    _log.info(
        "digest_llm_response",
        template=template_name,
        output_chars=len(digest_text),
    )
    return digest_text
