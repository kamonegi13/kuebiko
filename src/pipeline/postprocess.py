"""投稿前後の後処理: cross-task dedup / sort / ops 通知 (src.main から分割)。"""

from __future__ import annotations

from datetime import UTC, datetime

from src.logging_config import get_logger
from src.pipeline.publish import _resolve_channel
from src.pipeline.result import PipelineRunResult
from src.pipeline.summary import DiscordChannel
from src.storage.run_history import RunHistoryRepository
from src.tools.channel_registry import order_map
from src.tools.discord_publisher import BriefingMessage, DiscordPublisher, Importance
from src.tools.embedding_client import EmbeddingClient

_log = get_logger(__name__)


def _dedup_briefings_by_source_url(
    briefings: list[tuple[str, BriefingMessage]],
) -> tuple[list[tuple[str, BriefingMessage]], int]:
    """同一の X 投稿 URL を持つ briefing の重複を後勝ちで除去する (Phase 5D / H)。

    優先順:
      - state_apt 由来を残し x_early_signals 由来を捨てる (深さ優先)
      - 両方が x_early_signals なら早い順 (受信順) を残す
    """
    # 各 briefing を「保有 URL 集合」とともに保持
    seen_urls: dict[str, int] = {}  # url → preserved index
    keep: list[tuple[str, BriefingMessage]] = []
    dropped = 0

    def _task_priority(msg: BriefingMessage) -> int:
        """state_apt = 0, x_early_signals = 1, その他 = 2"""
        tid = str(msg.metadata.get("grok_task_id") or "")
        if tid == "state_apt":
            return 0
        if tid == "x_early_signals":
            return 1
        return 2

    for art_id, msg in briefings:
        urls: set[str] = set()
        for inc in msg.incidents:
            for s in inc.sources:
                if s.url and ("x.com" in s.url or "twitter.com" in s.url):
                    urls.add(s.url)
        if not urls:
            keep.append((art_id, msg))
            continue
        # この briefing 内のいずれかの URL が既出か?
        conflict_idx: int | None = None
        for u in urls:
            if u in seen_urls:
                conflict_idx = seen_urls[u]
                break
        if conflict_idx is None:
            for u in urls:
                seen_urls[u] = len(keep)
            keep.append((art_id, msg))
        else:
            existing = keep[conflict_idx][1]
            # 優先度比較: 数値が小さい方を残す
            if _task_priority(msg) < _task_priority(existing):
                # 新規 (msg) が優位 → 既存を捨てて差し替え
                keep[conflict_idx] = (art_id, msg)
                for u in urls:
                    seen_urls[u] = conflict_idx
            # それ以外は新規を捨てる
            dropped += 1
    return keep, dropped


