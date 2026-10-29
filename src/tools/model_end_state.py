"""2 枠 (S・N) の終着の状態の検査 (P0-26、ops 用)。

割当 (tiers・``step:*``・``fallback:*``) が ``RESIDENT_SLOTS`` ∪ 埋込 ∪ 明示の外部 ∪
常駐の例外 (終了日つき) に入っているかを見る。``fallback:*`` に限り緊急の受け皿
(gemma4:26b) も許す。

⚠ ``MODEL_RESIDENT_ENFORCE=1`` にする順番: 先に ``step:*`` の gemma4 と s23i /
kuebiko-sft:26b の上書きを外し (`step:assign_evidence_judge` / `step:org_identity_judge`
の例外も含む)、``check_live_assignments()`` が空になってから旗を 1 にする。
先に 1 にすると、既存の上書きを含む保存 (POST /api/v1/model-tiers) が全部拒否される。

本番の検査は本番の DB を相手にする (``scripts/check_model_end_state.py``、
``/api/v1/health-status`` の ``model_end_state``)。pytest は静的な写しに頼らない。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from src.tools import model_registry as reg

STEP_PREFIX = "step:"
FALLBACK_PREFIX = "fallback:"
REGISTRY_FLAG = "MODEL_REGISTRY_ENFORCE"
RESIDENT_FLAG = "MODEL_RESIDENT_ENFORCE"


def resident_issue(
    model: str, *, step: str | None, allow_emergency: bool, today: date
) -> tuple[str, str] | None:
    """割当が 2 枠の方針に反するか。(旗の名前, 理由)。外部・空は対象外。"""
    from src.tools.model_tiers import is_external_model

    name = model.strip()
    if not name or is_external_model(name):
        return None
    if not reg.is_registered(name):
        return (REGISTRY_FLAG, "未登録のモデルです (model_registry に 1 行足す)")
    if allow_emergency and name in reg.EMERGENCY_FALLBACK_MODELS:
        return None
    if name in reg.RESIDENT_SLOTS.values():
        return None
    until = reg.RESIDENT_EXCEPTIONS.get(name)
    if step is not None:
        until = until or reg.RESIDENT_STEP_EXCEPTIONS.get((step, name))
    if until is not None:
        if today <= until:
            return None
        return (RESIDENT_FLAG, f"常駐の例外が期限切れです ({until.isoformat()})")
    return (RESIDENT_FLAG, "常駐の枠 (S・N) の外のモデルです")


def find_violations(
    tiers: Mapping[str, str],
    step_overrides: Mapping[str, str],
    local_fallbacks: Mapping[str, str],
    *,
    today: date,
) -> list[str]:
    """枠の外の割当を ``"<ラベル>=<モデル> (<理由>)"`` の一覧で返す。"""
    found: list[str] = []
    checks: list[tuple[str, str, str | None, bool]] = []
    checks += [(f"tier:{k}", v, None, False) for k, v in tiers.items() if k != "embedding"]
    checks += [(f"step:{k}", v, k, False) for k, v in step_overrides.items()]
    checks += [(f"fallback:{k}", v, None, True) for k, v in local_fallbacks.items()]
    for label, model, step, emergency in checks:
        issue = resident_issue(model, step=step, allow_emergency=emergency, today=today)
        if issue is not None:
            found.append(f"{label}={model} ({issue[1]})")
    return found


def check_live_assignments(*, today: date | None = None) -> list[str]:
    """いまの本番の割当 (DB) を検査する。読めなければ例外のまま上げる。"""
    from src.tools.model_tiers import (
        load_local_fallbacks,
        load_model_tiers,
        load_step_overrides,
    )

    return find_violations(
        load_model_tiers(),
        load_step_overrides(),
        load_local_fallbacks(),
        today=today or date.today(),
    )
