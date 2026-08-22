"""プロンプト統治の週次監査 (2026-08-20)。

「日付待ちの検証」を口約束・手動実行に置かない — 忘れる方が実害、という利用者判断で
週次ジョブ化した。やることは 2 つ:

1. **禁止事項監査** (``scripts/audit_prompt_prohibitions``): 判定基準の禁止が実データで
   守られているか (OK / VIOLATED / INCONCLUSIVE)。
2. **最新 rubric 切替の合否** (``scripts/verify_prompt_cutover``): summarizer_rubric の
   最新版の切替前後で LLM 出力が劣化していないか。cutover 時刻は config_store の版履歴
   から自動で取り、窓は 1〜7 日に clamp する。窓の純度 (中でさらに版が動いた) の判定は
   script 側の関門が担うので、ここは呼ぶだけでよい。

⚠ 判定ロジックの SSoT は script 側 — 本 module は **subprocess で呼ぶ薄い scheduler
ラッパ** に徹する (判定・閾値・語彙をここへ複製しない)。exit code (0=OK/PASS,
1=VIOLATED/FAIL, 2=INCONCLUSIVE) と stdout の総合行だけを読む。
失敗しても例外は上げない (scheduler job を殺さない)。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

from src.logging_config import get_logger

_log = get_logger(__name__)

# 検証対象は registry の全プロンプト (層分けの一般化 2026-08-20 — 追加に自動追従)
_MAX_WINDOW_DAYS = 7
_SUBPROCESS_TIMEOUT_SEC = 300
# ops 投稿の本文上限 (Discord embed の余裕を残す)。超過は先頭優先で切る。
_BODY_LIMIT = 1800

_STATUS_ICON = {0: "✅", 1: "🔴", 2: "⏳"}


def clamp_window_days(now: datetime, cutover: datetime) -> int | None:
    """切替からの経過で検証窓 (日) を決める。1 日未満は None (= 今週は判定しない)。

    窓は前後同幅なので、経過日数より広い窓を指定しても post 側が埋まらないだけ —
    経過ぶんに合わせて 1〜7 日に clamp する (7 日で頭打ちなのは verify 側の較正が
    7 日窓で行われているため)。
    """
    elapsed_days = int((now - cutover).total_seconds() // 86400)
    if elapsed_days < 1:
        return None
    return min(elapsed_days, _MAX_WINDOW_DAYS)


def _summary_line(stdout: str, *, prefix: str) -> str:
    """stdout から総合行 (`総合: X` / `総合判定: X`) を拾う。無ければ末尾行で代用。"""
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith(prefix):
            return ln
    return lines[-1] if lines else "(出力なし)"


def _violated_lines(stdout: str) -> list[str]:
    """監査表から VIOLATED 行だけを抜く (ops 本文は要点のみ)。"""
    return [ln.strip() for ln in stdout.splitlines() if "VIOLATED" in ln and "総合" not in ln]


async def _run_script(args: list[str]) -> tuple[int, str]:
    """repo 内 script を subprocess で実行し (exit_code, stdout) を返す。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_SUBPROCESS_TIMEOUT_SEC)
    except TimeoutError:
        proc.kill()
        return (1, "(timeout)")
    return (proc.returncode or 0, out.decode("utf-8", errors="replace"))


