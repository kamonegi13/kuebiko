"""watch ch 蓄積 article を期間 + 重要度で絞り込む SQL helpers (Phase 5T-J)。

E1 (daily research digest) と F1 (weekly recap) で共通の SQL access。
依存: src.storage.run_history.RunHistoryRepository が保持する SQLite。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_DB_PATH = Path("data/run_history.db")


@dataclass(frozen=True)
class DigestCandidate:
    """digest 集約対象 article の snapshot (DB から復元)。"""

    article_id: str
    title: str
    url: str
    feed_title: str
    importance: str | None
    category: str | None
    posted_channel: str | None
    created_at: str  # ISO 8601 string (DB に格納された形式)
    # Phase 5T-K: digest link 生成用 (NULL なら元 URL に fallback)
    discord_message_id: str | None = None
    discord_channel_id: str | None = None
    # Phase 5T-P: BriefingMessage.summary (digest LLM に渡して内容濃度を上げる)
    # 過去レコード (5T-P 以前) は NULL、digest 側で title fallback。
    summary: str | None = None
    # Phase 5T-T1: dedup_key cluster での代表選定と novelty 判定に使う
    dedup_key: str | None = None


def _connect(db_path: Path) -> Any:
    """Phase Y-3: DATABASE_URL set なら PG、 未設定なら SQLite。"""
    from src.storage.db_backend import connect as backend_connect

    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    return con


def _fetch_watch_articles_in_range(
    *,
    since: datetime,
    until: datetime,
    db_path: Path,
) -> list[DigestCandidate]:
    """watch ch に投稿された article を期間絞りで取得 (共通)。

    重複 (同 article_id が複数 run に出現する場合) は最新のみ採用。
    """
    sql = """
        SELECT a.article_id, a.title, a.url, a.feed_title,
               a.importance, a.category, a.posted_channel,
               a.discord_message_id, a.discord_channel_id, a.summary,
               a.dedup_key,
               MAX(a.created_at) AS created_at
        FROM articles a
        WHERE a.created_at >= ? AND a.created_at < ?
          AND a.status = 'posted'
          AND a.posted_channel = 'watch'
        GROUP BY a.article_id, a.title, a.url, a.feed_title,
                 a.importance, a.category, a.posted_channel,
                 a.discord_message_id, a.discord_channel_id, a.summary, a.dedup_key
        ORDER BY MAX(a.created_at) DESC
    """
    # 段5 PG 修正: 旧 `GROUP BY a.article_id` は非集約列を多数 SELECT しており PG (strict GROUP BY)
    # で GroupingError クラッシュ (SQLite は寛容ゆえ unit test で見逃し)。全非集約列を GROUP BY に
    # 含めて PG 準拠化。dup 行は同一 article のため列値が一致し 1 行に畳まれ dedup 意図を保つ。
    with _connect(db_path) as con:
        cur = con.cursor()
        cur.execute(sql, (since.isoformat(), until.isoformat()))
        rows = cur.fetchall()
    keys_set = set(rows[0].keys()) if rows else set()
    has_dmid = "discord_message_id" in keys_set
    has_dcid = "discord_channel_id" in keys_set
    has_summary = "summary" in keys_set
    has_dedup = "dedup_key" in keys_set
    return [
        DigestCandidate(
            article_id=r["article_id"],
            title=r["title"] or "",
            url=r["url"] or "",
            feed_title=r["feed_title"] or "",
            importance=r["importance"],
            category=r["category"],
            posted_channel=r["posted_channel"],
            created_at=r["created_at"],
            discord_message_id=r["discord_message_id"] if has_dmid else None,
            discord_channel_id=r["discord_channel_id"] if has_dcid else None,
            summary=r["summary"] if has_summary else None,
            dedup_key=r["dedup_key"] if has_dedup else None,
        )
        for r in rows
    ]


def fetch_recent_brief_titles(
    *,
    lookback_hours: int = 168,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    max_titles: int = 50,
) -> list[str]:
    """直近 lookback_hours 以内に brief / alert ch へ投稿した article のタイトル。

    deep dive 選定の context (今週速報案件) として LLM prompt に注入する。
    """
    base = now or datetime.now(UTC)
    since = base - timedelta(hours=lookback_hours)
    sql = """
        SELECT title FROM articles
         WHERE created_at >= ? AND created_at < ?
           AND status = 'posted'
           AND posted_channel IN ('brief', 'alert')
         ORDER BY created_at DESC
         LIMIT ?
    """
    with _connect(db_path) as con:
        rows = con.execute(sql, (since.isoformat(), base.isoformat(), max_titles)).fetchall()
    return [r["title"] for r in rows if r["title"]]


@dataclass(frozen=True)
class DeepDivePrefilterResult:
    """Phase 5T-T1: deep dive 候補 prefilter の結果。

    candidates は Stage 0 + Stage 1 を通過した最終候補。
    stage_counts は各段階の通過件数 (selection stage log 用)。
    """

    candidates: list[DigestCandidate]
    stage_counts: dict[str, int]


# 旧 chunk-all 方針 (2026-07-07〜2026-07-20) の名残の runaway guard。現行は Stage 2' の
# RUBRIC_POOL_MAX (60) が常に先に効くため実質不使用だが、引数互換のため残置。
DEEP_DIVE_SAFETY_MAX = 1200


# rubric (LLM) に渡す決定論 shortlist の上限 (2026-07-20 再設計)。60 件 = 1 チャンク以下
# (RUBRIC_CHUNK_SIZE=120) で、LLM 呼出が候補プールの成長と独立に O(1) になる。
# 週次選定は最終 ~10 件のため 6 倍の候補で十分。
RUBRIC_POOL_MAX = 60

# 決定論 composite の重み (コード所有)。取込時に計算済みのシグナルの射影であり、
# LLM の再判定ではない。値の根拠:
# - importance high (+60): 配信の背骨 (triage) の最上位判定を最優先
# - PIR タグ (+10/件, 上限 3): 利用者の関心定義 (PIR) への適合
# - KEV 実悪用 (+12): 実害進行中の事象は深掘り価値が高い
# - corroboration (+8/件, 上限 5): 報道の広がり = その週の大型案件の proxy
# - actor 帰属あり (+6): 帰属付き事象は作戦文脈の深掘りに向く
_W_HIGH = 60
_W_PIR = 10
_PIR_COUNT_MAX = 3
_W_KEV = 12
_W_CORROBORATION = 8
_CORROBORATION_MAX = 5
_W_ACTOR = 6


@dataclass(frozen=True)
class _CandidateSignals:
    """取込時に付与済みの entity シグナル (article_entities の射影)。"""

    pir_count: int = 0
    has_actor: bool = False
    cves: tuple[str, ...] = ()


def _fetch_candidate_signals(
    article_ids: list[str], *, db_path: Path
) -> dict[str, _CandidateSignals]:
    """候補集合の PIR/actor/cve シグナルを article_entities から一括取得 (N+1 回避)。"""
    if not article_ids:
        return {}
    ph = ",".join("?" * len(article_ids))
    sql = (
        "SELECT article_id, entity_type, value FROM article_entities "
        f"WHERE article_id IN ({ph}) AND entity_type IN ('pir', 'actor', 'cve')"
    )
    pir: Counter[str] = Counter()
    actors: set[str] = set()
    cves: dict[str, list[str]] = {}
    with _connect(db_path) as con:
        cur = con.cursor()
        cur.execute(sql, article_ids)
        for r in cur.fetchall():
            aid, etype = str(r["article_id"]), str(r["entity_type"])
            if etype == "pir":
                pir[aid] += 1
            elif etype == "actor":
                actors.add(aid)
            elif etype == "cve":
                cves.setdefault(aid, []).append(str(r["value"]).upper())
    out: dict[str, _CandidateSignals] = {}
    for aid in set(pir) | actors | set(cves):
        out[aid] = _CandidateSignals(
            pir_count=pir.get(aid, 0),
            has_actor=aid in actors,
            cves=tuple(cves.get(aid, [])),
        )
    return out


def _kev_set_failsafe() -> frozenset[str]:
    """KEV CVE 集合 (cache 障害時は空 = KEV 加点なしに degrade)。"""
    try:
        from src.tools.kev_client import get_kev_cve_set

        return get_kev_cve_set()
    except Exception:  # noqa: BLE001 — KEV 障害で選定を止めない
        return frozenset()


def _deterministic_composite(
    c: DigestCandidate,
    corroboration: int,
    sig: _CandidateSignals | None,
    kev_set: frozenset[str],
) -> int:
    score = _W_HIGH if c.importance == "high" else 0
    score += min(corroboration, _CORROBORATION_MAX) * _W_CORROBORATION
    if sig is not None:
        score += min(sig.pir_count, _PIR_COUNT_MAX) * _W_PIR
        if sig.has_actor:
            score += _W_ACTOR
        if any(cve in kev_set for cve in sig.cves):
            score += _W_KEV
    return score


def fetch_for_deep_dive_candidates(
    *,
    lookback_hours: int = 168,
    min_summary_chars: int = 150,
    now: datetime | None = None,
    db_path: Path = DEFAULT_DB_PATH,
    novelty_excluded_dedup_keys: set[str] | None = None,
    safety_max: int = DEEP_DIVE_SAFETY_MAX,
) -> DeepDivePrefilterResult:
    """F1 deep dive の候補を Stage 0 (機械) + Stage 1 (cluster) + Stage 2 (rank) で用意する。

    Stage 0:
        - watch posted_channel
        - lookback_hours 以内
        - importance in (high, medium)
        - summary 長 >= min_summary_chars
        - novelty: 過去 F1 選定済 dedup_key を除外
    Stage 1:
        - dedup_key 同一は最新 (created_at 最大) のみ残す
        - NULL dedup_key は cluster しない (全件残す)
    Stage 2 (2026-07-20 再設計 — 週中に蓄積した判断の射影):
        - 決定論 composite (importance / PIR タグ / KEV / corroboration / actor 帰属) で
          降順整列し **上位 RUBRIC_POOL_MAX (60) 件のみ返す**
        - LLM rubric は固有判断 (深掘り ROI・最終選定) を 1 チャンクで行う
        - 旧 chunk-all (7/7、全件 LLM 採点) は候補プール成長で timeout 構造超過のため置換

    Args:
        lookback_hours: 振り返り期間 (時間)、デフォルト 168h = 7 日
        min_summary_chars: summary 最小文字数 (これより短い article は除外)
        now: 基準時刻 (テスト用)
        db_path: SQLite ファイルパス
        novelty_excluded_dedup_keys: 過去 N 週で F1 選定済の dedup_key set
            (None なら novelty 除外を適用しない)
        safety_max: 暴走防止の絶対上限 (通常週は効かない)
    """
    base = now or datetime.now(UTC)
    since = base - timedelta(hours=lookback_hours)
    all_watch = _fetch_watch_articles_in_range(
        since=since,
        until=base,
        db_path=db_path,
    )

    # Stage 0a: importance フィルタ
    after_importance = [c for c in all_watch if c.importance in ("high", "medium")]

    # Stage 0b: summary 長フィルタ
    after_summary = [
        c for c in after_importance if c.summary and len(c.summary) >= min_summary_chars
    ]

    # Stage 0c: novelty (過去 F1 選定済 dedup_key 除外)
    excluded = novelty_excluded_dedup_keys or set()
    if excluded:
        after_novelty = [c for c in after_summary if c.dedup_key not in excluded]
    else:
        after_novelty = list(after_summary)

    # Stage 1: dedup_key cluster (NULL は全件残す、同 key は created_at 最大のみ)
    after_cluster = _cluster_by_dedup_key(after_novelty)

    # Stage 2 (2026-07-20 本質再設計): **週中に蓄積した判断の射影** で rubric 対象を有界化。
    #
    # 旧 chunk-all (7/7) は全候補 LLM 採点だったが、固定サイズ (~10 件) の出力のために
    # 収集量比例 O(N) の LLM 走査をする構造で、プール成長 (~1,100 件 = 10 chunk) により
    # 45 分 timeout を構造的に超えた (7/20 に 3 連続)。rubric の 4 軸のうち pir/importance/
    # novelty/timeliness はツールが取込時に計算済みのシグナル (PIR タグ・triage importance・
    # dedup 履歴・created_at) と等価であり、全数 LLM 再判定は背骨 (PIR→importance) の
    # 二重判定だった。よって決定論 composite (下記) で上位 RUBRIC_POOL_MAX 件に絞り、
    # LLM には固有判断 (深掘り ROI・最終選定) だけを 1 チャンクで委ねる。
    # 除外は silent にしない (stage_counts + 下の info ログで件数を常時可視化)。
    dedup_counts = Counter(c.dedup_key for c in after_novelty if c.dedup_key)

    def _corroboration(c: DigestCandidate) -> int:
        return dedup_counts.get(c.dedup_key, 1) if c.dedup_key else 1

    signals = _fetch_candidate_signals([c.article_id for c in after_cluster], db_path=db_path)
    kev_set = _kev_set_failsafe()
    # 安定ソート 2 段: 新しい順 → composite 降順 (同点は新しい方が上位に残る)
    by_recency = sorted(after_cluster, key=lambda c: str(c.created_at or ""), reverse=True)
    scored = sorted(
        by_recency,
        key=lambda c: (
            -_deterministic_composite(c, _corroboration(c), signals.get(c.article_id), kev_set)
        ),
    )
    after_cap = scored[: min(safety_max, RUBRIC_POOL_MAX)]
    dropped = len(scored) - len(after_cap)
    if dropped > 0:
        _log.info(
            "deep_dive_composite_bounded",
            total=len(scored),
            kept=len(after_cap),
            dropped=dropped,
            pool_max=RUBRIC_POOL_MAX,
        )

    stage_counts = {
        "total_watch": len(all_watch),
        "after_importance": len(after_importance),
        "after_summary": len(after_summary),
        "after_novelty": len(after_novelty),
        "after_cluster": len(after_cluster),
        "after_cap": len(after_cap),
    }
    _log.info(
        "digest_filter_deep_dive_prefilter",
        lookback_hours=lookback_hours,
        min_summary_chars=min_summary_chars,
        novelty_excluded_count=len(excluded),
        **stage_counts,
    )
    return DeepDivePrefilterResult(
        candidates=after_cap,
        stage_counts=stage_counts,
    )


def _cluster_by_dedup_key(
    candidates: list[DigestCandidate],
) -> list[DigestCandidate]:
    """dedup_key 同一は最新 (created_at 最大) のみ残す。NULL key は全件保持。

    created_at は ISO8601 文字列で lexicographic 比較が時系列と一致する。
    戻り値は created_at 降順。
    """
    seen: dict[str, DigestCandidate] = {}
    null_keys: list[DigestCandidate] = []
    for c in candidates:
        key = c.dedup_key
        if not key:
            null_keys.append(c)
            continue
        existing = seen.get(key)
        if existing is None or c.created_at > existing.created_at:
            seen[key] = c
    merged = list(seen.values()) + null_keys
    merged.sort(key=lambda x: x.created_at, reverse=True)
    return merged
