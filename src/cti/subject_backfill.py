"""犯行声明突合による主体の決定論補完 (2026-08-22)。

記事の主題アクター判定は本文言及集合の候補ゲートを持つため、ギャング名を書かない
被害報道では LLM が構造的に主体を選べない (実測では未到達 29 件のうち 28 件が
「候補ゼロ」= LLM に解けない問題だった)。一方 ransomware.live の犯行声明
(subject_actor_source='feed') は最強証拠であり、victim_org ±5 日 突合で主体を
決定論的に転移できる。LLM 呼出はゼロ。

呼出経路は 2 本 — ransomware_ingest 直後 (主経路) と daily-maintenance (安全網)。

**既存の主体は絶対に上書きしない**: feed 突合は本文言及ゲートの外側にある転移証拠の
ため、既に何らかの主体判定 (title/llm 等) が入っている記事には手を出さない保守則。

来歴: 自己進化チューニング (較正格子) の実験中に派生した部品。実験本体は
2026-08-22 に失敗として撤収したが、本モジュールは決定論のみで動き本番の主体充足に
実効があったため存続させた (撤収の経緯は docs/self_evolving_tuning_design.md)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.cti.subject_actor import SOURCE_FEED_MATCH
from src.logging_config import get_logger

_log = get_logger(__name__)

_DEFAULT_LOOKBACK_DAYS = 35
# 声明 ↔ 報道の日付照合窓
_MATCH_WINDOW_DAYS = 5
# 一般語の組織名は同名衝突で誤結合しやすいため完全一致で除外する最小 denylist
# (victim_org 抽出側の is_vendor_noise とは別軸 — あちらはベンダ言及)
_GENERIC_ORG_DENYLIST = frozenset(
    {
        "government",
        "ministry",
        "police",
        "hospital",
        "university",
        "school",
        "bank",
        "city",
        "council",
        "市役所",
        "病院",
        "大学",
        "政府",
        "警察",
        "学校",
    }
)


def _as_dt(value: Any) -> datetime | None:
    """created_at の両 backend 対応 (SQLite=TEXT / PG=TIMESTAMPTZ or TEXT)。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


@dataclass(frozen=True)
class SubjectBackfillResult:
    """1 回の補完実行の集計 (dry_run でも候補数として埋まる)。"""

    filled: int = 0
    skipped_conflict: int = 0
    already_attributed: int = 0  # 一致したが主体が既に入っていた件数 (上書きしない)
    errors: list[str] = field(default_factory=list)


def run_subject_backfill(
    repo: Any,
    *,
    now: datetime | None = None,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
    dry_run: bool = False,
) -> SubjectBackfillResult:
    """犯行声明 × victim_org 突合で主体未確定の記事を決定論的に埋める。

    ``repo`` は ``fetch_feed_subject_claims`` / ``fetch_victim_org_news_candidates`` /
    ``update_subject_actor_fields`` を持つ duck type。突合は org 単位に声明を束ね、
    generic org を除外、±_MATCH_WINDOW_DAYS で日付照合し、同一 org へ複数ギャングが
    競合する場合は正解を決められないため書かない。

    補完対象は **news 記事の subject_actor_ids が空のときのみ** (fetch 側で
    subject_actor_source='feed' の記事は既に除外済み)。1 記事の書込失敗は errors に
    落として他記事の補完を止めない。同一記事が複数 org で複数回一致しても
    (処理済み article_id の集合で) 二重カウントしない。
    """
    base = now or datetime.now(UTC)
    since_iso = (base - timedelta(days=lookback_days)).isoformat()

    try:
        claims = repo.fetch_feed_subject_claims(since_iso)
        news = repo.fetch_victim_org_news_candidates(since_iso)
    except Exception as e:  # noqa: BLE001 — 取得自体の失敗は全体を止めて報告する
        _log.warning("subject_backfill_fetch_failed", error=str(e))
        return SubjectBackfillResult(errors=[f"fetch: {type(e).__name__}: {e}"])

    claims_by_org: dict[str, list[tuple[str, datetime]]] = {}
    for c in claims:
        ts = _as_dt(c["created_at"])
        gt = str(c["gt"]).split(",")[0].strip()
        org = str(c["org"] or "")
        if ts is None or not gt or not org:
            continue
        if org in _GENERIC_ORG_DENYLIST:
            continue  # 一般語の組織名は同名衝突の誤結合源
        claims_by_org.setdefault(org, []).append((gt, ts))

    filled = 0
    skipped_conflict = 0
    already_attributed = 0
    errors: list[str] = []
    processed_article_ids: set[str] = set()
    window = timedelta(days=_MATCH_WINDOW_DAYS)

    for n in news:
        article_id = str(n["article_id"])
        if article_id in processed_article_ids:
            continue  # 同一記事が複数 org に一致しても二重カウントしない

        org = str(n.get("org") or "")
        n_ts = _as_dt(n["created_at"])
        if n_ts is None or org not in claims_by_org:
            continue

        matched_gts = {gt for gt, c_ts in claims_by_org[org] if abs(n_ts - c_ts) <= window}
        if not matched_gts:
            continue
        processed_article_ids.add(article_id)

        if len(matched_gts) > 1:
            skipped_conflict += 1
            continue

        existing_ids = str(n.get("subject_actor_ids") or "").strip()
        if existing_ids:
            already_attributed += 1  # 既存主体は絶対に上書きしない
            continue

        gt = next(iter(matched_gts))
        try:
            if not dry_run:
                repo.update_subject_actor_fields(
                    article_id, ids_csv=gt, source=SOURCE_FEED_MATCH, confidence=None
                )
            filled += 1
        except Exception as e:  # noqa: BLE001 — 1 記事の失敗で他記事の補完を止めない
            _log.warning("subject_backfill_update_failed", article_id=article_id, error=str(e))
            errors.append(f"{article_id}: {type(e).__name__}: {e}")

    result = SubjectBackfillResult(
        filled=filled,
        skipped_conflict=skipped_conflict,
        already_attributed=already_attributed,
        errors=errors,
    )
    _log.info(
        "subject_backfill_complete",
        filled=result.filled,
        skipped_conflict=result.skipped_conflict,
        already_attributed=result.already_attributed,
        dry_run=dry_run,
        error_count=len(errors),
    )
    return result