async def _dedup_incidents_semantic(
    briefings: list[tuple[str, BriefingMessage]],
    *,
    embedder: EmbeddingClient,
    threshold: float = 0.88,
) -> tuple[list[tuple[str, BriefingMessage]], int]:
    """incident 本文の embedding コサインで意味的重複を検出する (Phase 5D / L5)。

    全 briefing 横断で incident 単位の意味重複を検出し、後発の同じ意味の
    incident を **section から削除** する (BriefingMessage 自体は残す)。
    section が空になった briefing は削除対象。

    state_apt 由来を優先 (深さ優先)。embedder 障害時は graceful degradation。
    """
    import numpy as np

    if not briefings:
        return briefings, 0

    # incident 本文を集めて embedding 計算 (1 briefing あたり最大 N 件)
    flat: list[tuple[int, int, str]] = []  # (briefing_idx, incident_idx, body)
    for b_idx, (_aid, msg) in enumerate(briefings):
        for i_idx, inc in enumerate(msg.incidents):
            text = (inc.body or "")[:600]
            if text.strip():
                flat.append((b_idx, i_idx, text))
    if len(flat) <= 1:
        return briefings, 0

    vectors: list[list[float]] = []
    for _b, _i, body in flat:
        try:
            response = await embedder.embed(body)
        except Exception as e:  # noqa: BLE001
            _log.warning("semantic_dedup_embed_failed", error=str(e))
            return briefings, 0
        vectors.append(list(response.vector))

    arr = np.array(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = arr / norms
    sim_matrix = normalized @ normalized.T

    def _task_priority(msg: BriefingMessage) -> int:
        tid = str(msg.metadata.get("grok_task_id") or "")
        if tid == "state_apt":
            return 0
        if tid == "x_early_signals":
            return 1
        return 2

    # 後発 incident のうち、先発と類似度 >= threshold のものを drop
    drop_set: set[tuple[int, int]] = set()
    for j in range(len(flat)):
        if (flat[j][0], flat[j][1]) in drop_set:
            continue
        for i in range(j):
            if (flat[i][0], flat[i][1]) in drop_set:
                continue
            if sim_matrix[i, j] >= threshold:
                # i と j はどちらが優先か
                msg_i = briefings[flat[i][0]][1]
                msg_j = briefings[flat[j][0]][1]
                if _task_priority(msg_i) <= _task_priority(msg_j):
                    drop_set.add((flat[j][0], flat[j][1]))
                else:
                    drop_set.add((flat[i][0], flat[i][1]))
                break

    if not drop_set:
        return briefings, 0

    # drop_set を反映: 該当 incident を section から削除し、空になった briefing を捨てる
    new_briefings: list[tuple[str, BriefingMessage]] = []
    dropped = 0
    for b_idx, (art_id, msg) in enumerate(briefings):
        kept_incidents = [
            inc for i_idx, inc in enumerate(msg.incidents) if (b_idx, i_idx) not in drop_set
        ]
        dropped_count = len(msg.incidents) - len(kept_incidents)
        if dropped_count == 0:
            new_briefings.append((art_id, msg))
            continue
        dropped += dropped_count
        # section に bluf / summary が独立にあれば briefing は残す (incidents だけ更新)
        new_msg = msg.model_copy(update={"incidents": kept_incidents})
        # incident が全部消えても summary 等があれば残す
        if not kept_incidents and not msg.bluf and not msg.summary:
            continue
        new_briefings.append((art_id, new_msg))
    return new_briefings, dropped


def _sort_briefings_for_posting(
    briefings: list[tuple[str, BriefingMessage]],
    importance_map: dict[Importance, DiscordChannel],
) -> list[tuple[str, BriefingMessage]]:
    """priority → daily → research の順で投稿。同一 channel 内は関心度降順 (Phase 5D / E)。

    セカンダリキー:
      - relevance_score 降順 (高 → 低)
      - grok_task_id (state_apt → x_early_signals → 空)
      - article_id (安定ソート)
    """
    # C1: ch 優先順 (旧 _CHANNEL_ORDER) はレジストリの order_map() が SSoT
    channel_order = order_map()

    def _key(item: tuple[str, BriefingMessage]) -> tuple[int, int, int, str]:
        art_id, msg = item
        ch = _resolve_channel(msg, importance_map)
        ch_rank = channel_order.get(ch, 99)
        rel = msg.metadata.get("relevance_score")
        rel_int = int(rel) if isinstance(rel, int) else -1
        # rel_int 降順 → 負号で昇順化
        rel_key = -rel_int
        tid = str(msg.metadata.get("grok_task_id") or "")
        task_rank = 0 if tid == "state_apt" else 1 if tid == "x_early_signals" else 2
        return (ch_rank, rel_key, task_rank, art_id)

    return sorted(briefings, key=_key)


async def _maybe_post_system_notification(
    publishers: dict[DiscordChannel, DiscordPublisher],
    pipeline_name: str,
    result: PipelineRunResult,
    run_id: int | None,
    *,
    is_interval_run: bool = False,
    dedup_repo: RunHistoryRepository | None = None,
) -> bool:
    """system チャンネルにパイプライン稼働状態を 1 行投稿する (Phase 5C, 5L-1 rate limit)。

    返り値: ops へ実際に投稿できたら True (監査 backlog 2026-07-05:
    親プロセスの partial_failure 通知との二重投稿判定に使う)。

    成功時: ``🟢 {pipeline} 完了 · 取得 N / 重複 M / 投稿 P / 既読 K``
    失敗時: ``🔴 @here {pipeline} 失敗 · エラー件数 / 取得 / 投稿`` + 先頭エラー

    Phase 5L-1 rate limit:
        - **失敗時**: 常時送信 (rate limit 適用外)
        - **成功時 + cron**: 常時送信 (1 日 1 回前提のため抑制不要)
        - **成功時 + interval**: 直近 24h で成功 ops 通知済なら抑制 (毎時のノイズ防止)

    publisher が無ければ何もしない (system webhook 未設定の運用を許容)。
    例外は飲み込み、本処理 (Discord 投稿の戻り値) には影響させない。
    """
    publisher = publishers.get("ops")
    if publisher is None:
        return False
    has_failure = bool(result.errors)
    # rate limit: 成功 + interval + 24h 以内に成功 ops 済 → 抑制
    if (
        not has_failure
        and is_interval_run
        and dedup_repo is not None
        and _is_ops_notify_recent_success(dedup_repo, pipeline_name)
    ):
        _log.info(
            "ops_notification_rate_limited",
            pipeline=pipeline_name,
            run_id=run_id,
            reason="interval_success_within_24h",
        )
        return False
    # Phase 5D / 7: 色ベースの system 通知 (運用ノイズと脅威重要度を分離)
    if has_failure:
        title = f"🔴 {pipeline_name} run 失敗"
        bluf = (
            f"@here {pipeline_name} 失敗 · エラー {len(result.errors)} 件 · "
            f"取得 {result.total_fetched} / 投稿 {result.posted}"
        )
        importance: Importance = "high"
        analyst_note = result.errors[0][:300] if result.errors else None
    else:
        title = f"🟢 {pipeline_name} 完了"
        bluf = (
            f"{pipeline_name} 完了 · 取得 {result.total_fetched} / "
            f"重複 {result.skipped_dup} / 投稿 {result.posted} / "
            f"既読 {result.marked_read}"
        )
        importance = "low"
        analyst_note = None
    metadata: dict[str, object] = {"target_channel": "ops"}
    if run_id is not None:
        metadata["run_id"] = run_id
    msg = BriefingMessage(
        title=title,
        bluf=bluf,
        importance=importance,
        category="system",
        summary=bluf,
        analyst_note=analyst_note,
        metadata=metadata,
    )
    try:
        await publisher.post(msg)
    except Exception as e:  # noqa: BLE001
        _log.warning(
            "system_notification_failed",
            pipeline=pipeline_name,
            error=str(e),
        )
        return False
    # 送信成功時のみ rate limit 用に記録
    if dedup_repo is not None:
        try:
            dedup_repo.record_ops_notification(
                pipeline_name=pipeline_name,
                status="failure" if has_failure else "success",
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "ops_notify_log_record_failed",
                pipeline=pipeline_name,
                error=str(e),
            )
    return True


def _is_ops_notify_recent_success(
    dedup_repo: RunHistoryRepository,
    pipeline_name: str,
    *,
    window_hours: int = 24,
) -> bool:
    """直近 ``window_hours`` 以内に **成功** ops 通知が記録されているか。"""
    last = dedup_repo.get_last_ops_notification(pipeline_name)
    if last is None:
        return False
    last_at, last_status = last
    if last_status != "success":
        return False
    age = datetime.now(UTC) - last_at
    return bool(age.total_seconds() < window_hours * 3600)
