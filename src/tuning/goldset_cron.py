"""goldset 3 者比較の cron 化 — 較正格子 P2 (docs/self_evolving_tuning_design.md §4 C6 後半)。

summarizer_rubric の版が変わった週だけ、凍結 gold set で 旧版 1 回 + 新版 2 回 (2 本目が
揺らぎの床 = 対照) を自動実行し、フィールド別の合否を tuning_evals へ記録して ops へ
報告する。**測定のみ** (シャドー互換) — rollback の判断は週次ガバナンス (本番統計の
verify_prompt_cutover) が担い、本評価はその独立な証拠 (入力凍結 = コーパス交絡なし)。

判定ロジックの SSoT は ``scripts/eval_goldset.py`` — 本 module は subprocess で呼ぶ
薄い scheduler ラッパに徹する (prompt_governance と同じ原則。床・二項 sd・変化判定を
ここへ複製しない)。冪等性は tuning_evals の (prompt_id, kind, to_version) 既存行が担う。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.logging_config import get_logger

_log = get_logger(__name__)

_PROMPT_ID = "summarizer"
_CONFIG_KEY = "summarizer_rubric"
_KIND = "goldset_cutover"
# 最新版がこれより古ければ評価しない (cron 導入以前の古い切替を今さら測らない)
_RECENT_DAYS = 14
# 1 回の goldset run (~86 件 × LLM) の上限。3 回で job 予算 90 分に収まる幅
_RUN_TIMEOUT_SEC = 1800
_GOLDSET_PATH = Path("data/eval/goldset.jsonl")

RunScript = Callable[[list[str], int], Awaitable[tuple[int, str]]]


@dataclass(frozen=True)
class GoldsetCronOutcome:
    """1 回の cron 実行の結末 (テスト・ログ用)。"""

    status: str  # ran / skipped_no_change / skipped_already / skipped_no_goldset /
    # dry_run / error
    detail: str = ""
    verdict: str = ""


async def _run_script(args: list[str], timeout_sec: int) -> tuple[int, str]:
    """repo 内 script を subprocess で実行し (exit_code, stdout) を返す。"""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        proc.kill()
        return (1, "(timeout)")
    return (proc.returncode or 0, out.decode("utf-8", errors="replace"))


def _parse_created_at(raw: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def run_weekly_goldset_eval(
    *,
    dry_run: bool = False,
    repo: Any | None = None,
    run_script: RunScript | None = None,
    goldset_path: Path = _GOLDSET_PATH,
    now: datetime | None = None,
) -> GoldsetCronOutcome:
    """週次: rubric の版が変わっていれば goldset 3 者比較を実行する。

    例外は握って error (scheduler job を殺さない)。
    """
    try:
        return await _run_impl(
            dry_run=dry_run,
            repo=repo,
            run_script=run_script or _run_script,
            goldset_path=goldset_path,
            now=now or datetime.now(UTC),
        )
    except Exception as e:  # noqa: BLE001 — 評価の失敗で scheduler を汚さない
        _log.error("goldset_cron_failed", error=f"{type(e).__name__}: {e}")
        return GoldsetCronOutcome(status="error", detail=f"{type(e).__name__}: {e}")


async def _run_impl(
    *,
    dry_run: bool,
    repo: Any | None,
    run_script: RunScript,
    goldset_path: Path,
    now: datetime,
) -> GoldsetCronOutcome:
    from src.storage.config_store import list_history
    from src.storage.run_history import RunHistoryRepository

    history = list_history(_CONFIG_KEY, limit=2)
    if len(history) < 2:
        return GoldsetCronOutcome(status="skipped_no_change", detail="版が 1 つ以下")
    latest, prev = history[0], history[1]

    created = _parse_created_at(latest.created_at)
    if created is None or (now - created) > timedelta(days=_RECENT_DAYS):
        return GoldsetCronOutcome(
            status="skipped_no_change",
            detail=f"最新版 v{latest.version} は {_RECENT_DAYS} 日より古い",
        )

    _repo = repo if repo is not None else RunHistoryRepository()
    if _repo.find_tuning_eval(prompt_id=_PROMPT_ID, kind=_KIND, to_version=latest.version):
        return GoldsetCronOutcome(status="skipped_already", detail=f"v{latest.version} は評価済み")

    if not goldset_path.exists():
        await _notify(
            title="goldset 切替評価: gold set 未整備",
            body=(
                f"{_PROMPT_ID} v{prev.version}→v{latest.version} の切替を検出しましたが"
                f" {goldset_path} がありません。eval_goldset build で凍結してください。"
            ),
            importance="medium",
        )
        return GoldsetCronOutcome(status="skipped_no_goldset", detail=str(goldset_path))

    label_prev = f"cron-v{prev.version}"
    label_cur_a = f"cron-v{latest.version}-a"
    label_cur_b = f"cron-v{latest.version}-b"
    if dry_run:
        return GoldsetCronOutcome(
            status="dry_run",
            detail=(
                f"v{prev.version}→v{latest.version} を評価予定"
                f" ({label_prev}/{label_cur_a}/{label_cur_b})"
            ),
        )

    # 3 者を全て明示版で pin する (実行中に rubric が動いても評価は汚れない)
    runs = [
        (label_prev, prev.version),
        (label_cur_a, latest.version),
        (label_cur_b, latest.version),
    ]
    for label, version in runs:
        exit_code, out = await run_script(
            [
                "scripts.eval_goldset",
                "run",
                "--label",
                label,
                "--rubric-version",
                str(version),
            ],
            _RUN_TIMEOUT_SEC,
        )
        if exit_code != 0:
            _log.warning("goldset_cron_run_failed", label=label, out=out[-200:])
            await _notify(
                title="goldset 切替評価: 実行失敗",
                body=f"{label} (rubric v{version}) の実行が失敗しました。来週再試行します。",
                importance="medium",
            )
            return GoldsetCronOutcome(status="error", detail=f"run {label} failed")

    exit_code, out = await run_script(
        [
            "scripts.eval_goldset",
            "compare",
            label_prev,
            label_cur_a,
            "--floor",
            label_cur_a,
            label_cur_b,
            "--json",
        ],
        300,
    )
    if exit_code != 0:
        return GoldsetCronOutcome(status="error", detail=f"compare failed: {out[-200:]}")
    payload = json.loads(out)

    fields = payload.get("fields", [])
    degraded = [f for f in fields if f.get("is_change") and f.get("delta_pt", 0) < 0]
    improved = [f for f in fields if f.get("is_change") and f.get("delta_pt", 0) > 0]
    verdict = "degraded" if degraded else "pass"

    _repo.record_tuning_eval(
        prompt_id=_PROMPT_ID,
        kind=_KIND,
        verdict=verdict,
        mode="measure",
        from_version=prev.version,
        to_version=latest.version,
        detail=json.dumps(
            {
                "shared": payload.get("shared"),
                "floor_shared": payload.get("floor_shared"),
                "degraded": [
                    {"field": f["field"], "delta_pt": round(f["delta_pt"], 1)} for f in degraded
                ],
                "improved": [
                    {"field": f["field"], "delta_pt": round(f["delta_pt"], 1)} for f in improved
                ],
            },
            ensure_ascii=False,
        ),
    )

    outcome_label = f"🔴 劣化 {len(degraded)} 項目" if degraded else "✅ PASS"
    lines = [
        f"{_PROMPT_ID} v{prev.version}→v{latest.version} (同一入力 {payload.get('shared')} 件、"
        f"床 = 新版 2 実行): {outcome_label}",
    ]
    lines.extend(
        f"　🔻 {f['field']}: {f['delta_pt']:+.1f}pt (床 ±{f.get('noise_pt', 0):.1f}pt)"
        for f in degraded[:6]
    )
    lines.extend(f"　🔺 {f['field']}: {f['delta_pt']:+.1f}pt" for f in improved[:4])
    lines.append("測定のみ — rollback 判断は週次ガバナンス (本番統計) が担う")
    await _notify(
        title=f"goldset 切替評価: {verdict.upper()}",
        body="\n".join(lines),
        importance="medium" if degraded else "low",
    )
    _log.info(
        "goldset_cron_complete",
        from_version=prev.version,
        to_version=latest.version,
        verdict=verdict,
        degraded=len(degraded),
        improved=len(improved),
    )
    return GoldsetCronOutcome(status="ran", verdict=verdict)


async def _notify(*, title: str, body: str, importance: str) -> None:
    try:
        from src.ui.services.ops_notify import post_ops_message

        await post_ops_message(title=title, body=body, importance=importance)
    except Exception as e:  # noqa: BLE001 — 通知失敗で評価結果を捨てない
        _log.warning("goldset_cron_notify_failed", error=str(e))
