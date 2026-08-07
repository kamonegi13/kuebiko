"""アクター行動史の週次蒸留ジョブ (アクター辞書 Phase1 F7)。

subject 記事の取込時判定を actor_observed_profile (月次期間行) へ決定論射影する。
LLM 不使用・数百行規模の集計のみで軽量 (実測 subject 記事 ~400 件/月)。

蒸留規律 (2026-07-26 確定):
- 定常実行は **当月+前月のみ** 再蒸留 (週次実行のため月末尾の数日が翌月初回実行まで
  未集計になる取りこぼしを前月再蒸留で回収する)。それ以前の月には不干渉。
- 初回 (テーブル空) は SUBJECT_EPOCH_MONTH (2026-07、主題判定層稼働) から全月 backfill。
- 上流訂正時の窓内再蒸留は正当 (行は判断記録ではなく決定論射影) — 承認時有界再帰属
  (actor_reattribution) も同じ distill_and_store を呼ぶ。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.cti.actor_observed_history import (
    DISTILL_ENTITY_TYPES,
    SUBJECT_EPOCH_MONTH,
    distill_month,
    month_label,
    month_window_utc,
    months_between,
)
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)


def _kev_cve_set() -> frozenset[str]:
    """KEV 掲載 CVE 集合 (取得失敗時は空集合 — kev_hits が 0 に倒れるだけで蒸留は続行)。"""
    try:
        from src.tools.kev_client import get_kev_cve_set

        return get_kev_cve_set()
    except Exception as e:  # noqa: BLE001 — KEV 欠落は蒸留全体を殺す理由にならない
        _log.warning("actor_distill_kev_unavailable", error=str(e))
        return frozenset()


def distill_and_store(
    repo: RunHistoryRepository, months: list[str], *, kev_cves: frozenset[str] | None = None
) -> dict[str, int]:
    """指定月を再蒸留して全置換保存する (job / 承認時再帰属の共通経路)。"""
    kev = _kev_cve_set() if kev_cves is None else kev_cves
    profiles_written = 0
    articles_seen = 0
    for month in months:
        since, before = month_window_utc(month)
        rows = repo.list_subject_article_rows(since, before)
        article_ids = sorted({str(r["article_id"]) for r in rows})
        entities = repo.list_entity_pairs_for_articles(article_ids, DISTILL_ENTITY_TYPES)
        profiles = distill_month(month, rows, entities, kev)
        profiles_written += repo.replace_actor_month_profiles(month, profiles)
        articles_seen += len(article_ids)
        _log.info(
            "actor_distill_month_done",
            month=month,
            actors=len(profiles),
            articles=len(article_ids),
        )
    return {"months": len(months), "profiles": profiles_written, "articles": articles_seen}


def months_to_distill(repo: RunHistoryRepository, *, now: datetime | None = None) -> list[str]:
    """定常実行の対象月 (当月+前月、epoch 未満は除外)。テーブル空なら全月 backfill。"""
    current = month_label(now or datetime.now(UTC))
    if repo.count_actor_profile_rows() == 0:
        return months_between(SUBJECT_EPOCH_MONTH, current)
    all_months = months_between(SUBJECT_EPOCH_MONTH, current)
    return all_months[-2:] if len(all_months) >= 2 else all_months


# 週次 title 層スイープの窓 (週次実行 + 再蒸留対象の前月をカバーする余裕)
_SWEEP_WINDOW_DAYS = 45


def title_layer_sweep(repo: RunHistoryRepository, *, days: int = _SWEEP_WINDOW_DAYS) -> int:
    """subject 未評価記事へ決定論の title 層判定を適用する (蒸留前の供給スイープ)。

    ransomware.live 等の直接取込経路は briefing 永続化 (取込時の主題判定点) を通らず
    subject が NULL のまま残る — 放置すると行動史がそれらのアクターで無音欠落する
    (供給網無監視の教訓)。LLM 不使用・title のみのため安全に全件適用できる。
    """
    from datetime import timedelta

    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.subject_actor import SOURCE_TITLE, determine_subject_actors

    registry = load_actor_aliases()
    since = datetime.now(UTC) - timedelta(days=days)
    updated = 0
    for row in repo.list_unevaluated_titles(since):
        subj = determine_subject_actors(
            titles=(str(row.get("title") or ""),),
            detected_actor_ids=(),
            llm_primary_actor_id="",
            llm_confidence="",
            category=str(row.get("category") or "") or None,
            registry=registry,
        )
        if subj.source == SOURCE_TITLE and subj.ids:
            repo.update_subject_actor_fields(
                str(row["article_id"]),
                ids_csv=",".join(subj.ids),
                source=SOURCE_TITLE,
                confidence=None,
            )
            updated += 1
    return updated


async def run_actor_history_distill(repo: RunHistoryRepository | None = None) -> dict[str, int]:
    """週次 bespoke ジョブ本体 (title 層スイープ → 蒸留)。統計 dict を返す。"""
    repo = repo or RunHistoryRepository()
    swept = title_layer_sweep(repo)
    months = months_to_distill(repo)
    stats = {"swept_titles": swept, **distill_and_store(repo, months)}
    _log.info("actor_history_distill_done", **stats)
    return stats
