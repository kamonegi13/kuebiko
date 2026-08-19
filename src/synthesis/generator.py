"""Phase 3 Synthesis: LLM ベースの状況総括生成。

Pipeline:
    1. 軸別構造集計 (intel_graph_analytics の fetch_axis_dashboard を再利用)
    2. trend cluster 集計 (trend_aggregator の aggregate_trends を再利用)
    3. prompts/synthesis/status_synthesis.j2 で LLM call
    4. JSON 出力を parse → StatusSynthesisRecord に変換
    5. 呼び出し側 (runner) が repo に UPSERT

LLM は軸別 article 統計 + trend cluster + PIR を context として受け、
**軸間関係・連鎖・不均衡・重心** に焦点を当てた structured JSON を返す。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jinja2

from src.assessment.context import build_assessment_context
from src.digest.trend_aggregator import aggregate_trends
from src.logging_config import get_logger
from src.storage.run_history import StatusSynthesisRecord
from src.tools.llm_client import LLMClient, LLMResponse
from src.ui.services.intel_graph_analytics import (
    fetch_axis_dashboard,
)

_log = get_logger(__name__)

PROMPTS_DIR = Path("prompts")
SYNTHESIS_TEMPLATE = "synthesis/status_synthesis.j2"
SYNTHESIS_MAX_TOKENS = 12_000
SYNTHESIS_TEMPERATURE = 0.25


@dataclass(frozen=True)
class SynthesisGenerationResult:
    record: StatusSynthesisRecord | None
    raw_response: str
    error: str | None = None


def _spike_label(spike_ratio: float, is_new: bool) -> str:
    if is_new or spike_ratio == float("inf"):
        return "🆕 NEW"
    if spike_ratio >= 5:
        return f"🔥 {spike_ratio:.1f}x"
    if spike_ratio >= 2:
        return f"📈 {spike_ratio:.1f}x"
    if spike_ratio > 0:
        return f"{spike_ratio:.1f}x"
    return "-"


# 軸別 incident は最近順 N 件、recall 不足対策で 12 件に増やす (Phase 3 修正)
_RECENT_INCIDENTS_PER_AXIS = 12
# cross-axis high-importance article は 軸タグに関わらず 30 件まで LLM に渡す
_CROSS_AXIS_HIGH_IMPORTANCE_LIMIT = 30


def _build_axes_data(
    *,
    lookback_hours: int,
    baseline_weeks: int,
    db_path: Path,
) -> list[dict[str, Any]]:
    """LLM prompt に渡す軸別構造データ。"""
    cards = fetch_axis_dashboard(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        db_path=db_path,
        recent_incident_limit=_RECENT_INCIDENTS_PER_AXIS,
    )
    out: list[dict[str, Any]] = []
    for c in cards:
        out.append(
            {
                "axis_id": c.axis_id,
                "display": c.display,
                "total_current": c.total_current,
                "baseline_avg": f"{c.total_baseline_avg:.1f}",
                "spike_label": _spike_label(c.spike_ratio, c.is_new),
                "top_sectors": [{"label": s.label, "count": s.count} for s in c.top_sectors],
                "top_countries": [{"label": s.label, "count": s.count} for s in c.top_countries],
                "recent_incidents": [
                    {
                        "title": i.title,
                        "feed_title": i.feed_title,
                        "article_id": i.article_id,
                        "importance": i.importance or "",
                    }
                    for i in c.recent_incidents
                ],
            },
        )
    return out


def _build_high_importance_cross_axis(
    *,
    lookback_hours: int,
    db_path: Path,
    limit: int = _CROSS_AXIS_HIGH_IMPORTANCE_LIMIT,
    importance_levels: tuple[str, ...] = ("high",),
) -> list[dict[str, Any]]:
    """軸横断で importance が指定水準の article を high 優先→直近順に N 件返す。

    軸別 recent_incidents から漏れる重要事象 (e.g. software supply chain、
    新規 APT 動向、KEV 追加) を LLM に確実に届けるための補助 channel。
    ``importance_levels`` 既定は high のみ (legacy single-pass 互換)。grounded は
    ("high","medium") を渡し、各 claim が続報・裏取り (medium) を複数接地できるようにする。
    """
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from src.storage.db_backend import connect as backend_connect

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    levels = importance_levels or ("high",)
    placeholders = ",".join("?" for _ in levels)
    con = backend_connect(db_path)
    if hasattr(con, "row_factory"):
        con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""
            SELECT article_id, title, feed_title, feed_url, importance, editorial_stance,
                   created_at, event_date, event_date_basis,
                   pmesii_p, pmesii_m, pmesii_e, pmesii_s,
                   pmesii_i_infra, pmesii_i_cyber, pmesii_p_env, pmesii_t
              FROM articles
             WHERE status='posted'
               AND importance IN ({placeholders})
               AND datetime(created_at) >= datetime(?)
             ORDER BY CASE importance WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                      datetime(created_at) DESC
             LIMIT ?
            """,
            (*levels, since.isoformat(), limit),
        ).fetchall()
    finally:
        con.close()
    out: list[dict[str, Any]] = []
    axis_cols = [
        ("P", "pmesii_p"),
        ("M", "pmesii_m"),
        ("E", "pmesii_e"),
        ("S", "pmesii_s"),
        ("I-infra", "pmesii_i_infra"),
        ("I-cyber", "pmesii_i_cyber"),
        ("P-env", "pmesii_p_env"),
        ("T", "pmesii_t"),
    ]
    # A (裏取り信号): 各高重要記事に source 信頼度 tier (S1, NATO Admiralty 信頼度軸) を付与。
    # synthesis が「単一の劇的報告 (低 tier・未裏取り) に narrative を賭ける」のを防ぐ。
    from src.cti.source_basis import classify_source_tier
    from src.synthesis.grounded.chronology import article_chronology

    def _col(row: Any, name: str) -> str:
        try:
            return str(row[name] or "")
        except (KeyError, IndexError):
            return ""

    for r in rows:
        axes = [aid for aid, col in axis_cols if r[col]]
        chrono = article_chronology(
            report=_col(r, "created_at"),
            event_date=_col(r, "event_date"),
            event_date_basis=_col(r, "event_date_basis"),
        )
        out.append(
            {
                "title": r["title"],
                "feed_title": r["feed_title"],
                "article_id": r["article_id"],
                "importance": str(r["importance"] or ""),
                "axes": axes,
                # A: NATO Admiralty 2 軸 — source 信頼度 (tier) + 情報の確度 (editorial_stance)。
                "reliability": classify_source_tier(r["feed_title"] or "", _col(r, "feed_url")),
                "stance": _col(r, "editorial_stance"),
                # 時系列事実 (発生日時の前後関係)。因果は断定しない。
                "chronology": chrono.label,
                "resurfaced": chrono.resurfaced,
            },
        )
    return out


