"""ACH 判定整合の監査/是正プランナ (2026-07-25 ACH 整合監査)。

leading_hypothesis (スカラ) と hypotheses[].verdict (マトリクス) は同じ判定の二重表現で、
旧 apply_adversarial がスカラだけを flip していたため自己矛盾 revision が生じた
(388 中 21 件、canonical 9/111 件)。本モジュールは保存済み revision の JSON を検査し、
是正内容を決定論的に計画する (pure — 書込は scripts/backfill_ach_coherence.py)。

是正の規律:
- verdict は常に (採点カウント, leading) からの導出値 (estimate.derive_verdict が SSoT)。
- スカラがカウント上「反証」(反整合 > 整合) で、マトリクス側に健在な主説 verdict が
  残っている場合は、旧 flip を取消してマトリクス主説へ戻す (revert_flip)。
  それ以外はスカラを尊重して verdict を導出し直すのみ (reconcile)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from src.synthesis.grounded.estimate import derive_verdict

CoherenceAction = Literal["none", "reconcile", "revert_flip"]


@dataclass(frozen=True)
class CoherencePlan:
    """1 revision の是正計画。action=none なら健全 (書込不要)。"""

    action: CoherenceAction
    new_leading: str
    hypotheses: tuple[dict[str, Any], ...]
    note: str


def _counts(h: dict[str, Any]) -> tuple[int, int]:
    def _i(v: Any) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    return _i(h.get("consistent")), _i(h.get("inconsistent"))


def _derived(h: dict[str, Any], leading: str) -> str:
    c, i = _counts(h)
    return derive_verdict(
        hypothesis=str(h.get("hypothesis") or ""), consistent=c, inconsistent=i, leading=leading
    )


def plan_coherence_fix(leading: str, hypotheses: list[dict[str, Any]]) -> CoherencePlan:
    """保存済み revision の (leading, マトリクス) を検査し是正計画を返す (決定論)。"""
    rows = [h for h in hypotheses if isinstance(h, dict) and h.get("hypothesis")]
    scalar_row = next((h for h in rows if h.get("hypothesis") == leading), None)

    if (
        rows
        and scalar_row is not None
        and all(h.get("verdict") == _derived(h, leading) for h in rows)
    ):
        return CoherencePlan("none", leading, tuple(rows), "")

    new_leading = leading
    action: CoherenceAction = "reconcile"
    note = "整合是正: verdict を採点カウントと見立てから導出し直し"

    # 旧 adversarial flip の取消判定: スカラ見立てが自マトリクスで反証済み (反整合 > 整合) かつ
    # マトリクス側の主説 verdict (旧見立て) がカウント上健在なら、そちらへ戻す。
    if scalar_row is not None:
        c, i = _counts(scalar_row)
        if i > c:
            for cand in rows:
                if cand.get("verdict") != "leading" or cand.get("hypothesis") == leading:
                    continue
                cc, ci = _counts(cand)
                if ci <= cc:
                    new_leading = str(cand["hypothesis"])
                    action = "revert_flip"
                    note = f"整合是正: 反証済み仮説への flip を取消 ({leading}→{new_leading})"
                    break

    fixed = [{**h, "verdict": _derived(h, new_leading)} for h in rows]
    if new_leading and all(h.get("hypothesis") != new_leading for h in fixed):
        # 見立てがマトリクスに無い (parse-fallback 遺物): 0/0 行を追記して可視化 (捏造しない)
        fixed.append(
            {"hypothesis": new_leading, "consistent": 0, "inconsistent": 0, "verdict": "leading"}
        )
    return CoherencePlan(action, new_leading, tuple(fixed), note)