def _latest_cutover(config_key: str) -> tuple[int, datetime] | None:
    """config_key の最新版 (version, created_at)。履歴が無い/読めないなら None。"""
    from src.storage.config_store import list_history

    history = list_history(config_key, limit=1)
    if not history:
        return None
    latest = history[0]
    try:
        parsed = datetime.fromisoformat(latest.created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (latest.version, parsed)


async def run_weekly_prompt_governance() -> None:
    """週次: 禁止事項監査 + 最新 rubric 切替の合否を ops へ 1 通で投稿する。"""
    try:
        sections: list[str] = []
        worst_exit = 0

        # 1) 禁止事項監査
        audit_exit, audit_out = await _run_script(["scripts.audit_prompt_prohibitions"])
        worst_exit = max(worst_exit, audit_exit if audit_exit in (0, 1, 2) else 1)
        sections.append(
            f"{_STATUS_ICON.get(audit_exit, '🔴')} 禁止事項監査: "
            f"{_summary_line(audit_out, prefix='総合:')}"
        )
        sections.extend(f"　{ln}" for ln in _violated_lines(audit_out)[:6])

        # 2) 管理対象プロンプトの最新切替 (registry に自動追従)
        #
        # ⚠ 統計検証 (verify_prompt_cutover) は **summarizer のみ**。指標が articles
        # (summarizer 出力) の充足率なので、他プロンプトの切替に流すと帰属が壊れる。
        # block 系 (status_synthesis 等、1 run/日) は標本が構造的に不足するため、
        # golden (seed=legacy byte 一致) + 保存時 render 検証 + 版履歴で担保し、
        # ここでは最新版の存在だけを正直に報告する。
        from src.prompts.registry import all_specs

        for spec in all_specs():
            latest = _latest_cutover(spec.config_key)
            if latest is None:
                sections.append(f"⏳ {spec.prompt_id}: 版履歴なし (seed 未投入?)")
                worst_exit = max(worst_exit, 2)
                continue
            version, cutover = latest
            if spec.prompt_id != "summarizer":
                sections.append(
                    f"ℹ️ {spec.prompt_id}: v{version} (統計検証は対象外 — 版履歴/golden で担保)"
                )
                continue
            days = clamp_window_days(datetime.now(UTC), cutover)
            if days is None:
                sections.append(
                    f"⏳ {spec.prompt_id} 切替検証: v{version} は切替後 1 日未満 — 次週判定"
                )
                continue
            cutover_iso = cutover.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            verify_exit, verify_out = await _run_script(
                [
                    "scripts.verify_prompt_cutover",
                    "--cutover",
                    cutover_iso,
                    "--days",
                    str(days),
                ]
            )
            worst_exit = max(worst_exit, verify_exit if verify_exit in (0, 1, 2) else 1)
            sections.append(
                f"{_STATUS_ICON.get(verify_exit, '🔴')} {spec.prompt_id} 切替検証 "
                f"(v{version}, {days} 日窓): "
                f"{_summary_line(verify_out, prefix='総合判定:')}"
            )
            # C7 自動 rollback 関門 (較正格子 P2): FAIL のとき前版へ戻す判定。
            # 既定はシャドー (記録のみ)、実適用は TUNING_AUTO_ROLLBACK=1。
            # 判定・適用の実体は src/tuning/auto_rollback (ここは 1 行足すだけ)。
            from src.tuning.auto_rollback import maybe_auto_rollback

            rollback_line = await maybe_auto_rollback(verify_exit=verify_exit)
            if rollback_line:
                sections.append(rollback_line)

        # 較正格子のシャドー部品台帳 (§13-8): 無期限シャドー化を防ぐ期限起票
        try:
            from src.tuning.shadow_registry import shadow_status_lines

            sections.extend(shadow_status_lines())
        except Exception as e:  # noqa: BLE001 — 台帳の失敗で監査投稿を止めない
            _log.warning("shadow_registry_failed", error=str(e))

        # 蒸留ヘッド v0 のシャドー統計 (§14.3 の常設消費者 — write-only 化の防止)
        try:
            from src.storage.run_history import RunHistoryRepository

            hs = RunHistoryRepository().head_shadow_stats(days=7)
            if hs.total > 0:
                agree_pct = 100.0 * hs.agree_with_triage / hs.total
                sections.append(
                    f"🧮 ヘッド v0 シャドー (7 日): {hs.total} 件 / triage 一致 "
                    f"{agree_pct:.0f}% / 足切り不一致 {hs.disagree_cutoff} 件 / "
                    f"triage エラー {hs.triage_error} 件"
                )
        except Exception as e:  # noqa: BLE001 — シャドー統計の失敗で監査投稿を止めない
            _log.warning("head_shadow_stats_failed", error=str(e))

        title = "プロンプト統治 週次監査"
        body = "\n".join(sections)[:_BODY_LIMIT]
        importance = "medium" if worst_exit == 1 else "low"
        from src.ui.services.ops_notify import post_ops_message

        sent = await post_ops_message(title=title, body=body, importance=importance)
        _log.info("prompt_governance_audit", sent=sent, worst_exit=worst_exit)
    except Exception as e:  # noqa: BLE001 — 監査自体の失敗で scheduler を汚さない
        _log.error("prompt_governance_audit_failed", error=f"{type(e).__name__}: {e}")
