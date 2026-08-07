"""パイプライン起動サービス (Phase 1.5)。

UI からの即時実行・スケジューラからの cron 起動・テストからの呼び出し、
すべてが同じコードパスを通るようにする (CLAUDE.md §11)。

責務:
- ``run`` (in-process): スケジューラ用。``run_default`` を直接呼ぶ
- ``start_subprocess_run`` (Phase 1.5b): 即時実行用。run_id を即返し、
  subprocess の stdout を ``run_logs`` に逐次永続化 + RunRegistry に broadcast
- 同時 1 実行制約 (Ollama 直列利用) を ``asyncio.Lock`` で保証
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from src.logging_config import get_logger
from src.main import PipelineRunResult, run_default
from src.storage.run_history import RunHistoryRepository, RunRecord, RunStatus
from src.ui.services.run_registry import LogEvent, RunRegistry
from src.ui.ws.realtime import broadcast as ws_broadcast

# subprocess の structlog event → app-wide WebSocket event 名のマッピング。
# subprocess 内から直接 broadcast は呼べないため、stdout を読み取る parent (本 module) で
# JSON line を parse してこの map に該当すれば WebSocket 経由で全 client に push する。
_STDOUT_EVENT_BROADCAST_MAP: dict[str, str] = {
    "discord_post_sent": "article_posted",
    "synthesis_persisted": "synthesis_ready",
}

# subprocess 全体の wallclock timeout (秒)。LLM HTTP timeout (idle timeout) と異なり
# 「Ollama サーバが chunk を送り続けて永久 hang」のような状況も確実に救う絶対 timeout。
# synthesis pipeline (Gemma 4 31B / 21k char prompt) で 5-8 分かかるため、その 4 倍弱を
# 既定値とする。env ``PIPELINE_TIMEOUT_SECONDS`` で override 可能。
_DEFAULT_PIPELINE_TIMEOUT_SECONDS = 1800.0
# per-pipeline timeout override (2026-07-06)。重い weekly 31B synthesis は既定 1800s
# (30 分) を超えて timeout kill されることがある (実測: 07-06 に weekly-status-synthesis が
# 1800s timeout。先週は成功 = LLM 遅延の間欠性)。ただし延ばしすぎると単一ロックで後続の
# 夜間ジョブ (pir-spotlight 03:30 等) を新たにブロックするため、pir-spotlight を侵さない
# 範囲 (02:45+40 分=03:25) に限定して headroom を与える。env
# ``PIPELINE_TIMEOUT_SECONDS_<PIPELINE_UPPER_SNAKE>`` で個別 override 可能。
_PIPELINE_TIMEOUT_OVERRIDES: dict[str, float] = {
    "weekly-status-synthesis": 2400.0,
    # monthly は 720h (月) を総括する最重の synthesis。夜間解析帯で collection が
    # 停止し資源を専有できる前提で、より大きい headroom を与える (day=1 のみ・低頻度)。
    "monthly-status-synthesis": 3600.0,
    # weekly-recap は deep-dive を chunk-all で全件採点する (2026-07-07)。候補 ~900 →
    # ~8 チャンク × 数分 + digest 生成で ~20-25 分。夜間・週1なので 45 分の headroom を与える。
    "weekly-recap": 2700.0,
}
# shutdown 時、in-flight な subprocess の自然完了を待つ秒数。これを超えたら cancel に切り替え。
# 朝 07:30 JST の synthesis 直後にリビルドしても、ほぼ完了寸前なら救える程度を狙う。
_DEFAULT_SHUTDOWN_GRACE_SECONDS = 30.0


def _resolve_timeout_seconds(env_key: str, default: float) -> float:
    raw = os.environ.get(env_key, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _resolve_pipeline_timeout(pipeline_name: str) -> float:
    """pipeline 別 timeout を解決する: env 個別 → env global → コード override → 既定。"""
    per_env = f"PIPELINE_TIMEOUT_SECONDS_{pipeline_name.upper().replace('-', '_')}"
    if os.environ.get(per_env, "").strip():
        return _resolve_timeout_seconds(per_env, _DEFAULT_PIPELINE_TIMEOUT_SECONDS)
    default = _PIPELINE_TIMEOUT_OVERRIDES.get(pipeline_name, _DEFAULT_PIPELINE_TIMEOUT_SECONDS)
    return _resolve_timeout_seconds("PIPELINE_TIMEOUT_SECONDS", default)


_log = get_logger(__name__)

TriggerSource = Literal["scheduler", "manual", "cli"]

# subprocess 経路で子プロセスが書き出す PipelineRunResult JSON のディレクトリ。
# src/main.py の RUN_RESULTS_DIR と一致させること。
_RUN_RESULTS_DIR = Path("data/run_results")


class UnknownPipelineError(ValueError):
    """未知/撤去済の pipeline 名で run を要求されたときに送出 (幽霊 run の防止)。"""


def _reject_unknown_pipeline(pipeline_name: str) -> None:
    """pipeline_name が現行 pipelines.yaml に存在しなければ即座に拒否する。

    config ロード自体に失敗したときは検証を skip (可用性優先 — 検証の失敗で
    正当な run まで止めない)。
    """
    try:
        from src.config_loader import load_pipelines

        valid = {p.name for p in load_pipelines()}
    except Exception as e:  # noqa: BLE001 — 検証不能時は通す (fail-open)
        _log.warning("pipeline_validation_skipped", error=str(e))
        return
    if valid and pipeline_name not in valid:
        raise UnknownPipelineError(f"未知の処理です: {pipeline_name}")


class PipelineBusyError(RuntimeError):
    """既に別の run が実行中のときに送出。"""


class PipelineRunner:
    """run_default を呼びつつ run_history に記録するサービス。"""

    def __init__(
        self,
        repo: RunHistoryRepository,
        *,
        registry: RunRegistry | None = None,
    ) -> None:
        self._repo = repo
        self._registry = registry or RunRegistry()
        # 同時 1 実行に絞る (Ollama を直列で使うため)
        self._lock = asyncio.Lock()
        # 進行中の subprocess タスク (キャンセル / shutdown 用)
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def registry(self) -> RunRegistry:
        return self._registry

    @property
    def is_busy(self) -> bool:
        return self._lock.locked()

    # ----- in-process run (scheduler 用) -----

    async def run(
        self,
        *,
        dry_run: bool = False,
        max_articles: int | None = None,
        pipeline_name: str = "daily-briefing",
        triggered_by: TriggerSource = "manual",
    ) -> PipelineRunResult:
        """パイプライン 1 回分を in-process で実行する (scheduler 用)。"""
        if self._lock.locked():
            _log.warning("pipeline_run_skipped_busy", triggered_by=triggered_by)
            raise PipelineBusyError("既に別の処理が実行中です")

        async with self._lock:
            started = datetime.now(UTC)
            run_id = self._repo.start_run(
                RunRecord(
                    started_at=started,
                    pipeline=pipeline_name,
                    dry_run=dry_run,
                    triggered_by=triggered_by,
                ),
            )
            _log.info(
                "pipeline_runner_started",
                run_id=run_id,
                triggered_by=triggered_by,
                dry_run=dry_run,
            )
            try:
                result = await run_default(
                    dry_run=dry_run,
                    max_articles=max_articles,
                    pipeline_name=pipeline_name,
                )
            except Exception as e:  # noqa: BLE001
                self._repo.finish_run(
                    run_id,
                    status="failed",
                    finished_at=datetime.now(UTC),
                    total_fetched=0,
                    summarized=0,
                    posted=0,
                    marked_read=0,
                    error_count=1,
                    note=f"{type(e).__name__}: {e}",
                )
                _log.error("pipeline_runner_failed", run_id=run_id, error=str(e))
                raise

            status: Literal["succeeded", "partial_failure"] = (
                "succeeded" if not result.errors else "partial_failure"
            )
            self._repo.finish_run(
                run_id,
                status=status,
                finished_at=datetime.now(UTC),
                total_fetched=result.total_fetched,
                summarized=result.summarized,
                posted=result.posted,
                marked_read=result.marked_read,
                error_count=len(result.errors),
                note="\n".join(result.errors) if result.errors else None,
                triage_error_count=result.triage_error_count,
                partial_fetch=result.partial_fetch,
                partial_fetch_count=result.partial_fetch_count,
            )
            _log.info(
                "pipeline_runner_finished",
                run_id=run_id,
                status=status,
                summarized=result.summarized,
                posted=result.posted,
            )
            # 監査 backlog 2026-07-05: in-process 経路でも partial_failure を親側通知
            # (子=run_default が自前通知済みなら ops_notified=True で二重投稿しない)
            if status == "partial_failure" and not result.ops_notified:
                await self._notify_partial(run_id, pipeline_name, result.errors)
            return result

    # ----- subprocess run (即時実行 + SSE 配信) -----

    async def start_subprocess_run(
        self,
        *,
        dry_run: bool = True,
        max_articles: int | None = None,
        pipeline_name: str = "daily-briefing",
        triggered_by: TriggerSource = "manual",
        skip_dedup: bool = False,
        argv_override: list[str] | None = None,
    ) -> int:
        """``python -m src.main`` をサブプロセスで起動し、run_id を即返す。

        実体は ``asyncio.create_task`` で動かすバックグラウンドタスク。
        HTTP リクエストの寿命と subprocess 寿命を分離する。
        """
        if self._lock.locked():
            raise PipelineBusyError("既に別の処理が実行中です")

        # 防御 (2026-07-05): 既定 pipeline_name="daily-briefing" は pipelines.yaml から
        # 撤去済の幽霊。無効名で run を作ると main.py が既定 daily-briefing で失敗し、
        # 親側失敗通知 (P2) が ops に「daily-briefing run 失敗」を投稿する罠になる。
        # 無効名は run 生成前に fail-fast (ops スパムも幽霊 run 記録も生じさせない)。
        # argv_override 指定時は実 pipeline を名前起動しない (テスト/デバッグ) ので検証 skip。
        if argv_override is None:
            _reject_unknown_pipeline(pipeline_name)

        # Lock は task の中で取得 (create_task した直後にコルーチンに入る前提)
        started = datetime.now(UTC)
        run_id = self._repo.start_run(
            RunRecord(
                started_at=started,
                pipeline=pipeline_name,
                dry_run=dry_run,
                triggered_by=triggered_by,
            ),
        )
        _log.info(
            "pipeline_subprocess_scheduled",
            run_id=run_id,
            dry_run=dry_run,
            triggered_by=triggered_by,
        )

        task = asyncio.create_task(
            self._subprocess_lifecycle(
                run_id=run_id,
                dry_run=dry_run,
                max_articles=max_articles,
                pipeline_name=pipeline_name,
                skip_dedup=skip_dedup,
                argv_override=argv_override,
            ),
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def _subprocess_lifecycle(
        self,
        *,
        run_id: int,
        dry_run: bool,
        max_articles: int | None,
        pipeline_name: str,
        skip_dedup: bool,
        argv_override: list[str] | None,
    ) -> None:
        """subprocess を起動し stdout を repo + registry に流す。"""
        # Lock は task 開始時に取得し、タスク完了まで保持する
        await self._lock.acquire()
        try:
            args: list[str]
            if argv_override is not None:
                args = list(argv_override)
            else:
                args = [sys.executable, "-m", "src.main"]
                if dry_run:
                    args.append("--dry-run")
                if max_articles is not None:
                    args.extend(["--max-articles", str(max_articles)])
                # 既定の daily-briefing 以外を指定された場合のみ --pipeline を渡す
                if pipeline_name and pipeline_name != "daily-briefing":
                    args.extend(["--pipeline", pipeline_name])
                if skip_dedup:
                    args.append("--no-dedup")
                # 子側で articles テーブル書き込み + 結果 JSON 出力に使う
                args.extend(["--run-id", str(run_id)])

            await self._registry.broadcast(
                run_id,
                LogEvent(type="status", status="running"),
            )
            # App-wide push: dashboard / runs / history 等を即座にリフレッシュ。
            await ws_broadcast(
                "pipeline_running",
                {"run_id": run_id, "pipeline": pipeline_name},
            )

            timeout_seconds = _resolve_pipeline_timeout(pipeline_name)

            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    # 2026-08-01: 子に wallclock 予算を伝える。子は soft deadline で
                    # 記事処理を途中打ち切りし、処理済み分を確定してから終了する
                    # (timeout kill = 成果全損 → 次 run 再処理、の全損ループ防止)。
                    env={
                        **os.environ,
                        "PIPELINE_TIME_BUDGET_SECONDS": f"{timeout_seconds:.0f}",
                    },
                )
            except OSError as e:
                await self._record_failure_and_notify(
                    run_id, pipeline_name, f"subprocess_spawn_failed: {e}"
                )
                await self._registry.complete(run_id, "failed")
                await ws_broadcast(
                    "pipeline_complete",
                    {"run_id": run_id, "pipeline": pipeline_name, "status": "failed"},
                )
                return

            async def _consume_stdout() -> int:
                assert proc.stdout is not None
                while True:
                    raw = await proc.stdout.readline()
                    if not raw:
                        break
                    line = raw.decode(errors="replace").rstrip("\n")
                    seq = self._repo.append_log_line(run_id, line)
                    if seq is not None:
                        await self._registry.broadcast(
                            run_id,
                            LogEvent(type="line", seq=seq, line=line),
                        )
                    # subprocess structlog event を parse して app-wide push に流す。
                    # JSON 以外の行 (raw text / 部分行) は無視。
                    await _maybe_app_broadcast_from_line(line, run_id)
                return await proc.wait()

            try:
                # 絶対 timeout で全体を wrap。LLM hang (Ollama が chunk を送り続けて
                # client の idle timeout が発火しないケース) もここで確実に救う。
                returncode = await asyncio.wait_for(
                    _consume_stdout(),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                # subprocess 全体 timeout: SIGTERM → 5s grace → SIGKILL
                _log.warning(
                    "pipeline_subprocess_timeout",
                    run_id=run_id,
                    pipeline=pipeline_name,
                    timeout_seconds=timeout_seconds,
                )
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                await self._record_failure_and_notify(
                    run_id,
                    pipeline_name,
                    f"timeout exceeded ({timeout_seconds:.0f}s)",
                )
                await self._registry.complete(run_id, "failed")
                await ws_broadcast(
                    "pipeline_complete",
                    {"run_id": run_id, "pipeline": pipeline_name, "status": "failed"},
                )
                return
            except asyncio.CancelledError:
                # shutdown 等でキャンセルされたら subprocess を終了させる
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                # shutdown は運用者起点のため ops 通知しない (cancel 済み task 内の await も不可)
                self._record_failure(run_id, "cancelled by shutdown")
                await self._registry.complete(run_id, "failed")
                await ws_broadcast(
                    "pipeline_complete",
                    {"run_id": run_id, "pipeline": pipeline_name, "status": "failed"},
                )
                raise
            finally:
                # 念のため未終了なら掃除
                if proc.returncode is None:
                    proc.terminate()
                    with _suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)

            # exit code を最終行として永続化
            tail = f"[exit code: {returncode}]"
            tail_seq = self._repo.append_log_line(run_id, tail, stream="system")
            if tail_seq is not None:
                await self._registry.broadcast(
                    run_id,
                    LogEvent(type="line", seq=tail_seq, line=tail),
                )

            # 子プロセスが書き出した PipelineRunResult を取り込み、runs テーブルの
            # totals (total_fetched / summarized / posted / marked_read) を埋める。
            # 子側書き込み失敗・argv_override 経路 (テスト) では None になり、
            # 従来どおり 0 / exit code ベースの note を使う。
            result_payload = _read_run_result(run_id)
            base_status: Literal["succeeded", "failed"] = (
                "succeeded" if returncode == 0 else "failed"
            )
            if result_payload is not None:
                totals = _coerce_result_payload(result_payload)
                # 部分失敗を runs.status に反映 (in-process 経路の挙動に合わせる)
                if returncode == 0 and totals.errors:
                    final_status: RunStatus = "partial_failure"
                else:
                    final_status = base_status
                # 2026-08-01: soft deadline による繰越は失敗ではないが note で可視化する
                note_parts = list(totals.errors)
                if totals.deferred_count > 0:
                    note_parts.append(
                        f"時間予算内に処理しきれず {totals.deferred_count} 件を次 run へ繰越"
                    )
                self._repo.finish_run(
                    run_id,
                    status=final_status,
                    finished_at=datetime.now(UTC),
                    total_fetched=totals.total_fetched,
                    summarized=totals.summarized,
                    posted=totals.posted,
                    marked_read=totals.marked_read,
                    error_count=len(totals.errors),
                    note=("\n".join(note_parts) if note_parts else None),
                    triage_error_count=totals.triage_error_count,
                    partial_fetch=totals.partial_fetch,
                    partial_fetch_count=totals.partial_fetch_count,
                )
                # 監査 backlog 2026-07-05: 「完走したが errors あり」も親側で ops に届ける。
                # 子が自前通知済 (ops_notified=True、run_pipeline 経路) なら二重投稿しない。
                # digest/synthesis 系など自前通知を持たない pipeline の partial/failed が対象。
                if not totals.ops_notified:
                    if final_status == "partial_failure":
                        await self._notify_partial(run_id, pipeline_name, totals.errors)
                    elif final_status == "failed":
                        await self._notify_failure(
                            run_id,
                            pipeline_name,
                            f"exit code {returncode} · エラー {len(totals.errors)} 件",
                        )
                status = base_status
            else:
                status = base_status
                self._repo.finish_run(
                    run_id,
                    status=status,
                    finished_at=datetime.now(UTC),
                    total_fetched=0,
                    summarized=0,
                    posted=0,
                    marked_read=0,
                    error_count=0 if status == "succeeded" else 1,
                    note=None if status == "succeeded" else f"exit code {returncode}",
                )
                if status == "failed":
                    # result file なしの異常終了 = 子は自前の ops 通知に到達できずに死んだ
                    await self._notify_failure(
                        run_id, pipeline_name, f"exit code {returncode} (result file なし)"
                    )
            await self._registry.complete(run_id, status)
            await ws_broadcast(
                "pipeline_complete",
                {
                    "run_id": run_id,
                    "pipeline": pipeline_name,
                    "status": status,
                    "returncode": returncode,
                },
            )
            _log.info(
                "pipeline_subprocess_finished",
                run_id=run_id,
                status=status,
                returncode=returncode,
                result_payload_present=result_payload is not None,
            )
        except Exception as e:  # noqa: BLE001
            _log.error("pipeline_subprocess_crashed", run_id=run_id, error=str(e))
            await self._record_failure_and_notify(
                run_id, pipeline_name, f"runner crashed: {type(e).__name__}: {e}"
            )
            await self._registry.complete(run_id, "failed")
            await ws_broadcast(
                "pipeline_complete",
                {"run_id": run_id, "pipeline": pipeline_name, "status": "failed"},
            )
        finally:
            self._lock.release()

    async def _notify_failure(self, run_id: int, pipeline_name: str, message: str) -> None:
        """監査 2026-07-05 P2: 親プロセス検知の失敗を ops に通知する (失敗しても飲む)。

        従来は「run_pipeline 経路 × errors 非空」でしか通知が届かず、subprocess
        timeout / spawn 失敗 / runner crash は無音だった (子は通知前に死んでいる)。
        """
        try:
            from src.ui.services.ops_notify import notify_pipeline_failure

            await notify_pipeline_failure(pipeline_name, run_id, message)
        except Exception as e:  # noqa: BLE001 — 通知失敗で runner を殺さない
            _log.warning("failure_notify_failed", run_id=run_id, error=str(e))

    async def _notify_partial(self, run_id: int, pipeline_name: str, errors: list[str]) -> None:
        """監査 backlog 2026-07-05: 完走したが errors ありの run を ops に通知する。"""
        try:
            from src.ui.services.ops_notify import notify_pipeline_partial

            await notify_pipeline_partial(pipeline_name, run_id, errors)
        except Exception as e:  # noqa: BLE001 — 通知失敗で runner を殺さない
            _log.warning("partial_notify_failed", run_id=run_id, error=str(e))

    async def _record_failure_and_notify(
        self, run_id: int, pipeline_name: str, message: str
    ) -> None:
        self._record_failure(run_id, message)
        await self._notify_failure(run_id, pipeline_name, message)

    def _record_failure(self, run_id: int, message: str) -> None:
        self._repo.finish_run(
            run_id,
            status="failed",
            finished_at=datetime.now(UTC),
            total_fetched=0,
            summarized=0,
            posted=0,
            marked_read=0,
            error_count=1,
            note=message,
        )

    async def shutdown(self) -> None:
        """進行中の subprocess タスクを終了する (lifespan shutdown 用)。

        graceful 戦略:
          1. ``_DEFAULT_SHUTDOWN_GRACE_SECONDS`` (env ``PIPELINE_SHUTDOWN_GRACE_SECONDS``)
             まで自然完了を待つ → ほぼ完了寸前の run は救える
          2. grace 超過分のみ cancel + wait → 確実に shutdown を完了させる
        """
        if not self._tasks:
            return
        grace = _resolve_timeout_seconds(
            "PIPELINE_SHUTDOWN_GRACE_SECONDS",
            _DEFAULT_SHUTDOWN_GRACE_SECONDS,
        )
        tasks = list(self._tasks)
        _log.info("pipeline_runner_shutdown_begin", in_flight=len(tasks), grace_seconds=grace)
        done, pending = await asyncio.wait(tasks, timeout=grace)
        if pending:
            _log.warning(
                "pipeline_runner_shutdown_cancelling",
                completed=len(done),
                cancelling=len(pending),
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        else:
            _log.info("pipeline_runner_shutdown_graceful", completed=len(done))


# ----- helper: /prompts/dry-run 用の buffered subprocess wrapper -----
#
# Phase 1.5b 以前は /runs もこれを使っていたが、SSE + run_history 永続化に
# 移行したため、現状は /prompts/dry-run のみが利用する (履歴に残さない 1 回限りの
# プレビュー用)。Phase 5 で /prompts も run_id ベースに統合する余地あり。


async def stream_pipeline_output(
    *,
    dry_run: bool = True,
    max_articles: int | None = None,
) -> AsyncIterator[str]:
    """``python -m src.main`` を起動し、stdout を行単位で yield する (履歴非永続)。"""
    args: list[str] = [sys.executable, "-m", "src.main"]
    if dry_run:
        args.append("--dry-run")
    if max_articles is not None:
        args.extend(["--max-articles", str(max_articles)])

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line.decode(errors="replace").rstrip("\n")
    finally:
        await proc.wait()
        yield f"[exit code: {proc.returncode}]"


# 内部用: contextlib.suppress を関数で隠蔽 (mypy strict で型推論を簡潔に)


class _Suppress:
    def __init__(self, excs: tuple[type[BaseException], ...]) -> None:
        self._excs = excs

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> bool:
        return exc_type is not None and issubclass(exc_type, self._excs)


def _suppress(*excs: type[BaseException]) -> _Suppress:
    return _Suppress(excs)


class _ResultTotals:
    """``_read_run_result`` の生 dict を ``finish_run`` の引数型に揃える DTO。"""

    __slots__ = (
        "deferred_count",
        "errors",
        "marked_read",
        "ops_notified",
        "partial_fetch",
        "partial_fetch_count",
        "posted",
        "summarized",
        "total_fetched",
        "triage_error_count",
    )

    def __init__(
        self,
        *,
        total_fetched: int,
        summarized: int,
        posted: int,
        marked_read: int,
        errors: list[str],
        triage_error_count: int = 0,
        partial_fetch: bool = False,
        partial_fetch_count: int = 0,
        ops_notified: bool = False,
        deferred_count: int = 0,
    ) -> None:
        self.total_fetched = total_fetched
        self.summarized = summarized
        self.posted = posted
        self.marked_read = marked_read
        self.errors = errors
        self.triage_error_count = triage_error_count
        self.partial_fetch = partial_fetch
        self.partial_fetch_count = partial_fetch_count
        self.ops_notified = ops_notified
        self.deferred_count = deferred_count


def _coerce_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _coerce_result_payload(payload: dict[str, object]) -> _ResultTotals:
    raw_errors = payload.get("errors") or []
    errors: list[str] = [str(x) for x in raw_errors] if isinstance(raw_errors, list) else []
    return _ResultTotals(
        total_fetched=_coerce_int(payload.get("total_fetched", 0)),
        summarized=_coerce_int(payload.get("summarized", 0)),
        posted=_coerce_int(payload.get("posted", 0)),
        marked_read=_coerce_int(payload.get("marked_read", 0)),
        errors=errors,
        triage_error_count=_coerce_int(payload.get("triage_error_count", 0)),
        partial_fetch=bool(payload.get("partial_fetch", False)),
        partial_fetch_count=_coerce_int(payload.get("partial_fetch_count", 0)),
        ops_notified=bool(payload.get("ops_notified", False)),
        deferred_count=_coerce_int(payload.get("deferred_count", 0)),
    )


async def _maybe_app_broadcast_from_line(line: str, run_id: int) -> None:
    """subprocess stdout の 1 行を JSON parse し、対応する event なら app-wide push。

    subprocess (``python -m src.main``) は structlog で JSON line を吐いている。
    parent (本 process = uvicorn) で line を読みながら、Discord 投稿成功や synthesis 保存
    完了などの「クライアントが即座に知りたい状態変化」を WebSocket で全 client に push する。
    JSON parse 失敗 (非構造化行 / 部分行) は無視 (raw text は無害)。
    """
    if not line or not line.startswith("{"):
        return
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return
    event = d.get("event")
    if not isinstance(event, str):
        return
    ws_event = _STDOUT_EVENT_BROADCAST_MAP.get(event)
    if ws_event is None:
        return
    payload: dict[str, object] = {"run_id": run_id, "source_event": event}
    # 既知の補助情報を payload に乗せる (存在すれば)
    for key in ("period_type", "channel", "category", "importance"):
        if key in d:
            payload[key] = d[key]
    await ws_broadcast(ws_event, payload)


def _read_run_result(run_id: int) -> dict[str, object] | None:
    """``data/run_results/{run_id}.json`` を読み取り、消費後に削除する。

    存在しない / 壊れていれば None を返す (subprocess が書き出せなかったケース)。
    """
    path = _RUN_RESULTS_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log.warning("run_result_read_failed", run_id=run_id, error=str(e))
        return None
    finally:
        # 残しておくとディスクが溜まるだけなので消費後即削除
        with contextlib.suppress(OSError):
            path.unlink()
    return payload if isinstance(payload, dict) else None
