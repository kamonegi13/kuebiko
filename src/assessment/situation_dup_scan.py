"""重複して開設された情勢の検査 (2026-09-22)。

規則 (``assignment.match_claim``) は誤って繋ぐ一方で、**本当の重複は取りこぼす**。
情勢どうしを埋込で総当たりし、高い余弦の組を **人の確認へ回す** (自動統合はしない —
確度 medium 以下は審判自身が迷っている組で、誤統合は追跡を消す)。

実測 (Opus 盲検 115 組・余弦の帯で層化抽出、2026-09-22):

| 帯 | 該当組 | 統合すべき |
|---|---|---|
| 0.75 以上 | 3 | 3/3 (100%) |
| 0.65-0.75 | 12 | 6/12 (50%) |
| 0.55-0.65 | 100 | 1/40 (2%) |
| 0.45-0.55 | 1,197 | 1/40 (2%) |
| 0.30-0.45 | 12,713 | 0/20 (0%) |

⭐ **0.65 を境に急落する**。0.55 まで下げると 98% が空振りで、人の確認が飽和する。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: 検査に拾う余弦の下限。この上では 9/15 が統合すべき、下では 2% に落ちる。
DUP_SCAN_THRESHOLD = 0.65
#: 常設情報要求は設計上 別物 (監視対象国ごとに分けている) — 互いに近くても統合しない。
STANDING_KIND = "standing"


@dataclass(frozen=True)
class DuplicatePair:
    """重複の疑いがある情勢の組 (確認待ち)。"""

    a_id: str
    a_title: str
    b_id: str
    b_title: str
    cosine: float


def find_duplicate_pairs(
    situations: Sequence[tuple[str, str, str, np.ndarray]],
    *,
    threshold: float = DUP_SCAN_THRESHOLD,
) -> list[DuplicatePair]:
    """題名の埋込が近い情勢の組を、余弦の高い順に返す (純粋関数)。

    Args:
        situations: (situation_id, 題名, kind, 題名の正規化済み埋込)
    """
    items = [s for s in situations if s[2] != STANDING_KIND]
    out: list[DuplicatePair] = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            cos = float(np.dot(items[i][3], items[j][3]))
            if cos >= threshold:
                out.append(
                    DuplicatePair(
                        a_id=items[i][0],
                        a_title=items[i][1],
                        b_id=items[j][0],
                        b_title=items[j][1],
                        cosine=cos,
                    )
                )
    return sorted(out, key=lambda p: -p.cosine)


def audit_line(pairs: Sequence[DuplicatePair]) -> str:
    """週次監査に載せる 1 行。

    ⭐ **0 件でも出す** — 検査が動かなくなったことに気付けなくなる (fill-rate 監査と
    同じ思想: 届くこと自体が生存証明)。
    """
    if not pairs:
        return f"重複して開設された情勢: 0 組 (余弦 {DUP_SCAN_THRESHOLD} 以上)"
    top = pairs[0]
    return (
        f"重複して開設された情勢: {len(pairs)} 組 (余弦 {DUP_SCAN_THRESHOLD} 以上) ⚠️"
        f" 最近似 {top.cosine:.2f}「{top.a_title[:28]}」←「{top.b_title[:28]}」"
        " — scripts/merge_duplicate_situations.py で確認"
    )
