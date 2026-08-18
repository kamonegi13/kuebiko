"""行 → レコード変換ヘルパ (run_history 分割の一部)。

``sqlite3.Row`` / psycopg の行を Pydantic レコードへ写像する純関数群。
すべて ``_`` 始まりの private シンボルだが ``src.storage.run_history`` から
re-export され、既存テストの import 互換を保つ。
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from src.storage.records import (
    LOG_TRUNCATE_SUFFIX,
    MAX_LOG_LINE_BYTES,
    ActorUpdateProposalRecord,
    ArticleNoteRecord,
    ArticleRecord,
    LogLine,
    RunRecord,
    StatusSynthesisRecord,
    TaxonomyProposalRecord,
)


def _to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _from_iso(s: str | datetime | None) -> datetime | None:
    """SQLite (TEXT) は str を返し、 PG (TIMESTAMPTZ) は datetime を返すので両対応。"""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    return datetime.fromisoformat(s)


def _normalize_jsonb(v: Any) -> str:
    """SQLite (TEXT 列に JSON 文字列) と PG (JSONB を psycopg が dict 化) を統一。

    どちらの backend から来ても JSON 文字列 (json.loads() で再パース可能) を返す。
    """
    import json as _json

    if v is None:
        return "{}"
    if isinstance(v, (dict, list)):
        return _json.dumps(v, ensure_ascii=False)
    return str(v)


def _parse_brief_sources(raw: Any) -> list[dict[str, str]]:
    """daily_briefs.sources (JSON 文字列 or 既に list) を ``[{title,url}]`` に正規化。"""
    import json as _json

    if not raw:
        return []
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
    except (_json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[dict[str, str]] = []
    for item in parsed:
        if isinstance(item, dict) and item.get("url"):
            out.append({"title": str(item.get("title") or ""), "url": str(item["url"])})
    return out


def _parse_brief_payload(raw: Any) -> dict[str, Any] | None:
    """daily_briefs.payload (JSON 文字列 or 既に dict) を dict に正規化 (壊れていれば None)。"""
    import json as _json

    if not raw:
        return None
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
    except (_json.JSONDecodeError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _row_to_daily_brief(row: Any) -> dict[str, Any]:
    """daily_briefs の 1 行を API/Web 向け dict に変換 (generated_at は ISO 正規化)。"""
    gen = _from_iso(row["generated_at"])
    return {
        "id": int(row["id"]),
        "slot": str(row["slot"]),
        "period_label": str(row["period_label"]),
        "title": str(row["title"]),
        "bluf": str(row["bluf"] or ""),
        "summary": str(row["summary"]),
        "section_count": int(row["section_count"] or 0),
        "sources": _parse_brief_sources(row["sources"]),
        "payload": _parse_brief_payload(row["payload"]),
        "generated_at": gen.isoformat() if gen else str(row["generated_at"]),
    }


def _truncate_line(line: str) -> str:
    """1 行が ``MAX_LOG_LINE_BYTES`` を超えたら末尾を切り詰める。"""
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_LOG_LINE_BYTES:
        return line
    suffix_bytes = LOG_TRUNCATE_SUFFIX.encode("utf-8")
    keep_bytes = MAX_LOG_LINE_BYTES - len(suffix_bytes)
    head = encoded[:keep_bytes].decode("utf-8", errors="ignore")
    return head + LOG_TRUNCATE_SUFFIX


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    keys = row.keys() if hasattr(row, "keys") else []
    return RunRecord(
        id=int(row["id"]),
        started_at=_from_iso(row["started_at"]) or datetime.now(UTC),
        finished_at=_from_iso(row["finished_at"]),
        pipeline=row["pipeline"],
        dry_run=bool(row["dry_run"]),
        triggered_by=row["triggered_by"],
        status=row["status"],
        total_fetched=int(row["total_fetched"]),
        summarized=int(row["summarized"]),
        posted=int(row["posted"]),
        marked_read=int(row["marked_read"]),
        error_count=int(row["error_count"]),
        note=row["note"],
        log_line_count=int(row["log_line_count"]) if "log_line_count" in keys else 0,
        log_truncated=bool(row["log_truncated"]) if "log_truncated" in keys else False,
        triage_error_count=(int(row["triage_error_count"]) if "triage_error_count" in keys else 0),
        partial_fetch=(bool(row["partial_fetch"]) if "partial_fetch" in keys else False),
        partial_fetch_count=(
            int(row["partial_fetch_count"]) if "partial_fetch_count" in keys else 0
        ),
    )


def _row_to_article(row: sqlite3.Row) -> ArticleRecord:
    # Phase 5L-4: 既存 DB に dedup_key カラムが無い場合のフォールバック
    # Phase 5T-K: discord_message_id / discord_channel_id も同様 (旧 DB は NULL)
    keys = row.keys()
    dedup_key = row["dedup_key"] if "dedup_key" in keys else None
    dmid = row["discord_message_id"] if "discord_message_id" in keys else None
    dcid = row["discord_channel_id"] if "discord_channel_id" in keys else None
    summary_val = row["summary"] if "summary" in keys else None

    # Phase H: PMESII-PT 8 軸 + Diamond victim (旧 DB なら 0/None)
    def _pmesii(col: str) -> bool:
        return bool(row[col]) if col in keys and row[col] is not None else False

    def _opt_text(col: str) -> str | None:
        return row[col] if col in keys else None

    return ArticleRecord(
        id=int(row["id"]),
        run_id=int(row["run_id"]),
        article_id=row["article_id"],
        title=row["title"],
        url=row["url"],
        feed_title=row["feed_title"],
        feed_url=_opt_text("feed_url"),
        importance=row["importance"],
        category=row["category"],
        status=row["status"],
        failure_reason=row["failure_reason"],
        posted_channel=row["posted_channel"],
        duration_seconds=row["duration_seconds"],
        dedup_key=dedup_key,
        discord_message_id=dmid,
        discord_channel_id=dcid,
        summary=summary_val,
        pmesii_p=_pmesii("pmesii_p"),
        pmesii_m=_pmesii("pmesii_m"),
        pmesii_e=_pmesii("pmesii_e"),
        pmesii_s=_pmesii("pmesii_s"),
        pmesii_i_infra=_pmesii("pmesii_i_infra"),
        pmesii_i_cyber=_pmesii("pmesii_i_cyber"),
        pmesii_p_env=_pmesii("pmesii_p_env"),
        pmesii_t=_pmesii("pmesii_t"),
        victim_sector_canonical=_opt_text("victim_sector_canonical"),
        victim_sector_raw=_opt_text("victim_sector_raw"),
        victim_country_iso=_opt_text("victim_country_iso"),
        victim_country_raw=_opt_text("victim_country_raw"),
        victim_country_scope=_opt_text("victim_country_scope"),
        is_ransomware=_pmesii("is_ransomware"),
        socio_political_intent=_opt_text("socio_political_intent"),
        # 有機的結合監査 H3: 確度列が read 経路で落ちていた (write-only)。NULL=旧レジーム
        # 確定 tag / 非 NULL=新レジーム (low は仮説) — 消費側は NULL を low 扱いしないこと。
        intent_confidence=_opt_text("intent_confidence"),
        socio_political_rationale=_opt_text("socio_political_rationale"),
        # M1: 対処 1 文 (7/04 新設)。記事詳細で表示する。
        remediation=_opt_text("remediation"),
        analyst_note=_opt_text("analyst_note"),
        technical_axis_summary=_opt_text("technical_axis_summary"),
        editorial_stance=_opt_text("editorial_stance"),
        routing_rule_id=_opt_text("routing_rule_id"),
        routing_reason=_opt_text("routing_reason"),
        published_at=(
            _from_iso(row["published_at"])
            if "published_at" in keys and row["published_at"]
            else None
        ),
        event_date=_opt_text("event_date"),
        event_date_basis=_opt_text("event_date_basis"),
        compromise_date=_opt_text("compromise_date"),
        # 主題アクター層 (2026-07-17): NULL = 未評価 (legacy) — 消費側は none と区別すること
        subject_actor_ids=_opt_text("subject_actor_ids"),
        subject_actor_source=_opt_text("subject_actor_source"),
        subject_actor_confidence=_opt_text("subject_actor_confidence"),
        # アクター辞書 D1 (2026-07-26): LLM 層生入力 (保存開始以降のみ非 NULL)
        llm_primary_actor_raw=_opt_text("llm_primary_actor_raw"),
        llm_primary_confidence=_opt_text("llm_primary_confidence"),
        subject_actor_rationale=_opt_text("subject_actor_rationale"),
        # 本文完全性 (2026-07-27): body の由来 + 全文取得失敗理由 (NULL=保存開始前 or heuristic 前)
        body_source=_opt_text("body_source"),
        extraction_failure_reason=_opt_text("extraction_failure_reason"),
        article_type=_opt_text("article_type"),
        # body_source 状態機械化 (2026-07-29): 再取得試行回数 (欠損列耐性、DEFAULT 0)
        refetch_attempts=(int(row["refetch_attempts"] or 0) if "refetch_attempts" in keys else 0),
        created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
    )


def _row_to_synthesis(row: sqlite3.Row) -> StatusSynthesisRecord:
    """sqlite3.Row → StatusSynthesisRecord 変換 (Phase 3)。"""
    keys = row.keys()
    posted_raw = row["posted_at"] if "posted_at" in keys else None
    return StatusSynthesisRecord(
        id=int(row["id"]) if row["id"] is not None else None,
        period_type=str(row["period_type"]),
        period_start=_from_iso(row["period_start"]) or datetime.now(UTC),
        period_end=_from_iso(row["period_end"]) or datetime.now(UTC),
        headline=str(row["headline"]),
        weight_section=str(row["weight_section"]),
        chain_section=str(row["chain_section"]),
        cog_section=str(row["cog_section"]),
        spillover_section=str(row["spillover_section"]),
        pir_section=str(row["pir_section"]),
        axes_evidence=_normalize_jsonb(row["axes_evidence"]),
        tradecraft=(
            _normalize_jsonb(row["tradecraft"])
            if "tradecraft" in keys and row["tradecraft"] is not None
            else ""
        ),
        article_count=int(row["article_count"] or 0),
        llm_model=row["llm_model"],
        generated_at=_from_iso(row["generated_at"]) or datetime.now(UTC),
        posted_at=_from_iso(posted_raw) if posted_raw else None,
    )


def _row_to_spotlight(row: sqlite3.Row) -> Any:
    """sqlite3.Row → SpotlightRecord 変換 (Phase Diamond verify-spotlight)。"""
    import json as _json

    from src.spotlight.models import KeyEvent, SpotlightRecord

    keys = row.keys()
    try:
        events_raw = _json.loads(_normalize_jsonb(row["key_events"]))
        key_events = [KeyEvent.model_validate(e) for e in events_raw if isinstance(e, dict)]
    except (ValueError, TypeError):
        key_events = []
    posted_raw = row["posted_at"] if "posted_at" in keys else None
    return SpotlightRecord(
        pir_id=row["pir_id"],
        pir_title=row["pir_title"],
        period_type=row["period_type"],
        period_start=_from_iso(row["period_start"]) or datetime.now(UTC),
        period_end=_from_iso(row["period_end"]) or datetime.now(UTC),
        headline=row["headline"],
        outlook=row["outlook"],
        key_events=key_events,
        article_count=int(row["article_count"]),
        llm_model=row["llm_model"] or "",
        generated_at=_from_iso(row["generated_at"]) or datetime.now(UTC),
        posted_at=_from_iso(posted_raw) if posted_raw else None,
    )


def _row_to_forecast_indicator(row: sqlite3.Row) -> Any:
    """sqlite3.Row → ForecastIndicatorRecord 変換 (Phase 4 FC2)。"""
    from src.forecast.models import ForecastIndicatorRecord

    keys = row.keys()
    hit_raw = row["hit"] if "hit" in keys else None
    verified_raw = row["verified_at"] if "verified_at" in keys else None
    return ForecastIndicatorRecord(
        id=int(row["id"]) if row["id"] is not None else None,
        period_type=str(row["period_type"]),
        period_start=_from_iso(row["period_start"]) or datetime.now(UTC),
        scope=str(row["scope"]),  # type: ignore[arg-type]
        target_value=str(row["target_value"]),
        direction=str(row["direction"]),
        z_score=float(row["z_score"] or 0.0),
        baseline_avg=float(row["baseline_avg"] or 0.0),
        latest_count=int(row["latest_count"] or 0),
        rationale=str(row["rationale"] or ""),
        created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
        verified_at=_from_iso(verified_raw) if verified_raw else None,
        hit=(bool(hit_raw) if hit_raw is not None else None),
        observed_count=int(row["observed_count"] or 0),
    )


def _row_to_article_note(row: sqlite3.Row) -> ArticleNoteRecord:
    """sqlite3.Row → ArticleNoteRecord 変換 (Phase 5)。"""
    import json as _json

    try:
        tags_raw = _json.loads(row["tags"]) if row["tags"] else []
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
    except (ValueError, TypeError):
        tags = []
    return ArticleNoteRecord(
        article_id=str(row["article_id"]),
        bookmarked=bool(row["bookmarked"]),
        note=str(row["note"] or ""),
        tags=tags,
        judgment=str(row["judgment"] or ""),
        created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
        updated_at=_from_iso(row["updated_at"]) or datetime.now(UTC),
    )


def _row_to_taxonomy_proposal(row: sqlite3.Row) -> TaxonomyProposalRecord:
    """sqlite3.Row → TaxonomyProposalRecord 変換 (Phase H)。"""
    return TaxonomyProposalRecord(
        id=int(row["id"]) if row["id"] is not None else None,
        run_id=int(row["run_id"]) if row["run_id"] is not None else None,
        proposal_type=str(row["proposal_type"]),
        tier=str(row["tier"]),
        target_yaml=str(row["target_yaml"]),
        target_canonical=row["target_canonical"],
        proposed_change=str(row["proposed_change"]),
        rationale=str(row["rationale"]),
        confidence=str(row["confidence"]),
        evidence_count=int(row["evidence_count"] or 0),
        evidence_ids=row["evidence_ids"],
        status=str(row["status"]),
        created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
        reviewed_at=_from_iso(row["reviewed_at"]) if row["reviewed_at"] else None,
        reviewed_by=str(row["reviewed_by"] or "manual"),
    )


def _row_to_actor_proposal(row: sqlite3.Row) -> ActorUpdateProposalRecord:
    """sqlite3.Row → ActorUpdateProposalRecord 変換 (Actors Stage 4)。"""
    return ActorUpdateProposalRecord(
        id=int(row["id"]) if row["id"] is not None else None,
        run_id=int(row["run_id"]) if row["run_id"] is not None else None,
        proposal_type=str(row["proposal_type"]),
        mitre_group=str(row["mitre_group"]),
        dedup_key=str(row["dedup_key"]),
        actor_id=row["actor_id"],
        payload=str(row["payload"]),
        rationale=str(row["rationale"]),
        status=str(row["status"]),
        created_at=_from_iso(row["created_at"]) or datetime.now(UTC),
        decided_at=_from_iso(row["decided_at"]) if row["decided_at"] else None,
    )


def _row_to_log(row: sqlite3.Row) -> LogLine:
    return LogLine(
        run_id=int(row["run_id"]),
        seq=int(row["seq"]),
        ts=_from_iso(row["ts"]) or datetime.now(UTC),
        stream=row["stream"],
        line=row["line"],
    )
