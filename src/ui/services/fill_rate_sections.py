"""週次 fill-rate 監査の補助の節 (fill_rate_audit.py から分離、2026-09-27)。

fill_rate_audit.py が 800 行の上限を超えたため、自己完結した節 (重複本文・重複情勢・
事象ニュースの網羅率・アクターの偏り) をここへ移した。名前は fill_rate_audit から再公開している。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

_FULL_BODY_COND = "body_source IN ('full_extract','playwright_extract','prefetch','scraper')"

# 本文同一性監査 (2026-08-21): サイト改装で抽出がナビ/ティーザー列に化けると「取得成立・
# 本文非空・長さ妥当」のまま複数記事が同一 body になり、既存の全監視 (死活 / 充足率 /
# 切り株率 / html 残渣) をすり抜ける (The Register 344 件・約 1 か月無検知の実例)。
# 同一 feed 内で同一 body 先頭部が多重出現することを検出信号にする。
_DUP_BODY_PREFIX_LEN = 400  # 比較キー長 (枠テキストは先頭から同一。全文比較は不要に重い)
_DUP_BODY_MIN_LEN = 500  # これ未満の短文は定型 advisory 等の正当な重複がありうるため除外
_DUP_BODY_WARN_COUNT = 5  # 7 日内の同一 body がこの件数以上 = 構造的な抽出誤りの疑い


def fetch_duplicate_body_rows(con: Any, since_iso: str) -> list[tuple[str, int, int]]:
    """feed 単位の (feed, 最多重複 body 件数, 全文系の総件数) を返す (grok/ransomware 除外)。"""
    sql = (
        "SELECT feed, MAX(cnt) AS dup_max, SUM(cnt) AS n_total FROM ("
        " SELECT COALESCE(feed_title,'(不明)') AS feed,"
        f" substr(body, 1, {_DUP_BODY_PREFIX_LEN}) AS body_key, COUNT(*) AS cnt"  # noqa: S608
        " FROM articles"
        f" WHERE created_at >= ? AND {_FULL_BODY_COND}"  # noqa: S608 — 条件は内部定数のみ
        f" AND body IS NOT NULL AND LENGTH(body) >= {_DUP_BODY_MIN_LEN}"
        " AND (feed_title IS NULL OR LOWER(feed_title) NOT IN ('grok','ransomware.live'))"
        f" GROUP BY COALESCE(feed_title,'(不明)'), substr(body, 1, {_DUP_BODY_PREFIX_LEN})"
        ") t GROUP BY feed"
    )
    rows = con.execute(sql, [since_iso]).fetchall()
    return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]


def detect_duplicate_body_warns(
    rows: list[tuple[str, int, int]], *, warn_count: int = _DUP_BODY_WARN_COUNT
) -> list[tuple[str, int, int]]:
    """同一 body の最多重複が閾値以上の feed を、重複数の降順で返す。"""
    return sorted((r for r in rows if r[1] >= warn_count), key=lambda r: -r[1])


def build_duplicate_body_lines(warns: list[tuple[str, int, int]]) -> list[str]:
    """weekly 監査投稿の本文同一性セクション (OK 時は空=投稿を短く保つ)。"""
    if not warns:
        return []
    lines = [
        f"本文同一性: ⚠️ {len(warns)} ソースで同一本文の多重出現 (改装でナビ/枠を抽出している疑い)"
    ]
    for feed, dup, total in warns[:8]:
        lines.append(
            f"- {feed}: 同一本文 {dup} 件 / 7日 {total} 件 — 実記事の目視と pre-trim 登録を検討"
        )
    return lines


async def _scan_duplicate_situations() -> list[Any]:
    """情勢の題名を埋め込み、重複の疑いがある組を返す (週次監査用)。

    ⚠ 埋込は 220 件前後なので数十秒。失敗は呼び手が握る (監査全体は落とさない)。
    ⚠ async で定義する: 監査本体は event loop 上で動くため、内部で asyncio.run を呼ぶと
       毎回「running event loop」で失敗していた (2026-09-27 まで検査が一度も動いていない)。
    """
    import numpy as np

    from src.assessment.situation_dup_scan import find_duplicate_pairs
    from src.assessment.situation_store import SituationStore
    from src.config_loader import load_app_config
    from src.tools.embedding_client import OllamaEmbeddingClient
    from src.tools.model_tiers import resolve_embedding_model

    rows = SituationStore(db_path=Path("data/run_history.db")).load_situations(
        ("active", "dormant")
    )

    async def _vectors() -> list[tuple[str, str, str, Any]]:
        client = OllamaEmbeddingClient(
            base_url=load_app_config().ollama_base_url, model=resolve_embedding_model()
        )
        out = []
        for row in rows:
            arr = np.asarray((await client.embed(row.title)).vector, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm:
                out.append((row.situation_id, row.title, row.kind, arr / norm))
        return out

    return find_duplicate_pairs(await _vectors())


def _eventnews_fidelity_line(now: datetime) -> tuple[str, bool]:
    """直近 7 日と前の 7 日の版で、固有情報の網羅率の平均を比べる。"""
    from src.eventnews.fidelity import version_coverage, weekly_line
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    edges = [(now - timedelta(days=d)).isoformat() for d in (14, 7, 0)]

    def _covs(since: str, until: str) -> list[Any]:
        rows = repo.event_version_texts_between(since, until)
        return [c for p, b, h in rows if (c := version_coverage(p, b, h)) is not None]

    return weekly_line(_covs(edges[1], edges[2]), _covs(edges[0], edges[1]))


def _actor_skew_lines(con: Any, now: datetime) -> tuple[list[str], bool]:
    """アクターの言及が地政学の記事に偏るものを確認候補に (直近 12 週、2026-09-27)。"""
    from src.cti.actor_contamination import (
        contamination_exclusions,
        find_skewed_actors,
        skew_lines,
    )

    since = (now - timedelta(weeks=12)).isoformat()
    rows = con.execute(
        "SELECT e.value AS actor, COALESCE(a.category, '') AS category, a.title AS title"
        " FROM article_entities e JOIN articles a ON a.article_id = e.article_id"
        " WHERE e.entity_type = 'actor' AND a.created_at >= ?",
        (since,),
    ).fetchall()
    skews = find_skewed_actors(
        ((str(r["actor"]), str(r["category"]), str(r["title"] or "")) for r in rows),
        exclude=contamination_exclusions(),
    )
    return skew_lines(skews)