def _build_trend_clusters_data(
    *,
    lookback_hours: int,
    baseline_weeks: int,
    db_path: Path,
) -> list[dict[str, Any]]:
    clusters = aggregate_trends(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        db_path=db_path,
    )
    out: list[dict[str, Any]] = []
    for c in clusters[:20]:  # LLM 入力を絞る
        out.append(
            {
                "name": c.name,
                "total_count": c.total_count,
                "sources": list(c.sources),
                "is_new": c.is_new,
                "spike_ratio": "inf" if c.spike_ratio == float("inf") else f"{c.spike_ratio:.1f}",
            },
        )
    return out


def _render_prompt(
    *,
    period_type: str,
    period_label: str,
    baseline_weeks: int,
    axes_data: list[dict[str, Any]],
    trend_clusters: list[dict[str, Any]],
    high_importance_articles: list[dict[str, Any]],
    nation_correlation: list[dict[str, Any]] | None = None,
    nation_window_days: int = 7,
    forecast_indicators: list[dict[str, Any]] | None = None,
    freshness: dict[str, Any] | None = None,
    previous_synthesis: dict[str, Any] | None = None,
) -> str:
    # 層分け (2026-08-20、2 本目): 編集層の SSoT は DB (config_store,
    # key=status_synthesis_rubric)。合成に失敗したら **必ず** legacy .j2 に落ちる
    # (WARNING を残す = 無音にしない)。rollback: SYNTHESIS_COMPOSER=0。
    template = None
    from src.prompts.prompt_store import build_prompt_template
    from src.prompts.registry import get_spec

    spec = get_spec("status_synthesis")
    if spec is not None:
        template = build_prompt_template(spec, PROMPTS_DIR / SYNTHESIS_TEMPLATE)
    if template is None:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        template = env.get_template(SYNTHESIS_TEMPLATE)

    # Phase Diamond verify-pir-driven: config/delivery/pir.yaml の PIR list を context として注入。
    # 空 / 不在なら pir_context=[] で legacy 挙動 (template 側で空 list を扱う)。
    try:
        from src.pir.integration import build_synthesis_pir_context, get_pir_config

        pir_context = build_synthesis_pir_context(get_pir_config().priorities)
    except Exception:  # noqa: BLE001 — PIR システム障害で synthesis を止めない
        pir_context = []

    return template.render(
        period_type=period_type,
        period_label=period_label,
        baseline_weeks=baseline_weeks,
        axes_data=axes_data,
        trend_clusters=trend_clusters,
        high_importance_articles=high_importance_articles,
        nation_correlation=nation_correlation or [],
        nation_window_days=nation_window_days,
        forecast_indicators=forecast_indicators or [],
        freshness=freshness or {},
        pir_context=pir_context,
        previous_synthesis=previous_synthesis,
    )


