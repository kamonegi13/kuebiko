"""Phase 2 K6: status synthesis を Discord (brief) に配信する。

UI 限定だった「状況総括」を朝読チャンネルにも届ける。生成 (generator) と配信を
分離し (SRP)、配信は pipeline runner から呼ぶ。再配信 dedup は
``status_synthesis.posted_at`` で行う (一度配信した period は再生成しても再投稿しない)。
"""

from __future__ import annotations

import json

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository, StatusSynthesisRecord
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher

_log = get_logger(__name__)

# period_type → 日本語ラベル (タイトル表示用)。
_PERIOD_LABEL: dict[str, str] = {
    "daily": "日次",
    "weekly": "週次",
    "monthly": "月次",
}

# 配信時のセクション見出し (status_synthesis の各 section column に対応)。
_SECTION_HEADERS: list[tuple[str, str]] = [
    ("weight_section", "■ 比重 (どの軸が重いか)"),
    ("chain_section", "■ 連鎖 (軸間の因果)"),
    ("cog_section", "■ 重心 (CoG)"),
    ("spillover_section", "■ 波及"),
    ("pir_section", "■ PIR 達成度"),
]


def _format_tradecraft(raw: str) -> str:
    """S2: tradecraft JSON を Discord 用の block に整形 (主見立て+対立仮説+前提+指標)。

    空 / parse 失敗 / 内容なしは空文字 (block を出さない)。
    """
    if not raw:
        return ""
    try:
        tc = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(tc, dict):
        return ""

    lines: list[str] = []
    leading = str(tc.get("leading_assessment", "")).strip()
    if leading:
        lines.append(f"主見立て: {leading}")
    for key, label in (
        ("alternatives", "🔀 対立仮説"),
        ("key_assumptions", "🧩 前提"),
        ("indicators", "📡 監視指標"),
    ):
        items = tc.get(key)
        if not isinstance(items, list):
            continue
        cleaned = [str(it).strip() for it in items if str(it).strip()]
        if cleaned:
            lines.append(f"{label}: " + " / ".join(cleaned))
    if not lines:
        return ""
    return "🔍 分析トレードクラフト (別解・前提・監視指標)\n" + "\n".join(lines)


def build_synthesis_message(record: StatusSynthesisRecord) -> BriefingMessage:
    """StatusSynthesisRecord を Discord 投稿用 BriefingMessage に変換する。

    headline を太字先頭に置き、各 section を見出し付きで連結する。長文は
    publisher 側で改行境界に沿って自動分割される。category は
    ``_CATEGORY_PRESENTATION`` 外の値にして余計な chrome (badge / author) を抑える。
    """
    label = _PERIOD_LABEL.get(record.period_type, record.period_type)
    parts: list[str] = [f"**{record.headline.strip()}**"]
    for col, header in _SECTION_HEADERS:
        section = (getattr(record, col, "") or "").strip()
        if section:
            parts.append(f"{header}\n{section}")
    # S2: 分析トレードクラフト (対立仮説・前提・覆る指標) を末尾に添える。
    tradecraft_block = _format_tradecraft(record.tradecraft)
    if tradecraft_block:
        parts.append(tradecraft_block)
    summary = "\n\n".join(parts)

    return BriefingMessage(
        title=f"📊 状況総括 ({label})",
        importance="medium",
        category="status_synthesis",  # 専用カテゴリ (chrome 最小化)
        summary=summary,
        metadata={"intel_kind": "synthesis", "period_type": record.period_type},
    )


# Discord 要点射影 (BLUF, 2026-07-12): 日次ブリーフの全文 push をやめ、headline + 比重 +
# 重心のみを配信する。連鎖 / 波及 / PIR 達成度 / トレードクラフトは Web 全文 (daily_briefs
# → /app/daily-brief) で読む。決定論の節選択のみで LLM 追加呼出なし。
_COMPACT_SECTION_KEYS: tuple[str, ...] = ("weight_section", "cog_section")


def build_synthesis_compact(record: StatusSynthesisRecord) -> str:
    """StatusSynthesisRecord の Discord 用 要点射影を返す (全文は Web で読む)。"""
    headers = dict(_SECTION_HEADERS)
    parts: list[str] = [f"**{record.headline.strip()}**"]
    for col in _COMPACT_SECTION_KEYS:
        section = (getattr(record, col, "") or "").strip()
        if section:
            parts.append(f"{headers[col]}\n{section}")
    return "\n\n".join(parts)


async def deliver_synthesis(
    *,
    repo: RunHistoryRepository,
    publisher: DiscordPublisher,
    period_types: list[str],
) -> list[str]:
    """指定 period の最新 synthesis を未配信なら brief に投稿する。

    各 period について ``get_latest_synthesis`` で最新を取り、``posted_at`` が
    未設定 (未配信) のものだけ投稿し ``mark_synthesis_posted`` で記録する。
    1 period の失敗は他 period の配信を止めない (投稿失敗なら posted_at は据え置き
    = 次回 run でリトライ)。配信できた period_type のリストを返す。
    """
    delivered: list[str] = []
    for pt in period_types:
        record = repo.get_latest_synthesis(period_type=pt)
        if record is None:
            continue
        if record.posted_at is not None:
            _log.info("synthesis_already_delivered", period_type=pt)
            continue
        try:
            await publisher.post(build_synthesis_message(record))
            repo.mark_synthesis_posted(
                period_type=record.period_type,
                period_start=record.period_start,
            )
            delivered.append(pt)
            _log.info("synthesis_delivered", period_type=pt)
        except Exception as e:  # noqa: BLE001
            _log.error("synthesis_delivery_failed", period_type=pt, error=str(e))
    return delivered
