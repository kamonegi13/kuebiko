"""犯行声明突合による主体の決定論補完 (§13 対処 A、2026-08-22)。

記事の主題アクター判定は本文言及集合の候補ゲートを持つため、ギャング名を書かない
被害報道では LLM が構造的に主体を選べない (実測 26% が未到達、
docs/self_evolving_tuning_design.md §13)。一方 ransomware.live の犯行声明
(subject_actor_source='feed') は最強証拠であり、victim_org ±窓日 突合で主体を
決定論的に転移できる。

突合ロジックは src/tuning/label_harvest.py の収穫① (``_sweep_feed_news_subject``) と
**同一セマンティクス** — 突合パラメータ (時間窓 / generic org denylist / 日付比較方式)
の SSoT を割らないため、当該モジュールの private 定数・ヘルパをそのまま再利用する
(``_GENERIC_ORG_DENYLIST`` / ``_MATCH_WINDOW_DAYS`` / ``_as_dt``)。パラメータを変える
場合は label_harvest.py 側の値を直せば両方に反映される。

label_harvest との役割差: label_harvest はラベル台帳 (tuning_labels、観測専用) に
書くだけで記事本体は変更しない。本モジュールは articles.subject_actor_* 列を実際に
埋める (Discord 投稿・PIR 評価等の下流消費者に届く決定論補完)。**既存の主体は
絶対に上書きしない** (feed 突合は本文言及ゲートの外側にある転移証拠のため、
既に何らかの主体判定 (title/llm 等) が入っている記事には手を出さない保守則)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from src.cti.subject_actor import SOURCE_FEED_MATCH
from src.logging_config import get_logger

# 突合パラメータの SSoT は label_harvest 側にある。ここでは再定義せず import して使う
# (パッケージ内 private ヘルパの意図的再利用 — 突合窓/denylist が二重管理化しないよう
# 変更は label_harvest.py 側の値を直すこと)。
from src.tuning.label_harvest import _GENERIC_ORG_DENYLIST, _MATCH_WINDOW_DAYS, _as_dt

_log = get_logger(__name__)

_DEFAULT_LOOKBACK_DAYS = 35


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
    ``update_subject_actor_fields`` を持つ duck type (label_harvest._LabelRepo と同様の
    型付けをしない設計)。突合は _sweep_feed_news_subject と同一 (org 単位に声明を束ね、
    generic org を除外、±_MATCH_WINDOW_DAYS で日付照合、同一 org へ複数ギャングが
    競合する場合は正解を決められないため書かない)。

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
            continue  # 一般語の組織名は同名衝突の誤結合源 (label_harvest §13-1 と同じ)
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