def _load_previous_synthesis(
    *,
    period_type: str,
    before: datetime,
    db_path: Path,
) -> dict[str, Any] | None:
    """FC1: 同 period_type の「前期」(period_start < before) 総括を 1 件返す。

    synthesis の reasoning に継続性を持たせ、予測 (spillover) の説明責任の基礎にする。
    障害時は None (legacy 挙動)。
    """
    try:
        from src.storage.run_history import RunHistoryRepository

        repo = RunHistoryRepository(db_path)
        for rec in repo.list_synthesis(period_type=period_type, limit=3):
            if rec.period_start < before:
                # B(2): 前期の構造化予測 (tradecraft.forecasts) を採点対象として渡す。
                prior_forecasts: list[str] = []
                try:
                    tc = json.loads(rec.tradecraft) if rec.tradecraft else {}
                    for fc in tc.get("forecasts", []) if isinstance(tc, dict) else []:
                        if isinstance(fc, dict) and str(fc.get("claim", "")).strip():
                            prior_forecasts.append(str(fc["claim"]).strip())
                except (json.JSONDecodeError, ValueError, TypeError):
                    prior_forecasts = []
                return {
                    "period_label": rec.period_start.date().isoformat(),
                    "headline": rec.headline,
                    "cog_section": rec.cog_section,
                    "spillover_section": rec.spillover_section,
                    "forecasts": prior_forecasts,
                }
    except Exception as e:  # noqa: BLE001
        _log.warning("previous_synthesis_load_failed", error=str(e))
    return None


def _sanitize_article_id(raw: str) -> str:
    """LLM が転記時に付けた前後の空白を落とす。

    以前は撤去済み feed 集約サービスの id 形式 (tag:google.com,2005:reader/item/…) に
    LLM が挿入する noise を救済していたが、その形式の記事は 2026-05-26 を最後に
    増えず synthesis の対象窓 (7/30 日) にも入らないため撤去した。
    """
    return raw.strip() if raw else raw


def _axis_min_events(total_current: int) -> int:
    """Phase 3 A2: 軸別 article 件数に応じた axes_evidence events 数の下限。

    LLM が 1-2 events に過剰圧縮するのを防ぐ。
    """
    if total_current >= 100:
        return 5
    if total_current >= 50:
        return 3
    if total_current >= 20:
        return 2
    if total_current >= 1:
        return 1
    return 0


