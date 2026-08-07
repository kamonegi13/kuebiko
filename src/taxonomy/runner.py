"""Phase H: weekly-taxonomy-review pipeline runner。

generate_all_proposals() で 提案を生成 → taxonomy_review_proposals テーブルに
upsert (同 target の pending 提案があれば evidence を refresh、なければ insert)。

源 (source.type='taxonomy_review') から呼ばれる。出力は UI のみ (Discord 配信なし)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository, TaxonomyProposalRecord
from src.taxonomy.proposal_generator import (
    generate_all_proposals,
    proposed_change_to_json,
)

_log = get_logger(__name__)


@dataclass(frozen=True)
class TaxonomyReviewResult:
    """1 回の taxonomy review 実行結果。"""

    total_generated: int
    new_proposals: int
    refreshed_proposals: int
    errors: list[str]


async def run_taxonomy_review(
    *,
    repo: RunHistoryRepository | None = None,
    run_id: int | None = None,
    lookback_days: int = 30,
) -> TaxonomyReviewResult:
    """週次 taxonomy review を実行する。

    Args:
        repo: 永続化用 (None なら DB 書き込みなし、dry-run 相当)
        run_id: 提案に紐付ける run_id
        lookback_days: 集計期間 (default 30 日)
    """
    # 監査 2026-07-05 P6: repo の db_path を検出器に配線する (DI 欠落の修正)。
    # 未配線だと常に DEFAULT_DB_PATH を読み、SQLite fallback (dev/tests) で
    # 「repo に入れたデータが検出器から見えない」— PG 本番では connect が
    # DATABASE_URL に繋ぐため隠れていた実装バグ。
    if repo is not None and getattr(repo, "db_path", None) is not None:
        proposals = generate_all_proposals(lookback_days=lookback_days, db_path=repo.db_path)
    else:
        proposals = generate_all_proposals(lookback_days=lookback_days)

    if repo is None:
        _log.info(
            "taxonomy_review_dry_run",
            total_generated=len(proposals),
        )
        return TaxonomyReviewResult(
            total_generated=len(proposals),
            new_proposals=0,
            refreshed_proposals=0,
            errors=[],
        )

    new_count = 0
    refreshed_count = 0
    skipped_rejected = 0
    errors: list[str] = []
    for gp in proposals:
        change_json = proposed_change_to_json(gp.proposed_change)
        evidence_ids_json = json.dumps(gp.evidence_ids, ensure_ascii=False)
        try:
            # 却下は再提案しない (MITRE sync と同原則)。pending のみの照合だと却下した
            # 同一提案が翌週再 INSERT され、レビューが学習しないループになる。
            if repo.has_rejected_proposal(
                proposal_type=gp.proposal_type,
                target_yaml=gp.target_yaml,
                target_canonical=gp.target_canonical,
                proposed_change=change_json,
            ):
                skipped_rejected += 1
                continue
            existing = repo.find_pending_proposal(
                proposal_type=gp.proposal_type,
                target_yaml=gp.target_yaml,
                target_canonical=gp.target_canonical,
                proposed_change=change_json,
            )
            if existing is not None and existing.id is not None:
                # evidence 件数 / rationale / confidence のみ refresh
                repo.refresh_proposal_evidence(
                    existing.id,
                    evidence_count=gp.evidence_count,
                    evidence_ids=evidence_ids_json,
                    rationale=gp.rationale,
                    confidence=gp.confidence,
                )
                refreshed_count += 1
            else:
                repo.insert_taxonomy_proposal(
                    TaxonomyProposalRecord(
                        run_id=run_id,
                        proposal_type=gp.proposal_type,
                        tier=gp.tier,
                        target_yaml=gp.target_yaml,
                        target_canonical=gp.target_canonical,
                        proposed_change=change_json,
                        rationale=gp.rationale,
                        confidence=gp.confidence,
                        evidence_count=gp.evidence_count,
                        evidence_ids=evidence_ids_json,
                    ),
                )
                new_count += 1
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "taxonomy_proposal_persist_failed",
                proposal_type=gp.proposal_type,
                error=str(e),
            )
            errors.append(f"{gp.proposal_type}: {type(e).__name__}: {e}")

    _log.info(
        "taxonomy_review_complete",
        total_generated=len(proposals),
        new_proposals=new_count,
        refreshed_proposals=refreshed_count,
        skipped_rejected=skipped_rejected,
        errors=len(errors),
    )
    return TaxonomyReviewResult(
        total_generated=len(proposals),
        new_proposals=new_count,
        refreshed_proposals=refreshed_count,
        errors=errors,
    )
