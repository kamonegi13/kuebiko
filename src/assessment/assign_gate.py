"""台帳の割当に埋込の確認を課す関門 (2026-09-24)。

規則 (``assignment.match_situation``) は **候補を出す** 役に留め、記事の要約と情勢の題名が意味的にも
近いときだけ割り当てる。規則は「一番当てはまる情勢」を必ず選び、「どれにも入れない」選択肢を
持たないため、一般語 (脆弱性・悪用・侵害) と頻出国 (米国) の重なりだけで無関係な情勢へ入っていた。

実測 (Opus 盲検 362 組・claim 種別で分けても同傾向):
  規則の「同じ情勢」率  anchor 31% / nation 10% / token 4% (証拠全体で推定 ~17%)
  類似度 0.60           「無関係」を 132 件中 2 件まで落とし、「同じ」の 88% を残す (AUC 0.921)
「同じ」と「関連するが別の事案」の見分けは類似度では付かない (AUC 0.82) — 次段の ML の仕事。

段階導入: ``ASSIGN_EMBED_GATE`` = off / **shadow (既定: 落ちるはずの割当を記録するだけ)** / on。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

#: 記事要約 × 情勢題名の類似度の下限 (上の実測)
ASSIGN_GATE_THRESHOLD = 0.60
_MODE_ENV = "ASSIGN_EMBED_GATE"
_MODES = ("off", "shadow", "on")

GateMode = Literal["off", "shadow", "on"]


@dataclass(frozen=True)
class GateResult:
    """1 件の割当に対する確認結果。cos が None = 埋込が無く確認できなかった。"""

    cos: float | None
    passed: bool


def gate_mode() -> GateMode:
    """関門の動作。未知の値は shadow (本番の割当を変えない側) に倒す。"""
    raw = os.environ.get(_MODE_ENV, "shadow").strip()
    return raw if raw in _MODES else "shadow"  # type: ignore[return-value]


def evaluate_gate(
    *, article_vec: Any, situation_vec: Any, threshold: float = ASSIGN_GATE_THRESHOLD
) -> GateResult:
    """正規化済み埋込どうしの確認。

    **埋込が無ければ割当を残す** (確認できないことで挙動を変えない)。
    """
    if article_vec is None or situation_vec is None:
        return GateResult(cos=None, passed=True)
    cos = float(article_vec @ situation_vec)
    return GateResult(cos=cos, passed=cos >= threshold)