def _check_axes_evidence_coverage(
    axes_evidence: dict[str, list[dict[str, Any]]],
    axes_data: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """各軸の axes_evidence が件数依存の下限を満たすか確認、不足軸を返す。"""
    shortage: list[dict[str, Any]] = []
    counts_by_axis = {a["axis_id"]: int(a["total_current"]) for a in axes_data}
    for axis_id, articles in counts_by_axis.items():
        required = _axis_min_events(articles)
        delivered = len(axes_evidence.get(axis_id, []))
        if delivered < required:
            shortage.append(
                {
                    "axis_id": axis_id,
                    "article_count": articles,
                    "required": required,
                    "delivered": delivered,
                },
            )
    return shortage


def _coerce_pir_section(value: Any) -> str:
    """LLM が pir_section に dict を返してしまった場合に markdown checklist に整形。

    期待: 文字列 (markdown). 実際は LLM が `{"PIR 1: ...": "✅ ..."}` を
    返すケースを観測。Python dict として渡ってきたら ``- **{k}**: {v}`` 形式に変換する。
    """
    if isinstance(value, dict):
        lines: list[str] = []
        for k, v in value.items():
            lines.append(f"- **{str(k).strip()}**: {str(v).strip()}")
        return "\n".join(lines)
    if isinstance(value, list):
        # まれに list of strings で返るケース
        return "\n".join(f"- {str(item).strip()}" for item in value)
    return str(value).strip()


def _sanitize_axes_evidence(value: Any) -> dict[str, list[dict[str, Any]]]:
    """axes_evidence の article_id を sanitize。構造異常時は空 dict。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for axis_id, events in value.items():
        if not isinstance(events, list):
            continue
        cleaned_events: list[dict[str, Any]] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            ids_raw = ev.get("article_ids") or []
            sanitized_ids = [_sanitize_article_id(str(a)) for a in ids_raw if a]
            cleaned_events.append(
                {
                    "label": str(ev.get("label", "")).strip(),
                    "summary": str(ev.get("summary", "")).strip(),
                    "article_ids": sanitized_ids,
                },
            )
        out[str(axis_id)] = cleaned_events
    return out


def _sanitize_tradecraft(value: Any) -> str:
    """S2: 分析トレードクラフトを検証して JSON 文字列化。LLM 省略/構造異常時は空 {}。

    leading_assessment(主見立て) + alternatives(対立仮説) + key_assumptions(前提)
    + indicators(覆る指標)。各 list は最大 4 件に制限。
    """
    if not isinstance(value, dict):
        return "{}"

    def _strlist(v: Any) -> list[str]:
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()][:4]

    out = {
        "leading_assessment": str(value.get("leading_assessment", "")).strip(),
        "alternatives": _strlist(value.get("alternatives")),
        "key_assumptions": _strlist(value.get("key_assumptions")),
        "indicators": _strlist(value.get("indicators")),
        # 構造的矯正 (2026-06-28): 注入信号への明示応答を強制する必須フィールド。
        # source_caveat=出典割引 / forecast_alignment=FC2予測整合 / freshness_note=振り返り率判定。
        "source_caveat": str(value.get("source_caveat", "")).strip(),
        "forecast_alignment": str(value.get("forecast_alignment", "")).strip(),
        "freshness_note": str(value.get("freshness_note", "")).strip(),
        # B(2): 予測 + 前期予測の採点 (的中率 calibration の基礎)。
        "forecasts": _forecast_list(value.get("forecasts")),
        "forecast_scorecard": _scorecard_list(value.get("forecast_scorecard")),
    }
    return json.dumps(out, ensure_ascii=False)


_FORECAST_HORIZONS = frozenset({"next_period", "next_week", "next_month"})
_FORECAST_CONF = frozenset({"high", "medium", "low"})
_SCORE_VERDICTS = frozenset({"realized", "partial", "missed"})


def _forecast_list(value: Any) -> list[dict[str, str]]:
    """B(2) forecasts: [{claim, horizon, confidence}] を検証 (最大 3 件)。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for v in value:
        if not isinstance(v, dict):
            continue
        claim = str(v.get("claim", "")).strip()
        if not claim:
            continue
        horizon = str(v.get("horizon", "")).strip().lower()
        conf = str(v.get("confidence", "")).strip().lower()
        out.append(
            {
                "claim": claim,
                "horizon": horizon if horizon in _FORECAST_HORIZONS else "next_period",
                "confidence": conf if conf in _FORECAST_CONF else "medium",
            }
        )
        if len(out) >= 3:
            break
    return out


def _scorecard_list(value: Any) -> list[dict[str, str]]:
    """B(2) forecast_scorecard: [{claim, verdict, reason}] を検証 (最大 5 件)。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for v in value:
        if not isinstance(v, dict):
            continue
        claim = str(v.get("claim", "")).strip()
        verdict = str(v.get("verdict", "")).strip().lower()
        if not claim or verdict not in _SCORE_VERDICTS:
            continue
        out.append({"claim": claim, "verdict": verdict, "reason": str(v.get("reason", "")).strip()})
        if len(out) >= 5:
            break
    return out


def _parse_synthesis_json(text: str) -> dict[str, Any] | None:
    """LLM 出力から JSON を抽出。fenced block + 前置きに耐える。"""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    # 最初の { から最後の } まで
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError as e:
        _log.warning("synthesis_json_parse_failed", error=str(e))
        return None


def _resolve_period(
    *,
    period_type: str,
    now: datetime | None = None,
) -> tuple[datetime, datetime, str, int, int]:
    """period_type から (start, end, label, lookback_hours, baseline_weeks) を導出。

    Phase Diamond fix: label は LLM prompt に注入されるため JST 表記とする
    (旧 strftime は UTC 値を local 風に表示し、JST CTI 担当者向け narrative の
    中で UTC 時刻を示してしまう問題があった)。
    """
    from zoneinfo import ZoneInfo

    jst = ZoneInfo("Asia/Tokyo")
    base = now or datetime.now(UTC)
    base_jst = base.astimezone(jst)
    if period_type == "daily":
        # period_start を当日 JST 00:00 に固定。 trigger 時刻に関わらず同 day の
        # synthesis は (period_type, period_start) UNIQUE で 1 row になり、
        # ON CONFLICT UPSERT で最新 trigger が前回を上書きする設計。
        # article fetch range は lookback_hours=24 で別途決まる (start とは独立)。
        day_start_jst = base_jst.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start_jst.astimezone(UTC)
        end = base
        label = (
            f"{day_start_jst.strftime('%Y-%m-%d %H:%M')} 〜 "
            f"{base_jst.strftime('%Y-%m-%d %H:%M')} JST"
        )
        return start, end, label, 24, 2
    if period_type == "weekly":
        # 案2 (深夜バッチ化): 「完結した前週」を総括する。period_start=前週月曜 /
        # period_end=当週月曜 00:00 (= ISO 週単位の 1 row、end-start=厳密 7d)。月曜未明に
        # 実行しても前週全体を指す。article fetch は lookback_hours=168 から別途 (深夜実行ゆえ
        # 前週とほぼ一致、境界の数時間差は無視可)。
        days_from_monday = base_jst.weekday()  # Mon=0
        this_monday = base_jst.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days_from_monday
        )
        prev_monday = this_monday - timedelta(days=7)
        start = prev_monday.astimezone(UTC)
        end = this_monday.astimezone(UTC)
        label = (
            f"{prev_monday.strftime('%Y-%m-%d')} 〜 {this_monday.strftime('%Y-%m-%d')} JST (前週)"
        )
        return start, end, label, 168, 4
    if period_type == "monthly":
        # 案2: 「完結した前月」を総括 (1 日未明実行)。period_start=前月1日 / period_end=当月1日。
        this_month_1st = base_jst.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        prev_month_1st = (this_month_1st - timedelta(days=1)).replace(day=1)
        start = prev_month_1st.astimezone(UTC)
        end = this_month_1st.astimezone(UTC)
        label = (
            f"{prev_month_1st.strftime('%Y-%m-%d')} 〜 "
            f"{this_month_1st.strftime('%Y-%m-%d')} JST (前月)"
        )
        return start, end, label, 720, 3
    raise ValueError(f"unknown period_type: {period_type}")


async def generate_synthesis(
    *,
    llm: LLMClient,
    period_type: str = "weekly",
    now: datetime | None = None,
    db_path: Path = Path("data/run_history.db"),
    fast_llm: LLMClient | None = None,
    analysis_llm: LLMClient | None = None,
) -> SynthesisGenerationResult:
    """LLM ベースで status synthesis を生成して StatusSynthesisRecord を返す。

    SYNTHESIS_GROUNDED=1 のとき証拠駆動パイプライン (証拠接地→ACH→敵対的検証→較正→射影)
    に委譲する。shadow のときは旧 single-pass の結果を返しつつ grounded を並走生成して log 比較。
    ``analysis_llm`` は grounded の構造化分析 (ACH/射影) 用 (reasoning ティア)。None なら
    ``llm`` を流用 (挙動保存)。``llm`` は narrative ティア (散文、legacy 経路で使用)。
    """
    from src.synthesis.grounded.pipeline import generate_grounded_synthesis, grounded_mode

    mode = grounded_mode()
    if mode == "on":
        return await generate_grounded_synthesis(
            llm=llm,
            period_type=period_type,
            now=now,
            db_path=db_path,
            fast_llm=fast_llm,
            analysis_llm=analysis_llm,
        )
    if mode == "shadow":
        # 旧出力は維持しつつ grounded を並走生成して log 比較 (品質検証用、本番出力は不変)。
        try:
            shadow = await generate_grounded_synthesis(
                llm=llm,
                period_type=period_type,
                now=now,
                db_path=db_path,
                fast_llm=fast_llm,
                analysis_llm=analysis_llm,
            )
            if shadow.record is not None:
                _log.info(
                    "synthesis_grounded_shadow",
                    period_type=period_type,
                    headline=shadow.record.headline[:120],
                )
        except Exception as e:  # noqa: BLE001 — shadow 失敗で本番 synthesis を止めない
            _log.warning("synthesis_grounded_shadow_failed", error=str(e))

    start, end, label, lookback_hours, baseline_weeks = _resolve_period(
        period_type=period_type,
        now=now,
    )

    axes_data = _build_axes_data(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        db_path=db_path,
    )
    trend_clusters = _build_trend_clusters_data(
        lookback_hours=lookback_hours,
        baseline_weeks=baseline_weeks,
        db_path=db_path,
    )
    high_importance = _build_high_importance_cross_axis(
        lookback_hours=lookback_hours,
        db_path=db_path,
    )
    article_count = sum(a["total_current"] for a in axes_data)

    if article_count == 0:
        _log.info("synthesis_no_articles", period_type=period_type)
        return SynthesisGenerationResult(
            record=None,
            raw_response="",
            error="no articles in period",
        )

    previous_synthesis = _load_previous_synthesis(
        period_type=period_type,
        before=start,
        db_path=db_path,
    )
    # #1 国家横断 相関 + B(1) FC2 構造予測 + C 振り返り率 を **横断 assessment context** として
    # 一度に構築 (出力中心→状態中心: docs/synthesis_assessment_architecture.md)。synthesis の
    # 対象期間と同一窓に揃える (時間軸の混在を避ける)。daily→1 / weekly→7 / monthly→30。
    # 報告ストリーム基準であり実発生時刻ではない。各 builder は障害時に空で legacy 挙動。
    nation_window_days = max(1, round(lookback_hours / 24))
    assessment = build_assessment_context(
        nation_window_days=nation_window_days,
        freshness_lookback_hours=lookback_hours,
        db_path=db_path,
    )
    prompt = _render_prompt(
        period_type=period_type,
        period_label=label,
        baseline_weeks=baseline_weeks,
        axes_data=axes_data,
        trend_clusters=trend_clusters,
        high_importance_articles=high_importance,
        nation_correlation=assessment.nation_correlation,
        nation_window_days=nation_window_days,
        forecast_indicators=assessment.forecast_indicators,
        freshness=assessment.freshness,
        previous_synthesis=previous_synthesis,
    )
    _log.info(
        "synthesis_llm_request",
        period_type=period_type,
        prompt_chars=len(prompt),
        article_count=article_count,
    )
    try:
        response: LLMResponse = await llm.generate(
            prompt=prompt,
            temperature=SYNTHESIS_TEMPERATURE,
            max_tokens=SYNTHESIS_MAX_TOKENS,
            # think 方針は factory (narrative ティア + ThinkOnClient) が所有。
            # ここは安全既定 False (gemma_4_thinking_breaks_digests)。
            think=False,
        )
    except Exception as e:  # noqa: BLE001
        _log.error("synthesis_llm_failed", error=str(e))
        return SynthesisGenerationResult(
            record=None,
            raw_response="",
            error=f"llm: {type(e).__name__}: {e}",
        )

    raw = response.text or ""
    parsed = _parse_synthesis_json(raw)
    if not parsed:
        return SynthesisGenerationResult(
            record=None,
            raw_response=raw,
            error="failed to parse JSON",
        )

    # 必須 field 検証 (Phase Diamond verify: 不足 field を 1 回 retry で救済)
    required = [
        "headline",
        "weight_section",
        "chain_section",
        "cog_section",
        "spillover_section",
        "pir_section",
        "axes_evidence",
    ]
    missing = [k for k in required if k not in parsed]
    if missing:
        _log.warning(
            "synthesis_missing_fields_retry",
            period_type=period_type,
            missing=missing,
            first_attempt_chars=len(raw),
        )
        # 不足 field を明示注入して 1 回 retry。temperature を上げて出力 patterns を変える
        retry_prompt = (
            prompt
            + "\n\n# 再試行注意 (前回欠落 field)\n"
            + f"前回の出力で次の必須 field が欠落していました: {missing}\n"
            + "**全ての required field を必ず出力すること**:\n"
            + "- "
            + "\n- ".join(required)
            + "\n"
            + (
                "特に ``axes_evidence`` は 8 軸 "
                "(P/M/E/S/I-infra/I-cyber/P-env/T) すべての key を含むこと。"
            )
        )
        try:
            response2 = await llm.generate(
                prompt=retry_prompt,
                temperature=min(SYNTHESIS_TEMPERATURE + 0.1, 0.5),
                max_tokens=SYNTHESIS_MAX_TOKENS,
                think=False,
            )
            raw2 = response2.text or ""
            parsed2 = _parse_synthesis_json(raw2)
            if parsed2:
                missing2 = [k for k in required if k not in parsed2]
                if not missing2:
                    _log.info("synthesis_retry_succeeded", period_type=period_type)
                    parsed = parsed2
                    raw = raw2
                    missing = []
                else:
                    _log.warning(
                        "synthesis_retry_still_missing",
                        period_type=period_type,
                        missing=missing2,
                    )
        except Exception as e:  # noqa: BLE001
            _log.warning("synthesis_retry_exception", error=str(e))
        if missing:
            return SynthesisGenerationResult(
                record=None,
                raw_response=raw,
                error=f"missing fields: {missing}",
            )

    # axes_evidence の article_id sanitize (LLM transcription noise の救済)
    axes_evidence_sanitized = _sanitize_axes_evidence(parsed["axes_evidence"])
    # Phase 3 A2: 軸別最低 events 数の充足チェック (under-delivery diagnostics)
    shortage = _check_axes_evidence_coverage(axes_evidence_sanitized, axes_data)
    if shortage:
        _log.warning(
            "synthesis_axes_evidence_under_delivered",
            period_type=period_type,
            shortage=shortage,
        )
    axes_evidence_json = json.dumps(axes_evidence_sanitized, ensure_ascii=False)

    record = StatusSynthesisRecord(
        period_type=period_type,
        period_start=start,
        period_end=end,
        headline=str(parsed["headline"]).strip(),
        weight_section=str(parsed["weight_section"]).strip(),
        chain_section=str(parsed["chain_section"]).strip(),
        cog_section=str(parsed["cog_section"]).strip(),
        spillover_section=str(parsed["spillover_section"]).strip(),
        # PIR は LLM が dict を返した場合に markdown checklist に整形
        pir_section=_coerce_pir_section(parsed["pir_section"]),
        axes_evidence=axes_evidence_json,
        # S2: 分析トレードクラフト (主見立て+対立仮説+前提+覆る指標)。LLM が省略しても安全。
        tradecraft=_sanitize_tradecraft(parsed.get("tradecraft")),
        article_count=article_count,
        llm_model=response.model,
    )
    _log.info(
        "synthesis_generated",
        period_type=period_type,
        article_count=article_count,
        headline_chars=len(record.headline),
    )
    return SynthesisGenerationResult(record=record, raw_response=raw, error=None)
