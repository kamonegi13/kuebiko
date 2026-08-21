"""係争の人間裁定 — 較正格子 P4 (docs/self_evolving_tuning_design.md §4 C5、§10.2b)。

裁定対象 = 「パネル (全員一致 or 分裂) × E1 ラベル不一致」の係争。90 日 backfill の
実測でこの不一致は E1 ラベルノイズを誤検出ゼロで指した — 人間は「どちらが誤りか」を
2 択で裁く:

- ``label_wrong``: ラベルが誤り → E1 ラベルを E3 裁定ラベルで置換 (隔離)。
  E3 の値はパネル全員一致ならその値、分裂なら空 (保守側)。
- ``label_correct``: ラベルが正しく判定側の見逃し → E1 は現行のまま、E3 確認ラベルを
  追記 (few-shot の高価値教材候補になる)。

裁定 = 適用の終端保証 (承認キュー 3 原則③): 本 module の resolve_case 1 箇所が
裁定記録とラベル操作を必ず対で行う。TTL 30 日の無応答既定 (§12) は expire_stale_cases —
係争ラベルは保守側で隔離する (正しかった可能性より学習資産の純度を優先)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from src.logging_config import get_logger

_log = get_logger(__name__)

# §12: 無応答時の既定 — 30 日で未裁定アーカイブ
ADJUDICATION_TTL_DAYS = 30

Resolution = Literal["label_wrong", "label_correct"]


@dataclass(frozen=True)
class ResolveResult:
    """1 裁定の結末。"""

    ok: bool
    reason: str = ""
    superseded: int = 0


def _panel_consensus_value(verdicts_json: str) -> str | None:
    """パネル全員一致ならその値、分裂/不明なら None。"""
    try:
        verdicts = json.loads(verdicts_json)
        values = {str(v.get("value", "")) for v in verdicts}
    except (TypeError, ValueError):
        return None
    return values.pop() if len(values) == 1 else None


def resolve_case(repo: Any, *, case_key: str, resolution: Resolution) -> ResolveResult:
    """係争 1 件を裁定する (裁定記録 + ラベル操作を必ず対で行う)。"""
    case = repo.get_panel_verdict_case(case_key)
    if case is None:
        return ResolveResult(ok=False, reason="係争が見つかりません")
    if case["agreement"] not in ("unanimous_wrong", "split"):
        return ResolveResult(ok=False, reason="裁定対象の係争ではありません")

    # 記録が先 (1 係争 1 裁定の関門)。二重裁定はここで止まる
    if not repo.insert_panel_resolution(case_key=case_key, resolution=resolution):
        return ResolveResult(ok=False, reason="この係争は裁定済みです")

    article_id = case["article_id"]
    field = case["field"]
    if resolution == "label_correct":
        e3_value = case["truth_value"]
        provenance = {"producer": "adjudication", "case_key": case_key, "confirms": "E1"}
    else:
        consensus = _panel_consensus_value(case["verdicts"])
        e3_value = consensus if consensus is not None else ""
        provenance = {"producer": "adjudication", "case_key": case_key, "overrides": "E1"}

    e3_id = repo.record_tuning_label(
        dedup_key=f"adjudication:{case_key}",
        field=field,
        label_value=e3_value,
        source="E3",
        strength="strong",
        provenance=json.dumps(provenance, ensure_ascii=False),
        article_id=article_id,
    )
    superseded = 0
    if resolution == "label_wrong" and e3_id is not None and article_id:
        superseded = repo.supersede_active_labels(
            article_id=article_id, field=field, source="E1", superseded_by=e3_id
        )
    _log.info(
        "adjudication_resolved",
        case_key=case_key,
        resolution=resolution,
        superseded=superseded,
    )
    return ResolveResult(ok=True, superseded=superseded)


def expire_stale_cases(
    repo: Any,
    *,
    now: datetime | None = None,
    ttl_days: int = ADJUDICATION_TTL_DAYS,
) -> int:
    """TTL 超過の未裁定係争を expired にする (§12 の無応答既定 — 人間不在でも溜まらない)。

    係争中の E1 ラベルは保守側で隔離する (学習資産の純度 > 1 ラベルの回収)。
    """
    base = now or datetime.now(UTC)
    cutoff = base - timedelta(days=ttl_days)
    expired = 0
    for case in repo.list_pending_adjudications():
        try:
            created = datetime.fromisoformat(str(case["created_at"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if created > cutoff:
            continue
        if not repo.insert_panel_resolution(
            case_key=case["case_key"], resolution="expired", resolved_by="ttl"
        ):
            continue
        expired += 1
        if case["article_id"]:
            # 後継なしの隔離 (sentinel 0)。マーカーラベル方式は「空の active ラベル」を
            # 作り次回パネルの入力に混入するため不採用
            repo.supersede_active_labels(
                article_id=case["article_id"],
                field=case["field"],
                source="E1",
                superseded_by=0,
            )
    if expired:
        _log.info("adjudication_ttl_expired", count=expired, ttl_days=ttl_days)
    return expired
