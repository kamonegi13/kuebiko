"""シャドー部品の台帳と期限起票 (§13-8 対処、2026-08-22)。

§10.1 はシャドー先行を義務化したが終了条件が無く、「シャドーのまま忘れる」が
単独運用の既定平衡だった (実装済み主要部品が全て無期限シャドー)。本 module は
コード所有の小さな台帳を持ち、週次ガバナンスが期限超過を ops へ強制起票する
(3 択: cutover / 破棄 / 延長 — 判断は人間、§12 の拒否権)。

台帳はコード所有 (設定化しない): 部品の追加はデプロイと原子的であるべきで、
エントリ追加を忘れた部品はレビューで見えるようにする。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime

_DEFAULT_DEADLINE_WEEKS = 8


@dataclass(frozen=True)
class ShadowEntry:
    """シャドー運転中の部品 1 つ。"""

    name: str
    started: date
    # cutover 済み判定 (True = もうシャドーではない)。env flag はここで読む
    is_cutover: str  # env 変数名 ("1" で cutover 済みとみなす)。"" = 恒久シャドー扱い
    note: str
    deadline_weeks: int = _DEFAULT_DEADLINE_WEEKS


# 現役のシャドー部品 (棄却済みは載せない — few-shot は §10.2c で棄却済みのため対象外)
SHADOW_REGISTRY: tuple[ShadowEntry, ...] = (
    ShadowEntry(
        name="C7 自動 rollback (summarizer)",
        started=date(2026, 8, 22),
        is_cutover="TUNING_AUTO_ROLLBACK",
        note="唯一の無人安全網。シャドーのままでは一度も作動しない",
    ),
    ShadowEntry(
        name="蒸留ヘッド v0 (triage 地点のシャドー推論、§14)",
        started=date(2026, 8, 22),
        is_cutover="HEAD_CASCADE",
        note="cutover 基準は設計書 §14.4 (非劣 + 致命方向 0 + 足切り番兵の同時実装)",
    ),
)


def shadow_status_lines(now: datetime | None = None) -> list[str]:
    """週次ガバナンスの ops 本文に足す行。期限超過は 3 択の強制起票文になる。"""
    base = (now or datetime.now(UTC)).date()
    lines: list[str] = []
    for e in SHADOW_REGISTRY:
        if e.is_cutover and os.environ.get(e.is_cutover, "").strip() == "1":
            continue  # cutover 済み
        elapsed_weeks = max(0, (base - e.started).days // 7)
        if elapsed_weeks >= e.deadline_weeks:
            lines.append(
                f"⏰ シャドー期限超過: {e.name} ({elapsed_weeks} 週経過 / 期限 "
                f"{e.deadline_weeks} 週) — cutover / 破棄 / 延長 のいずれかを決めてください。"
                f" {e.note}"
            )
        else:
            lines.append(f"🕓 シャドー運転中: {e.name} ({elapsed_weeks}/{e.deadline_weeks} 週)")
    return lines
