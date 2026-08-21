"""C7 自動 rollback 関門 — 較正格子 P2 (docs/self_evolving_tuning_design.md §4 C7)。

cutover 後の週次指標 (verify_prompt_cutover の合否) が劣化を示したら、該当プロンプトの
config を前版へ復帰する。**本設計で唯一の無人適用** (fail-closed 方向のみ無人、§5-3)。

シャドー先行 (§10.1): 既定は記録のみ (mode=shadow、「戻すべきと判定した」を tuning_evals
に残す)。実適用は ``TUNING_AUTO_ROLLBACK=1`` を明示設定した後のみ — シャドー期間で
誤発動率を観察してから cutover する。env flag 即時 rollback は既確立の型 (subject-gate 等)。

判定は ``decide_rollback()`` 純関数に集約する (job_recovery の decide() と同じ原則)。
ガード:
- 劣化判定 (verify exit=1) のときだけ動く。INCONCLUSIVE (=2) では動かない (保守側)。
- 前版が無ければ戻せない。
- 直前の版が auto-rollback 自身なら動かない (rollback の rollback で flip-flop しない)。
- 同じ版への裁定は 1 回だけ (tuning_evals の既存行が状態)。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Literal

from src.logging_config import get_logger

_log = get_logger(__name__)

# 実適用の opt-in flag。未設定 = シャドー (記録のみ)。
ENV_FLAG = "TUNING_AUTO_ROLLBACK"
# auto-rollback が作る版の note 接頭辞 (decide の連鎖ガードが参照する SSoT)。
ROLLBACK_NOTE_PREFIX = "auto-rollback:"

_VERIFY_FAIL = 1


@dataclass(frozen=True)
class RollbackDecision:
    """decide_rollback の結論。action=none 以外は両版が必ず入る。

    tuning_evals への記録は from_version=restore_version (旧版) /
    to_version=degraded_version (劣化を検出した版) — flip-flop 防止の既存行検索は
    劣化版 (to_version) で引く。
    """

    action: Literal["none", "shadow", "apply"]
    reason: str
    degraded_version: int | None = None  # 劣化を検出した版 (rollback 元)
    restore_version: int | None = None  # 復元する前版 (rollback 先)


def is_auto_apply_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def decide_rollback(
    *,
    verify_exit: int,
    latest_version: int | None,
    latest_note: str,
    previous_version: int | None,
    already_decided: bool,
    auto_apply_enabled: bool,
) -> RollbackDecision:
    """rollback するか (純関数)。呼び出し側は結論を実行するだけにする。"""
    if verify_exit != _VERIFY_FAIL:
        return RollbackDecision(action="none", reason="劣化判定ではない")
    if latest_version is None or previous_version is None:
        return RollbackDecision(action="none", reason="前版が無く戻せない")
    if latest_note.startswith(ROLLBACK_NOTE_PREFIX):
        return RollbackDecision(
            action="none",
            reason="直前の版が auto-rollback — 連鎖しない (人間の判断待ち)",
        )
    if already_decided:
        return RollbackDecision(action="none", reason="この版への裁定は済んでいる")
    action: Literal["shadow", "apply"] = "apply" if auto_apply_enabled else "shadow"
    return RollbackDecision(
        action=action,
        reason="cutover 後の週次指標が劣化",
        degraded_version=latest_version,
        restore_version=previous_version,
    )


async def maybe_auto_rollback(
    *,
    verify_exit: int,
    prompt_id: str = "summarizer",
    config_key: str = "summarizer_rubric",
    repo: Any | None = None,
) -> str | None:
    """週次ガバナンスから呼ばれる適用層。ops 本文に足す 1 行を返す (何もしなければ None)。

    例外は握って None (rollback 層の失敗で監査投稿を止めない)。
    """
    try:
        from src.storage.config_store import get_config_version, list_history
        from src.storage.run_history import RunHistoryRepository

        history = list_history(config_key, limit=2)
        latest = history[0] if history else None
        previous = history[1] if len(history) > 1 else None
        _repo = repo if repo is not None else RunHistoryRepository()
        already = (
            latest is not None
            and _repo.find_tuning_eval(
                prompt_id=prompt_id, kind="auto_rollback", to_version=latest.version
            )
            is not None
        )
        decision = decide_rollback(
            verify_exit=verify_exit,
            latest_version=latest.version if latest else None,
            latest_note=latest.note if latest else "",
            previous_version=previous.version if previous else None,
            already_decided=already,
            auto_apply_enabled=is_auto_apply_enabled(),
        )
        if decision.action == "none":
            # 劣化なのに動けない場合だけ理由を可視化する (静かな不作動を残さない)
            if verify_exit == _VERIFY_FAIL:
                return f"⏸ auto-rollback 見送り: {decision.reason}"
            return None

        assert decision.degraded_version is not None and decision.restore_version is not None
        detail = json.dumps(
            {
                "config_key": config_key,
                "reason": decision.reason,
                "verify_exit": verify_exit,
            },
            ensure_ascii=False,
        )
        if decision.action == "shadow":
            _repo.record_tuning_eval(
                prompt_id=prompt_id,
                kind="auto_rollback",
                verdict="would_rollback",
                mode="shadow",
                detail=detail,
                from_version=decision.restore_version,
                to_version=decision.degraded_version,
            )
            _log.info(
                "auto_rollback_shadow",
                prompt_id=prompt_id,
                degraded_version=decision.degraded_version,
                restore_version=decision.restore_version,
            )
            return (
                f"🟠 auto-rollback (シャドー): v{decision.degraded_version} は"
                f" v{decision.restore_version} へ戻すべきと判定 (適用は {ENV_FLAG}=1 で有効化)"
            )

        # action == "apply": 前版の内容を新版として復元 (履歴は失わない — revert API と同型)
        value = get_config_version(config_key, decision.restore_version)
        if value is None:
            return f"⏸ auto-rollback 見送り: v{decision.restore_version} の内容を取得できない"
        from src.storage.config_store import save_config

        new_version = save_config(
            config_key,
            value,
            note=(
                f"{ROLLBACK_NOTE_PREFIX} v{decision.degraded_version} 劣化検出"
                f" → v{decision.restore_version} 復元"
            ),
        )
        _invalidate_prompt_cache(config_key)
        _repo.record_tuning_eval(
            prompt_id=prompt_id,
            kind="auto_rollback",
            verdict="rolled_back",
            mode="applied",
            detail=detail,
            from_version=decision.restore_version,
            to_version=decision.degraded_version,
        )
        _log.info(
            "auto_rollback_applied",
            prompt_id=prompt_id,
            degraded_version=decision.degraded_version,
            restore_version=decision.restore_version,
            new_version=new_version,
        )
        return (
            f"🔴 auto-rollback 適用: v{decision.degraded_version} 劣化 →"
            f" v{decision.restore_version} を v{new_version} として復元"
        )
    except Exception as e:  # noqa: BLE001 — rollback 層の失敗で監査投稿を止めない
        _log.error("auto_rollback_failed", error=f"{type(e).__name__}: {e}")
        return None


def _invalidate_prompt_cache(config_key: str) -> None:
    """復元後のプロセスキャッシュ無効化 (config_history._invalidate の該当分のみ)。"""
    if config_key == "summarizer_rubric":
        from src.prompts.rubric_store import invalidate_summarizer_cache

        invalidate_summarizer_cache()
