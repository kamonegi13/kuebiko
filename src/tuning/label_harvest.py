"""遅延正解ラベルの週次収穫 (較正格子 P1 — docs/self_evolving_tuning_design.md §4 C1)。

「当時の判定 vs 後日確定した事実」を突合して tuning_labels に書く。producer は全て
sweep 型 (pull・冪等) — push hook と違い過去分も自動で拾え、人間が一切触らなくても
ラベルが増え続ける (§12 人間非介在の縮退設計)。1 producer の失敗で他を止めない。

P1 の producer (§10.3 の決定):
- ①feed 突合 (E1): 犯行声明 (feed 帰属) と同一 victim_org を ±5 日に報じたニュース記事へ
  主体アクターのラベルを付与 (eval_judgment_subject_recall 2026-08-21 の実証手法)。
- ②MITRE 確定 (E1): mitre_sync の自動適用 hook が書く (本モジュール外、sync 時に確定)。
- E3 合流 (§11-A): taxonomy 採否・editorial 訂正の人間裁定をラベル化。

日付の突合は Python で行う (SQL の per-row 日付演算は SQLite/PG で方言が割れるため。
sqlite_pg_bare_column_group_by 2026-08-04 の教訓 — 可搬でない SQL を書かない)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from src.logging_config import get_logger

_log = get_logger(__name__)

# 犯行声明とニュース記事を同一事象とみなす時間窓 (±日)。eval 2026-08-21 と同値。
_MATCH_WINDOW_DAYS = 5
# 収穫の遡り日数 (週次実行の取りこぼし・遅延到着に余裕を持たせる)
_LOOKBACK_DAYS = 35


class _LabelRepo(Protocol):
    """収穫が必要とする repo 面 (RunHistoryRepository のサブセット)。"""

    def record_tuning_label(
        self,
        *,
        dedup_key: str,
        field: str,
        label_value: str,
        source: str,
        strength: str,
        provenance: str,
        article_id: str | None = ...,
        arrived_at: datetime | None = ...,
        supersede_same: bool = ...,
    ) -> int | None: ...

    def fetch_feed_subject_claims(self, since_iso: str) -> list[dict[str, Any]]: ...

    def fetch_victim_org_news_candidates(self, since_iso: str) -> list[dict[str, Any]]: ...

    def list_taxonomy_proposals(
        self, *, status: str | None = ..., tier: str | None = ..., limit: int = ...
    ) -> list[Any]: ...

    def list_editorial_stance_reviews(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class LabelHarvestResult:
    """1 回の収穫の実行結果 (producer 別の新規件数)。"""

    feed_subject_new: int = 0
    feed_subject_conflicts: int = 0
    taxonomy_new: int = 0
    editorial_new: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return self.feed_subject_new + self.taxonomy_new + self.editorial_new


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


def _record(
    repo: _LabelRepo,
    *,
    dry_run: bool,
    dedup_key: str,
    label_field: str,
    label_value: str,
    source: str,
    provenance: dict[str, Any],
    article_id: str | None = None,
    arrived_at: datetime | None = None,
    supersede_same: bool = False,
) -> bool:
    """1 ラベルの書き込み。dry_run は候補として数えるだけで書かない。"""
    if dry_run:
        return True
    new_id = repo.record_tuning_label(
        dedup_key=dedup_key,
        field=label_field,
        label_value=label_value,
        source=source,
        strength="strong",
        provenance=json.dumps(provenance, ensure_ascii=False),
        article_id=article_id,
        arrived_at=arrived_at,
        supersede_same=supersede_same,
    )
    return new_id is not None


def _sweep_feed_news_subject(
    repo: _LabelRepo,
    *,
    now: datetime,
    dry_run: bool,
) -> tuple[int, int]:
    """収穫① (E1): 犯行声明 × ニュース記事の victim_org ±5 日突合 → 主体ラベル。

    同一 org へ別ギャングの声明が窓内に複数ある場合は正解を決められないので付与しない
    (conflict として数える — 保守側の既定)。
    """
    since_iso = (now - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    claims = repo.fetch_feed_subject_claims(since_iso)
    news = repo.fetch_victim_org_news_candidates(since_iso)

    claims_by_org: dict[str, list[tuple[str, str, datetime]]] = {}
    for c in claims:
        ts = _as_dt(c["created_at"])
        gt = str(c["gt"]).split(",")[0].strip()
        if ts is None or not gt or not c["org"]:
            continue
        claims_by_org.setdefault(str(c["org"]), []).append((str(c["article_id"]), gt, ts))

    new_count = 0
    conflicts = 0
    window = timedelta(days=_MATCH_WINDOW_DAYS)
    for n in news:
        n_ts = _as_dt(n["created_at"])
        org = str(n["org"])
        if n_ts is None or org not in claims_by_org:
            continue
        matched = [
            (claim_id, gt)
            for claim_id, gt, c_ts in claims_by_org[org]
            if abs(n_ts - c_ts) <= window
        ]
        if not matched:
            continue
        gts = {gt for _, gt in matched}
        if len(gts) > 1:
            conflicts += 1
            continue
        gt = next(iter(gts))
        inserted = _record(
            repo,
            dry_run=dry_run,
            dedup_key=f"feedmatch:{n['article_id']}:{gt}",
            label_field="subject_actor",
            label_value=gt,
            source="E1",
            article_id=str(n["article_id"]),
            arrived_at=n_ts,
            supersede_same=True,
            provenance={
                "producer": "feed_victim_org_match",
                "claim_article_id": matched[0][0],
                "org": org,
                "window_days": _MATCH_WINDOW_DAYS,
            },
        )
        new_count += int(inserted)
    return new_count, conflicts


def _sweep_taxonomy_decisions(repo: _LabelRepo, *, dry_run: bool) -> int:
    """E3 合流 (§11-A): taxonomy 提案への人間の採否をラベル化 (保留は裁定ではない)。"""
    new_count = 0
    for status in ("accepted", "rejected"):
        for p in repo.list_taxonomy_proposals(status=status, limit=1000):
            if p.id is None:
                continue
            inserted = _record(
                repo,
                dry_run=dry_run,
                dedup_key=f"taxonomy:{p.id}:{status}",
                label_field="taxonomy_decision",
                label_value=status,
                source="E3",
                arrived_at=p.reviewed_at,
                provenance={
                    "producer": "taxonomy_review",
                    "proposal_id": p.id,
                    "proposal_type": p.proposal_type,
                    "target_yaml": p.target_yaml,
                    "target_canonical": p.target_canonical,
                },
            )
            new_count += int(inserted)
    return new_count


def _sweep_editorial_corrections(repo: _LabelRepo, *, dry_run: bool) -> int:
    """E3 合流 (§11-A): editorial 論調訂正をラベル化 (write-only 解消)。

    自由文コメントは provenance に持ち込まない (§4 マスク方針を書込側で満たす)。
    再訂正は supersede_same で現行 1 本に保つ。
    """
    new_count = 0
    for r in repo.list_editorial_stance_reviews():
        corrected = str(r["corrected_stance"])
        inserted = _record(
            repo,
            dry_run=dry_run,
            dedup_key=f"editorial:{r['article_id']}:{corrected}",
            label_field="editorial_stance",
            label_value=corrected,
            source="E3",
            article_id=str(r["article_id"]),
            arrived_at=_as_dt(r["created_at"]),
            supersede_same=True,
            provenance={
                "producer": "editorial_review",
                "original_stance": r["original_stance"],
            },
        )
        new_count += int(inserted)
    return new_count


def run_label_harvest(
    repo: _LabelRepo,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> LabelHarvestResult:
    """全 producer を実行する (週次 pipeline から呼ばれる)。

    1 producer の例外は errors に落として残りを続行する (収穫全体を止めない)。
    """
    base = now or datetime.now(UTC)
    errors: list[str] = []
    feed_new = feed_conflicts = taxonomy_new = editorial_new = 0

    try:
        feed_new, feed_conflicts = _sweep_feed_news_subject(repo, now=base, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 — 1 producer の失敗で他を止めない
        _log.warning("label_harvest_feed_sweep_failed", error=str(e))
        errors.append(f"feed_subject: {type(e).__name__}: {e}")

    try:
        taxonomy_new = _sweep_taxonomy_decisions(repo, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.warning("label_harvest_taxonomy_sweep_failed", error=str(e))
        errors.append(f"taxonomy: {type(e).__name__}: {e}")

    try:
        editorial_new = _sweep_editorial_corrections(repo, dry_run=dry_run)
    except Exception as e:  # noqa: BLE001
        _log.warning("label_harvest_editorial_sweep_failed", error=str(e))
        errors.append(f"editorial: {type(e).__name__}: {e}")

    result = LabelHarvestResult(
        feed_subject_new=feed_new,
        feed_subject_conflicts=feed_conflicts,
        taxonomy_new=taxonomy_new,
        editorial_new=editorial_new,
        errors=errors,
    )
    _log.info(
        "label_harvest_complete",
        feed_subject_new=result.feed_subject_new,
        feed_subject_conflicts=result.feed_subject_conflicts,
        taxonomy_new=result.taxonomy_new,
        editorial_new=result.editorial_new,
        dry_run=dry_run,
        error_count=len(errors),
    )
    return result
