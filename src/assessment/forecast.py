"""Situation indicators の forecast lifecycle (open→scored) と報告射影 (監査 2026-07-05 P4)。

indicators は「今後観測されれば判定が変わる」**反証可能な予測**。従来は生成して
render に列挙するだけで、当たったか外れたかを誰も採点していなかった (設計 doc §3.6
が予約した render.py の forecast_alignment/forecasts/forecast_scorecard/freshness_note
は空ハードコード)。ここで lifecycle を決定論で閉じる:

- **open**: active Situation の最新 revision の indicator ごとに open forecast を 1 本張る
- **hit**: 後続 revision の fired_indicators (I&W 照合、既存) に一致 → 的中
- **expired**: horizon (既定 30 日) 発現なし、または Situation close → 未発現

採点結果は tradecraft (forecast_scorecard) に射影され、既存 UI (ForecastScorecard) と
forecast_accuracy KPI (較正の実測) がそのまま復活する。LLM は一切呼ばない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.assessment.situation_store import SituationStore
from src.logging_config import get_logger

_log = get_logger(__name__)

# 予測の発現期限。台帳の dormancy (14d/30d) と整合する 30 日。
FORECAST_HORIZON_DAYS = 30
# 期限切れ採点の 1 run あたり上限 (監査 2026-08-01)。運用初期に open した数百件が
# 2026-08-04 から一斉に期限到来し、daily スコアカード窓が「外れ一色」に振れるのを
# 平準化する。cap 超過分は open のまま残り、次 run 以降で順次採点される (1日2run ×
# 50 = 100 件/日の排水能力)。
MAX_HORIZON_EXPIRY_PER_RUN = 50
# 報告に載せる open 予測の上限 (Discord/UI の可読性)
_MAX_FORECASTS_IN_REPORT = 6
# 継続情勢の「裏付けの古さ」を注記する閾値 (日)
_STALE_EVIDENCE_DAYS = 7

_CONF_TO_UI = {"high": "high", "moderate": "medium", "low": "low"}


@dataclass(frozen=True)
class ForecastSyncResult:
    """1 run の forecast 同期結果 (ログ/テスト用)。"""

    opened: int = 0
    hit: int = 0
    expired: int = 0


def _parse_iso(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def update_forecasts(
    *,
    store: SituationStore,
    now: datetime,
    latest_indicators_by_sid: dict[str, list[str]],
    fired_by_sid: dict[str, list[str]],
    active_sids: set[str],
    closed_sids: set[str],
    max_horizon_expiry: int = MAX_HORIZON_EXPIRY_PER_RUN,
) -> ForecastSyncResult:
    """forecast lifecycle を 1 run 分進める (決定論・冪等)。

    順序: 採点 (hit → expiry/close) → 新規 open。同一 run 内で開いた予測を
    即座に期限切れにしない (採点は既存 open 行のみが対象)。
    ``max_horizon_expiry`` = 期限切れ採点の 1 run 上限 (バースト平準化)。
    close 起因の expiry は close イベントと意味的に結び付くため cap 対象外。
    """
    now_iso = now.isoformat()
    opened = hit = expired = 0
    horizon_expired = 0

    for row in store.open_forecast_rows():
        sid = str(row["situation_id"])
        indicator = str(row["indicator"])
        fired = {f.strip() for f in fired_by_sid.get(sid, [])}
        if indicator.strip() in fired:
            store.score_forecast(
                int(row["id"]),
                status="hit",
                note="指標の発火を観測 (I&W 照合)",
                scored_at=now_iso,
            )
            hit += 1
            continue
        if sid in closed_sids:
            store.score_forecast(
                int(row["id"]),
                status="expired",
                note="情勢クローズにより未発現のまま終了",
                scored_at=now_iso,
            )
            expired += 1
            continue
        if horizon_expired >= max_horizon_expiry:
            continue  # cap 超過分は open のまま次 run で採点 (バースト平準化)
        opened_at = _parse_iso(str(row["opened_at"]))
        horizon = timedelta(days=int(row["horizon_days"]))
        if opened_at is not None and now - opened_at > horizon:
            store.score_forecast(
                int(row["id"]),
                status="expired",
                note=f"{int(row['horizon_days'])} 日以内に発現せず",
                scored_at=now_iso,
            )
            expired += 1
            horizon_expired += 1

    # 新規 open (既存 open との重複は indicator 完全一致で防止)
    still_open = {
        (str(r["situation_id"]), str(r["indicator"]).strip()) for r in store.open_forecast_rows()
    }
    for sid in sorted(active_sids):
        for indicator in latest_indicators_by_sid.get(sid, []):
            ind = indicator.strip()
            if not ind or (sid, ind) in still_open:
                continue
            store.open_forecast(
                situation_id=sid,
                indicator=ind,
                opened_at=now_iso,
                horizon_days=FORECAST_HORIZON_DAYS,
            )
            still_open.add((sid, ind))
            opened += 1

    result = ForecastSyncResult(opened=opened, hit=hit, expired=expired)
    _log.info(
        "forecasts_synced",
        opened=opened,
        hit=hit,
        expired=expired,
        open_total=len(still_open),
    )
    return result


def _latest_rev_map(store: SituationStore, sids: list[str]) -> dict[str, Any]:
    """situation_id → 最新 revision (無い sid は含まない)。"""
    out: dict[str, Any] = {}
    for sid in sids:
        rev = store.latest_revision(sid)
        if rev is not None:
            out[sid] = rev
    return out


def _claims_by_sid(store: SituationStore) -> dict[str, str]:
    """situation_id → 最新 claim (表示用、失敗時は空 dict)。"""
    try:
        rows = store.load_situations(("active", "dormant", "closed"))
        latest = _latest_rev_map(store, [r.situation_id for r in rows])
        return {sid: rev.claim for sid, rev in latest.items()}
    except Exception as e:  # noqa: BLE001 — 表示補助の失敗で射影を止めない
        _log.warning("forecast_claims_lookup_failed", error=str(e))
        return {}


def build_forecast_context(
    *,
    store: SituationStore,
    period_start: datetime,
    period_end: datetime,
    now: datetime,
) -> dict[str, Any]:
    """tradecraft 射影用の forecast/freshness コンテキストを組み立てる (決定論)。

    shape は既存 UI/KPI 契約に一致させる:
    - forecasts: [{claim, horizon, confidence}]
    - forecast_scorecard: [{claim, verdict, reason}] (verdict: realized/missed)
    - forecast_alignment / freshness_note: 1-2 文の決定論テキスト
    """
    claims = _claims_by_sid(store)

    def _label(sid: str, indicator: str) -> str:
        claim = claims.get(sid, "")
        prefix = f"[{claim[:30]}] " if claim else ""
        return f"{prefix}{indicator}"

    open_rows = store.open_forecast_rows()
    confidences = _confidences_by_sid(store, {str(r["situation_id"]) for r in open_rows})
    forecasts = [
        {
            "claim": _label(str(r["situation_id"]), str(r["indicator"])),
            "horizon": "next_month",
            "confidence": confidences.get(str(r["situation_id"]), "medium"),
        }
        for r in sorted(open_rows, key=lambda x: str(x["opened_at"]), reverse=True)[
            :_MAX_FORECASTS_IN_REPORT
        ]
    ]

    scored = store.forecasts_scored_between(period_start.isoformat(), period_end.isoformat())
    scorecard = [
        {
            "claim": _label(s["situation_id"], s["indicator"]),
            "verdict": "realized" if s["status"] == "hit" else "missed",
            "reason": s["note"],
        }
        for s in scored
    ]

    n_hit = sum(1 for s in scored if s["status"] == "hit")
    n_miss = len(scored) - n_hit
    if scored:
        alignment = (
            f"本期間に監視指標 {n_hit} 件が発火 (予測的中)、"
            f"{n_miss} 件が未発現のまま期限切れ。open 予測 {len(open_rows)} 件を追跡中。"
        )
    else:
        alignment = (
            f"本期間に採点対象の予測はない (open 予測 {len(open_rows)} 件を追跡中)。"
            if open_rows
            else "本期間に採点対象の予測はない。"
        )

    return {
        "forecasts": forecasts,
        "forecast_scorecard": scorecard,
        "forecast_alignment": alignment,
        "freshness_note": _freshness_note(store, now),
    }


def _confidences_by_sid(store: SituationStore, sids: set[str]) -> dict[str, str]:
    """situation_id → 最新 revision の確度 (UI 語彙 high/medium/low)。"""
    try:
        latest = _latest_rev_map(store, sorted(sids))
        return {sid: _CONF_TO_UI.get(rev.confidence, "medium") for sid, rev in latest.items()}
    except Exception as e:  # noqa: BLE001
        _log.warning("forecast_confidence_lookup_failed", error=str(e))
        return {}


def _freshness_note(store: SituationStore, now: datetime) -> str:
    """継続情勢の裏付けの古さを正直に注記する (確度 decay 不採用の代替、設計 §2.4)。"""
    try:
        rows = store.load_situations(("active",))
    except Exception as e:  # noqa: BLE001
        _log.warning("freshness_lookup_failed", error=str(e))
        return ""
    if not rows:
        return "追跡中の継続情勢はない。"
    stale: list[tuple[str, int]] = []
    for r in rows:
        ts = _parse_iso(str(r.last_evidence_at or ""))
        if ts is None:
            continue
        age_days = (now - ts).days
        if age_days >= _STALE_EVIDENCE_DAYS:
            stale.append((r.title, age_days))
    if not stale:
        return f"追跡中の全 {len(rows)} 情勢に直近 {_STALE_EVIDENCE_DAYS} 日以内の裏付けあり。"
    stale.sort(key=lambda x: -x[1])
    worst_title, worst_age = stale[0]
    return (
        f"{_STALE_EVIDENCE_DAYS} 日以上裏付けのない継続情勢 {len(stale)}/{len(rows)} 件 "
        f"(最古: 「{worst_title[:30]}」最終裏付け {worst_age} 日前)。"
        "継続は最終裏付け時点の評価であり、現在の活動継続は未確認。"
    )
