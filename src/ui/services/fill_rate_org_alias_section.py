"""被害組織名の別名辞書 (org_alias_dict) の鮮度の監査行 (ORG-D-IMPL、2026-10-31)。

辞書は日次の書き込みが無い静的な表なので、per-day の METRICS ではなく鮮度の行で見る。
``EVENTNEWS_ORG_ALIAS_SOURCE=dict`` のときだけ、最後の refresh が古い・空なら WARN。
それ以外 (既定 pairs) は情報行のみ (WARN にしない)。現在の pairs との差分 (追加/変更/削除) も出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.eventnews.org_alias_dict import DICT_SOURCE_PAIRS, build_dict_mapping, diff_mapping
from src.storage.run_history import RunHistoryRepository
from src.ui.services.eventnews_org_alias import SOURCE_DICT, org_alias_source

#: 最後の refresh がこの日数より古ければ WARN (dict 運用時)。
MAX_AGE_DAYS = 14


@dataclass(frozen=True)
class OrgAliasStats:
    rows: int
    last_updated_at: str | None
    added: int
    changed: int
    removed: int


def collect_org_alias_stats(repo: RunHistoryRepository) -> OrgAliasStats:
    rows, last = repo.org_alias_dict_stats(source=DICT_SOURCE_PAIRS)
    current = repo.load_org_alias_dict()
    # 伏字の除外は script 側の仕事。ここでは素の pairs との差を目安として出す
    added, changed, removed = diff_mapping(
        current, build_dict_mapping(repo.list_org_identity_same_pairs())
    )
    return OrgAliasStats(rows, last, len(added), len(changed), len(removed))


def _age_days(last: str, now: datetime) -> float | None:
    try:
        ts = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts) / timedelta(days=1)


def org_alias_line(stats: OrgAliasStats, *, now: datetime) -> tuple[str, bool]:
    """(表示行, WARN か)。dict 運用のときだけ WARN になりうる。"""
    age = _age_days(stats.last_updated_at, now) if stats.last_updated_at else None
    age_txt = f"{age:.0f}日前" if age is not None else "未構築"
    line = (
        f"別名辞書: {stats.rows} 行・最終更新 {age_txt}・"
        f"pairs との差 +{stats.added}/~{stats.changed}/-{stats.removed}"
    )
    if org_alias_source() != SOURCE_DICT:
        return line + " (参照元 pairs・参考)", False
    stale = stats.rows == 0 or age is None or age > MAX_AGE_DAYS
    if stale:
        return "⚠ " + line + f" — 参照元 dict なのに {MAX_AGE_DAYS} 日以上更新なし/空", True
    return line, False
