"""PIR Spotlight 生成 (LLM-driven narrative)。

1 PIR + 直近 article matches → SpotlightRecord (headline / key_events / outlook)。
prompt template は ``prompts/pir_spotlight.j2``。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jinja2
from pydantic import BaseModel, ConfigDict, Field

from src.assessment.context import AssessmentContext, build_assessment_context
from src.cti.source_basis import classify_source_tier
from src.logging_config import get_logger
from src.pir.evaluator import evaluate_pir_matches
from src.pir.models import Pir
from src.spotlight.models import KeyEvent, SpotlightPeriod, SpotlightRecord
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import LLMClient

_log = get_logger(__name__)

_PROMPT_TEMPLATE = "pir_spotlight.j2"
_CANDIDATE_LIMIT = 30  # LLM に渡す article 候補数 (prompt サイズ制御)
_KEY_EVENTS_MIN = 1
_KEY_EVENTS_MAX = 10


class _LLMKeyEvent(BaseModel):
    """LLM 出力の key_events 要素。

    候補一覧の ``[N]`` 番号 (``index``, 1-based) で参照させる。長い article_id を
    LLM にコピーさせると prefix 欠落等で hallucination 扱いになるため index を主とし、
    article_id は後方互換 / fallback 用に optional で受ける。
    """

    model_config = ConfigDict(extra="ignore")

    index: int | None = None
    article_id: str = ""
    rationale: str = ""


class _LLMSpotlightOutput(BaseModel):
    """LLM 出力 JSON のパース用。"""

    model_config = ConfigDict(extra="ignore")

    headline: str = ""
    key_events: list[_LLMKeyEvent] = Field(default_factory=list)
    outlook: str = ""


# ----- period 解決 -----


def _resolve_period(
    period_type: SpotlightPeriod, now: datetime | None = None
) -> tuple[datetime, datetime, str]:
    """period の境界を JST の暦境界にアラインして返す。

    period_start を実行時刻ではなく暦境界 (週=月曜 00:00 / 日=00:00 / 月=1日 00:00 JST)
    に丸めることで、同一 period 内の再生成が UNIQUE(pir_id, period_type, period_start)
    で同一行に UPSERT される (再生成のたびに行が増えるのを防ぐ)。
    match の lookback は呼び出し側で (end - start) として算出され period 長と一致する。
    """
    from zoneinfo import ZoneInfo

    base = now or datetime.now(UTC)
    jst = ZoneInfo("Asia/Tokyo")
    base_jst = base.astimezone(jst)
    day_start = base_jst.replace(hour=0, minute=0, second=0, microsecond=0)
    if period_type == "daily":
        start_jst = day_start
        end_jst = day_start + timedelta(days=1)
    elif period_type == "monthly":
        # 案2: 完結した前月 (1 日未明実行)。period_start=前月1日 / period_end=当月1日。
        this_month_1st = day_start.replace(day=1)
        start_jst = (this_month_1st - timedelta(days=1)).replace(day=1)
        end_jst = this_month_1st
    else:  # weekly: 完結した前週 (ISO 週・月曜起点、深夜実行で前週を覆う)
        this_monday = day_start - timedelta(days=day_start.weekday())
        start_jst = this_monday - timedelta(days=7)
        end_jst = this_monday
    start = start_jst.astimezone(UTC)
    end = end_jst.astimezone(UTC)
    label = (
        f"{start_jst.strftime('%Y-%m-%d')} 〜 {end_jst.strftime('%Y-%m-%d')} JST ({period_type})"
    )
    return start, end, label


# ----- prompt 構築 -----


def _build_prompt(
    pir: Pir,
    candidate_articles: list[dict[str, Any]],
    period_label: str,
    *,
    assessment: AssessmentContext | None = None,
    previous_spotlight: dict[str, Any] | None = None,
    ledger_situations: list[dict[str, Any]] | None = None,
) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(Path("prompts"))),
        autoescape=False,
        keep_trailing_newline=True,
    )
    template = env.get_template(_PROMPT_TEMPLATE)
    return template.render(
        pir=pir,
        candidate_articles=candidate_articles,
        period_label=period_label,
        # 状態中心: synthesis と同一の横断 assessment を射影として注入 (空なら legacy 挙動)。
        nation_correlation=assessment.nation_correlation if assessment else [],
        forecast_indicators=assessment.forecast_indicators if assessment else [],
        freshness=assessment.freshness if assessment else {},
        # 「事象は過去・未来に連なる」: 前期見立てを継続性 context として注入。
        previous_spotlight=previous_spotlight,
        # 段D-5: Situation Ledger の確定判定 (この PIR に紐づく canonical estimate の射影)。
        ledger_situations=ledger_situations or [],
    )


_LEDGER_SITUATIONS_MAX = 8  # spotlight prompt に載せる台帳判定の上限


def _build_ledger_context(
    pir_id: str,
    *,
    period_start_iso: str | None = None,
    period_end_iso: str | None = None,
    store: Any | None = None,
) -> list[dict[str, Any]]:
    """この PIR に紐づく台帳の確定判定 (salience 順・上限つき)。失敗/不在は空 list。

    監査 backlog 2026-07-05 (射影非対称の解消):
    - dormant は除外する (dormancy 閾値 14-30d ≥ spotlight 期間 = 当期に活動なし)。
    - period_end より未来の revision は読まない (backfill 再生成の混入防止)。
    - 期間外の delta を「当期の変化」として語らせない (no_change に中立化して
      standing context としてのみ注入)。
    """
    try:
        from src.assessment.ledger import ledger_mode
        from src.assessment.salience import salience
        from src.assessment.situation_store import SituationStore
        from src.assessment.stateful import _revision_to_judgment

        # 監査 2026-07-05: SYNTHESIS_STATE rollback (off/shadow) 時は台帳更新が止まる。
        # prompt は「台帳が canonical・矛盾させない」と narrative を拘束するため、
        # rollback 後に stale 台帳を canonical として注入し続けると rollback の実効性が
        # 毀損する。ledger_mode() != "on" なら台帳注入を無効化 (spotlight は台帳非依存の
        # 通常経路で生成される)。
        if ledger_mode() != "on":
            return []

        store = store or SituationStore()
        out: list[tuple[float, dict[str, Any]]] = []
        for row in store.load_situations(("active",)):
            if pir_id not in row.pir_ids:
                continue
            rev = (
                store.latest_revision_before(row.situation_id, until_iso=period_end_iso)
                if period_end_iso is not None
                else store.latest_revision(row.situation_id)
            )
            if rev is None:
                continue
            j = _revision_to_judgment(row.situation_id, rev, domain=row.domain)
            in_period = period_start_iso is None or rev.created_at >= period_start_iso
            out.append(
                (
                    salience(j),
                    {
                        "claim": rev.claim,
                        "leading_hypothesis": rev.leading_hypothesis,
                        "confidence": rev.confidence,
                        "delta_type": rev.delta_type if in_period else "no_change",
                        "delta_note": rev.delta_note if in_period else "",
                        "implication": rev.implication,
                    },
                )
            )
        out.sort(key=lambda x: -x[0])
        return [d for _, d in out[:_LEDGER_SITUATIONS_MAX]]
    except Exception as e:  # noqa: BLE001 — 台帳不在でも spotlight は動かす
        _log.warning("spotlight_ledger_context_failed", pir_id=pir_id, error=str(e))
        return []


def _load_previous_spotlight(
    *, pir_id: str, period_type: SpotlightPeriod, before: datetime
) -> dict[str, Any] | None:
    """前期 spotlight を継続性 context として 1 件返す (synthesis の前期注入と同思想)。

    当期の再生成で当期自身を誤認しないよう period_start < before で取得。障害時は None。
    """
    try:
        prev = RunHistoryRepository().get_previous_spotlight(
            pir_id=pir_id, period_type=period_type, before=before
        )
    except Exception as e:  # noqa: BLE001 — 前期取得の障害で生成を止めない
        _log.warning("spotlight_previous_load_failed", pir_id=pir_id, error=str(e))
        return None
    if prev is None:
        return None
    return {
        "period_label": prev.period_start.date().isoformat(),
        "headline": prev.headline,
        "outlook": (prev.outlook or "")[:600],
        "key_event_titles": [ke.title for ke in prev.key_events[:5]],
    }


# ----- 候補 article 取得 -----


def _format_candidates(
    matches: list[Any], limit: int, ttps_by_article: dict[str, list[str]] | None = None
) -> list[dict[str, Any]]:
    """PirMatch list → prompt context 用 dict list 変換 (JST 表示込み)。

    ttps_by_article: 候補記事の抽出済 MITRE technique (Q1: narrative の ID 接地用)。
    """
    from zoneinfo import ZoneInfo

    jst = ZoneInfo("Asia/Tokyo")
    ttps_by_article = ttps_by_article or {}
    out: list[dict[str, Any]] = []
    for m in matches[:limit]:
        try:
            dt = (
                datetime.fromisoformat(m.created_at.replace("Z", "+00:00"))
                if m.created_at
                else None
            )
        except ValueError:
            dt = None
        created_at_jst = dt.astimezone(jst).strftime("%Y-%m-%d %H:%M JST") if dt else ""
        out.append(
            {
                "article_id": m.article_id,
                "title": m.title,
                "url": m.url,
                "feed_title": m.feed_title,
                "importance": m.importance,
                "created_at_jst": created_at_jst,
                # R-C: narrative を接地するため要約を供給 (旧 MVP は ""=タイトルのみ→捏造温床)。
                # prompt 肥大を抑えるため 300 字に切詰め。
                "summary": (getattr(m, "summary", "") or "")[:300],
                # Q1: 抽出済 MITRE technique (実 ID)。narrative の ID 接地に使う (重複除去)。
                "ttps": sorted(set(ttps_by_article.get(m.article_id, [])))[:8],
                # 段2: source 信頼度 tier を事前計算 (synthesis と同じ NATO Admiralty 信頼度軸)。
                # 旧は LLM に feed_title から自力判断させていた → 機械的に渡し較正を確実化。
                "reliability": classify_source_tier(
                    m.feed_title or "", getattr(m, "feed_url", "") or ""
                ),
            }
        )
    return out


def _resolve_event_match(
    ev: _LLMKeyEvent, candidate_matches: list[Any], article_index: dict[str, Any]
) -> Any:
    """LLM の key_event 参照を実 PirMatch に解決する。

    優先順:
      1. ``index`` (候補一覧の [N] 番号、1-based) — 最も確実
      2. ``article_id`` 完全一致
      3. ``article_id`` の suffix 一致 (LLM が ``rss:https://`` prefix を欠落させた等)
    解決不能なら None。
    """
    if ev.index is not None and 1 <= ev.index <= len(candidate_matches):
        return candidate_matches[ev.index - 1]
    aid = (ev.article_id or "").strip()
    if not aid:
        return None
    exact = article_index.get(aid)
    if exact is not None:
        return exact
    for full_aid, m in article_index.items():
        if full_aid.endswith(aid) or aid.endswith(full_aid):
            return m
    return None


# ----- 主 generate entry point -----


async def generate_spotlight(
    pir: Pir,
    *,
    llm: LLMClient,
    period_type: SpotlightPeriod = "weekly",
    candidate_limit: int = _CANDIDATE_LIMIT,
    now: datetime | None = None,
    assessment: AssessmentContext | None = None,
) -> SpotlightRecord | None:
    """1 PIR × 1 period の Spotlight を生成。

    返り値:
        SpotlightRecord: 成功
        None: 該当 article がない (narrative 化不可)
    """
    if not pir.enabled:
        _log.info("spotlight_pir_disabled", pir_id=pir.id)
        return None

    start, end, period_label = _resolve_period(period_type, now)
    lookback_hours = int((end - start).total_seconds() / 3600)

    # evaluator は SQL で「直近 N 件取得後に PIR filter」順序のため、DB scan 範囲が
    # 狭いと PIR match を見逃す。lookback 期間の全 article を scan する大きい limit を渡し、
    # 結果から上位 candidate_limit 件を prompt 用に使う。
    matches = evaluate_pir_matches(pir, lookback_hours=lookback_hours, limit=5000)
    if len(matches) < _KEY_EVENTS_MIN:
        _log.info(
            "spotlight_insufficient_matches",
            pir_id=pir.id,
            count=len(matches),
            min_required=_KEY_EVENTS_MIN,
        )
        return None

    # Q1: 候補記事の抽出済 MITRE technique を取得し narrative に接地させる
    # (LLM が記憶から ID を作るのを防ぎ、実抽出値のみ引かせる)。失敗は無視 (空 map)。
    cand_ids = [m.article_id for m in matches[:candidate_limit]]
    try:
        ttps_by_article = RunHistoryRepository().entities_for_articles(cand_ids, entity_type="ttp")
    except Exception as e:  # noqa: BLE001
        _log.warning("spotlight_ttp_fetch_failed", pir_id=pir.id, error=str(e))
        ttps_by_article = {}

    candidate_articles = _format_candidates(matches, candidate_limit, ttps_by_article)

    # 状態中心 (docs/synthesis_assessment_architecture.md): 横断 assessment (国家相関/forecast/鮮度)
    # を runner から共有 (1 run 1 回計算)。直接呼び出し (API/test) で未指定なら spotlight の対象窓と
    # 同一に揃えて自前構築する (時間軸の混在を避ける)。
    if assessment is None:
        nation_window_days = max(1, round(lookback_hours / 24))
        assessment = build_assessment_context(
            nation_window_days=nation_window_days,
            freshness_lookback_hours=lookback_hours,
            db_path=Path("data/run_history.db"),
        )
    # 「事象は過去・未来に連なる」: 前期 spotlight を継続性 context として注入。
    previous_spotlight = _load_previous_spotlight(
        pir_id=pir.id, period_type=period_type, before=start
    )
    # 段D-5 (Situation Ledger 射影): この PIR に紐づく台帳の確定判定を注入。
    # narrative は台帳の判定・確度に整合させる (源が一つ = 状態中心)。失敗は空 (legacy 挙動)。
    # 期間境界を渡し、期間外 delta の混入 (射影非対称) を防ぐ。
    ledger_situations = _build_ledger_context(
        pir.id,
        period_start_iso=start.isoformat(),
        period_end_iso=end.isoformat(),
    )

    prompt = _build_prompt(
        pir,
        candidate_articles,
        period_label,
        assessment=assessment,
        previous_spotlight=previous_spotlight,
        ledger_situations=ledger_situations,
    )

    _log.info(
        "spotlight_llm_call_start",
        pir_id=pir.id,
        period_type=period_type,
        candidates=len(candidate_articles),
        model=llm.model,
    )

    # 長文 outlook (600-1000 字) + key_events 5-8 件で出力 token は 2.5-3.5k 想定。
    # 余裕を持って 6144 設定。think 方針は factory (model_tiers) が一元所有 — narrative
    # ティアの外部モデル割当時のみ ThinkOnClient が True に上書きする (A/B 2026-07-24)。
    # ここは安全既定 False (ローカル gemma thinking 空応答前歴)。
    output: _LLMSpotlightOutput = await llm.generate_structured(
        prompt=prompt,
        schema=_LLMSpotlightOutput,
        temperature=0.3,
        max_tokens=6144,
        think=False,
    )

    # LLM の参照 (index 優先 / article_id fallback) を実 article match に対応付け
    candidate_matches = matches[:candidate_limit]  # [N] 番号の母体
    article_index = {m.article_id: m for m in matches}
    key_events: list[KeyEvent] = []
    for ev in output.key_events[:_KEY_EVENTS_MAX]:
        m = _resolve_event_match(ev, candidate_matches, article_index)
        if m is None:
            _log.warning(
                "spotlight_unknown_article_id",
                pir_id=pir.id,
                article_id=ev.article_id,
                index=ev.index,
            )
            continue
        key_events.append(
            KeyEvent(
                article_id=m.article_id,
                title=m.title,
                url=m.url,
                feed_title=m.feed_title,
                importance=m.importance,
                published_at=m.created_at,
            )
        )

    record = SpotlightRecord(
        pir_id=pir.id,
        pir_title=pir.title,
        period_type=period_type,
        period_start=start,
        period_end=end,
        headline=output.headline.strip(),
        outlook=output.outlook.strip(),
        key_events=key_events,
        article_count=len(matches),
        llm_model=llm.model,
        generated_at=datetime.now(UTC),
    )

    _log.info(
        "spotlight_generated",
        pir_id=pir.id,
        period_type=period_type,
        key_events=len(key_events),
        headline_len=len(record.headline),
        outlook_len=len(record.outlook),
    )

    return record
