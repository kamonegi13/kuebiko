"""Phase 2 K6: PIR Spotlight を Discord (watch) に配信する。

UI 限定だった PIR 縦断 narrative を観察チャンネルにも届ける。生成 (generator) と
配信を分離し、配信は pipeline runner から呼ぶ。再配信 dedup は
``pir_spotlight.posted_at`` で行う (一度配信した period は再生成しても再投稿しない)。
"""

from __future__ import annotations

from src.logging_config import get_logger
from src.spotlight.models import SpotlightRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher, Source

_log = get_logger(__name__)

_PERIOD_LABEL: dict[str, str] = {
    "daily": "日次",
    "weekly": "週次",
    "monthly": "月次",
}
# key_events から sources に載せる最大件数 (Discord field 長を圧迫しない範囲)。
_MAX_EVENT_SOURCES = 8


def build_spotlight_message(record: SpotlightRecord) -> BriefingMessage:
    """SpotlightRecord を Discord 投稿用 BriefingMessage に変換する。

    headline を太字先頭、outlook を本文に置き、key_events は出典リンク (sources) に。
    category は ``_CATEGORY_PRESENTATION`` 外の値で chrome を最小化する。
    """
    label = _PERIOD_LABEL.get(record.period_type, record.period_type)
    parts: list[str] = [f"**{record.headline.strip()}**"]
    outlook = (record.outlook or "").strip()
    if outlook:
        parts.append(outlook)
    summary = "\n\n".join(parts)

    sources: list[Source] = []
    for ke in record.key_events[:_MAX_EVENT_SOURCES]:
        url = (ke.url or "").strip()
        if url.startswith("http"):
            sources.append(Source(title=ke.title[:120] or url, url=url))

    return BriefingMessage(
        title=f"🔦 {record.pir_title} ({label})",
        importance="medium",
        category="pir_spotlight",  # 専用カテゴリ (chrome 最小化)
        summary=summary,
        sources=sources,
        metadata={"intel_kind": "spotlight", "pir_id": record.pir_id},
    )


async def deliver_spotlights(
    *,
    repo: RunHistoryRepository,
    publisher: DiscordPublisher,
    pir_ids: list[str],
    period_type: str,
) -> list[str]:
    """指定 PIR の最新 spotlight を未配信なら watch に投稿する。

    各 pir_id の最新 spotlight を取り、``posted_at`` が未設定のものだけ投稿し
    ``mark_spotlight_posted`` で記録する。1 件の失敗は他を止めない (投稿失敗なら
    posted_at 据え置き = 次回 run でリトライ)。配信できた pir_id のリストを返す。
    """
    delivered: list[str] = []
    for pir_id in pir_ids:
        record: SpotlightRecord | None = repo.get_latest_spotlight(
            pir_id=pir_id, period_type=period_type
        )
        if record is None:
            continue
        if record.posted_at is not None:
            _log.info("spotlight_already_delivered", pir_id=pir_id)
            continue
        try:
            await publisher.post(build_spotlight_message(record))
            repo.mark_spotlight_posted(
                pir_id=record.pir_id,
                period_type=record.period_type,
                period_start=record.period_start,
            )
            delivered.append(pir_id)
            _log.info("spotlight_delivered", pir_id=pir_id)
        except Exception as e:  # noqa: BLE001
            _log.error("spotlight_delivery_failed", pir_id=pir_id, error=str(e))
    return delivered
