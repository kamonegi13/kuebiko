"""薄い本文の関門 (THIN_BODY_GATE) の誤落ちで extract_failed になった記事の回収 (2026-10-30)。

``_is_thin_body`` の決まり文句の照合が本文の長さを問わず当たっていた欠陥 (修正済み) により、
長い技術記事が status='extract_failed' / failure_reason='degenerate_body: thin_body' で
落ちていた。本文は保存されているので **再取得せず**、保存済み本文で 要約 → 主題 → entity →
分析列 を書き直し、status='posted' / posted_channel=NULL /
analysis_only='thin_body_recovery' (どこへも配信しない分析専用) へ進める。
failure_reason='analysis_only:thin_body_recovery' も併せて残す (列を足す前の回収済み行と揃える)。
除外は列 ``analysis_only`` が担う (配信・dedup・公開・PIR の朝の要点)。

- Discord には流さない (本モジュールは publisher を一切持たない)。
- ``articles.created_at`` は触らない: 日次ブリーフ等の「直近 24h」窓に過去分が入らない。
- 選定は「今の関門を通る本文」だけ (関門の再判定で落ちるものは回収しない)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jinja2

from src.logging_config import get_logger
from src.pipeline.briefing import _degenerate_body_reason, _is_thin_body, _summarize_and_build
from src.pipeline.reprocess import reenrich_stored_article
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)


@dataclass(frozen=True)
class RecoveryCandidate:
    """回収対象 1 件 (本文は持ち運ばず長さだけ)。"""

    row_id: int
    article_id: str
    body_len: int
    created_at: datetime | None


def passes_current_gate(body: str, title: str, article_id: str) -> bool:
    """今のコードの薄い本文の関門 (と他の劣化判定) を本文が通るか。"""
    text = (body or "").strip()
    if _degenerate_body_reason(text, title=title, article_id=article_id) is not None:
        return False
    return not _is_thin_body(text, title)


def _as_datetime(v: object) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    return None


def effective_until(
    until: datetime | None, min_age_hours: float, *, now: datetime | None = None
) -> datetime | None:
    """``--until`` と ``--min-age-hours`` (now - N 時間) のうち早い方を選定の上限にする。"""
    if min_age_hours <= 0:
        return until
    cutoff = (now or datetime.now(UTC)) - timedelta(hours=min_age_hours)
    return cutoff if until is None else min(until, cutoff)


def _row_id(row: dict[str, object]) -> int:
    v = row.get("id")
    return int(v) if isinstance(v, (int, str)) else -1


#: 欠陥の被害者 = 決まり文句の照合が長さを問わず当たっていた本文 (600 字以上)。600 字未満は
#: 2 段目 (THIN_BODY_PROBE) を通っていないので既定では回収の対象外。
MIN_BODY_CHARS = 600


def select_candidates(
    rows: list[dict[str, object]],
    *,
    limit: int | None = None,
    min_body_chars: int = MIN_BODY_CHARS,
) -> list[RecoveryCandidate]:
    """行のうち 今の関門を通り本文が ``min_body_chars`` 以上のものを新しい順に返す (純粋関数)。"""
    out: list[RecoveryCandidate] = []
    for r in rows:
        body = str(r.get("body") or "")
        if len(body) < min_body_chars:
            continue
        aid = str(r["article_id"])
        if not passes_current_gate(body, str(r.get("title") or ""), aid):
            continue
        out.append(RecoveryCandidate(_row_id(r), aid, len(body), _as_datetime(r.get("created_at"))))
    out.sort(key=lambda c: c.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out[:limit] if limit else out


async def recover_one(
    repo: RunHistoryRepository,
    row: dict[str, object],
    *,
    llm: LLMClient,
    template: jinja2.Template,
    enrichment: object | None,
) -> bool:
    """1 記事を保存済み本文で再要約して回収する。成功 True (失敗は理由を log して False)。"""
    article_id = str(row["article_id"])
    body = str(row["body"])
    rec = repo.get_article(article_id)
    if rec is None:
        _log.warning("thin_body_recover_skipped", article_id=article_id, reason="record_missing")
        return False
    published = _as_datetime(row.get("published_at")) or _as_datetime(row.get("created_at"))
    article = Article(
        id=article_id,
        title=str(row.get("title") or ""),
        url=str(row["url"]),
        summary_html="",
        published=published or datetime.now(UTC),
        feed_title=str(row.get("feed_title") or ""),
        feed_url=str(row.get("feed_url") or ""),
        body_text=body,
    )
    msg = await _summarize_and_build(
        article,
        body,
        llm,
        template,
        enrichment=enrichment,
        body_source=str(row.get("body_source") or "full_extract"),
    )
    reenrich_stored_article(repo, article_id, rec, msg)
    # 状態の更新は最後: 途中で落ちた記事は extract_failed のまま残り、再実行で拾われる
    promoted = repo.promote_recovered_thin_body(
        _row_id(row), importance=msg.importance, category=msg.category
    )
    if not promoted:
        _log.warning("thin_body_recover_skipped", article_id=article_id, reason="not_promoted")
        return False
    _log.info("thin_body_recovered", article_id=article_id, body_len=len(body))
    return True
