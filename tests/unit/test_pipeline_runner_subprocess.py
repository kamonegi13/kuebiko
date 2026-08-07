"""src.ui.services.pipeline_runner.PipelineRunner.start_subprocess_run のテスト。

Phase 1.5b: SSE + DB live streaming の subprocess 経路。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.ui.services import pipeline_runner
from src.ui.services.pipeline_runner import PipelineBusyError, PipelineRunner


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "subprocess.db")


def _echo_argv(*lines: str) -> list[str]:
    """改行区切りで複数行を出力する Python ワンライナーを組み立てる。"""
    body = "import sys; " + "; ".join(
        f"sys.stdout.write({line!r} + '\\n'); sys.stdout.flush()" for line in lines
    )
    return [sys.executable, "-c", body]


class TestSubprocessRun:
    @pytest.mark.asyncio
    async def test_returns_run_id_immediately_and_persists_lines(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=_echo_argv("hello", "world"),
        )
        assert run_id >= 1

        # subprocess の自然終了を待つ (shutdown は cancel するので使わない)
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        lines = repo.get_log_lines(run_id)
        # stdout 2 行 + exit code 1 行
        assert [line.line for line in lines][:2] == ["hello", "world"]
        assert lines[-1].line.startswith("[exit code:")

    @pytest.mark.asyncio
    async def test_finishes_run_with_status(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=_echo_argv("a"),
        )
        # 完了を待つため tasks をすべて await
        for task in list(runner._tasks):  # noqa: SLF001 (test introspection)
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.finished_at is not None

    @pytest.mark.asyncio
    async def test_raises_when_busy(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        runner = PipelineRunner(repo=repo)
        # 1 回目: わざと長め (sleep 0.5)
        long_argv = [sys.executable, "-c", "import time; time.sleep(0.5)"]
        await runner.start_subprocess_run(argv_override=long_argv)
        # まだ終わっていないので 2 回目は busy
        await asyncio.sleep(0.05)
        with pytest.raises(PipelineBusyError):
            await runner.start_subprocess_run(argv_override=_echo_argv("nope"))
        # 後始末
        await runner.shutdown()

    @pytest.mark.asyncio
    async def test_broadcasts_events_to_registry(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        runner = PipelineRunner(repo=repo)
        events = []

        async def consume(run_id: int) -> None:
            async for event in runner.registry.subscribe(run_id):
                events.append(event)
                if event.type == "done":
                    break

        run_id = await runner.start_subprocess_run(
            argv_override=_echo_argv("L1", "L2"),
        )
        consumer = asyncio.create_task(consume(run_id))
        await asyncio.wait_for(consumer, timeout=10.0)

        types = [e.type for e in events]
        assert "done" in types
        assert types.count("line") >= 2
        # done が最後に来る
        assert types[-1] == "done"

    @pytest.mark.asyncio
    async def test_subprocess_failure_marks_run_failed(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        runner = PipelineRunner(repo=repo)
        # 非 0 終了
        fail_argv = [sys.executable, "-c", "import sys; sys.exit(2)"]
        run_id = await runner.start_subprocess_run(argv_override=fail_argv)
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert loaded.note is not None
        assert "exit code 2" in loaded.note

    @pytest.mark.asyncio
    async def test_reads_pipeline_result_file_for_finish_run_totals(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """子プロセスが書き出した PipelineRunResult JSON を取り込み、
        runs.total_fetched / summarized / posted / marked_read を埋める。

        Phase 5A fix: 以前は subprocess 経路がこれらを常に 0 で書いていたため、
        ダッシュボードと履歴に件数が出なかった。
        """
        # 子プロセスが期待する result file を runner が読む先 (data/run_results/) を
        # tmp_path に向ける
        results_dir = tmp_path / "results"
        monkeypatch.setattr(pipeline_runner, "_RUN_RESULTS_DIR", results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        # 「子プロセス役」スクリプト: stdout に 1 行出して、result file を書いてから exit 0
        runner = PipelineRunner(repo=repo)
        # run_id を事前確保するため、一度 echo argv で起動 → DB に行を作る
        run_id = await runner.start_subprocess_run(
            argv_override=[
                sys.executable,
                "-c",
                (
                    "import json, pathlib, sys; "
                    f"p = pathlib.Path({str(results_dir)!r}); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    # 親が読みに行く file 名は run_id ベース → 進行中の run_id に合わせる必要があり
                    # ここでは固定 run_id=1 が払い出される前提でテストを書く
                    "(p / '1.json').write_text(json.dumps({"
                    "'run_id': 1, 'total_fetched': 7, 'skipped_dup': 2, "
                    "'summarized': 5, 'posted': 5, 'marked_read': 5, "
                    "'errors': [], 'dry_run': False"
                    "})); "
                    "sys.stdout.write('done\\n'); sys.stdout.flush()"
                ),
            ],
        )
        assert run_id == 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.total_fetched == 7
        assert loaded.summarized == 5
        assert loaded.posted == 5
        assert loaded.marked_read == 5
        assert loaded.error_count == 0
        # 取り込み後は file が削除されている (ディスク累積防止)
        assert not (results_dir / "1.json").exists()

    @pytest.mark.asyncio
    async def test_partial_failure_reflected_when_errors_present(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """returncode==0 でも errors 配列があれば runs.status を partial_failure に倒す。"""
        results_dir = tmp_path / "results"
        monkeypatch.setattr(pipeline_runner, "_RUN_RESULTS_DIR", results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=[
                sys.executable,
                "-c",
                (
                    "import json, pathlib, sys; "
                    f"p = pathlib.Path({str(results_dir)!r}); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    "(p / '1.json').write_text(json.dumps({"
                    "'run_id': 1, 'total_fetched': 3, 'skipped_dup': 0, "
                    "'summarized': 2, 'posted': 2, 'marked_read': 2, "
                    "'errors': ['a-bad: RuntimeError: boom'], 'dry_run': False"
                    "})); "
                    "sys.stdout.write('done\\n'); sys.stdout.flush()"
                ),
            ],
        )
        assert run_id == 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "partial_failure"
        assert loaded.error_count == 1
        assert loaded.note is not None and "boom" in loaded.note

    @pytest.mark.asyncio
    async def test_partial_failure_notifies_ops_when_child_did_not(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """完走 + errors あり + 子が未通知 (ops_notified=False) → 親が 🟡 通知する。"""
        results_dir = tmp_path / "results"
        monkeypatch.setattr(pipeline_runner, "_RUN_RESULTS_DIR", results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        calls: list[tuple[str, int, list[str]]] = []

        async def fake_partial(pipeline_name: str, run_id: int, errors: list[str]) -> None:
            calls.append((pipeline_name, run_id, errors))

        from src.ui.services import ops_notify

        monkeypatch.setattr(ops_notify, "notify_pipeline_partial", fake_partial)

        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=[
                sys.executable,
                "-c",
                (
                    "import json, pathlib, sys; "
                    f"p = pathlib.Path({str(results_dir)!r}); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    "(p / '1.json').write_text(json.dumps({"
                    "'run_id': 1, 'total_fetched': 3, 'posted': 2, "
                    "'errors': ['x: boom'], 'ops_notified': False"
                    "})); "
                    "sys.stdout.write('done\\n')"
                ),
            ],
        )
        assert run_id == 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        assert calls == [("daily-briefing", 1, ["x: boom"])]

    @pytest.mark.asyncio
    async def test_partial_failure_skips_notify_when_child_already_notified(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """子が自前通知済 (ops_notified=True) なら親は二重投稿しない。"""
        results_dir = tmp_path / "results"
        monkeypatch.setattr(pipeline_runner, "_RUN_RESULTS_DIR", results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        calls: list[tuple[str, int, list[str]]] = []

        async def fake_partial(pipeline_name: str, run_id: int, errors: list[str]) -> None:
            calls.append((pipeline_name, run_id, errors))

        from src.ui.services import ops_notify

        monkeypatch.setattr(ops_notify, "notify_pipeline_partial", fake_partial)

        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=[
                sys.executable,
                "-c",
                (
                    "import json, pathlib, sys; "
                    f"p = pathlib.Path({str(results_dir)!r}); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    "(p / '1.json').write_text(json.dumps({"
                    "'run_id': 1, 'total_fetched': 3, 'posted': 2, "
                    "'errors': ['x: boom'], 'ops_notified': True"
                    "})); "
                    "sys.stdout.write('done\\n')"
                ),
            ],
        )
        assert run_id == 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "partial_failure"
        assert calls == []

    @pytest.mark.asyncio
    async def test_failed_with_result_file_notifies_ops_when_child_did_not(
        self,
        repo: RunHistoryRepository,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """returncode!=0 + result file あり + 子が未通知 → 親が 🔴 通知する。

        従来は result file **なし** の異常死しか親側通知が無かった
        (result file ありの failed は子の自前通知前提 = 自前通知を持たない
        pipeline では無音だった)。
        """
        results_dir = tmp_path / "results"
        monkeypatch.setattr(pipeline_runner, "_RUN_RESULTS_DIR", results_dir)
        results_dir.mkdir(parents=True, exist_ok=True)

        calls: list[tuple[str, int, str]] = []

        async def fake_failure(pipeline_name: str, run_id: int, note: str) -> None:
            calls.append((pipeline_name, run_id, note))

        from src.ui.services import ops_notify

        monkeypatch.setattr(ops_notify, "notify_pipeline_failure", fake_failure)

        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(
            argv_override=[
                sys.executable,
                "-c",
                (
                    "import json, pathlib, sys; "
                    f"p = pathlib.Path({str(results_dir)!r}); "
                    "p.mkdir(parents=True, exist_ok=True); "
                    "(p / '1.json').write_text(json.dumps({"
                    "'run_id': 1, 'total_fetched': 3, 'posted': 0, "
                    "'errors': ['fatal: all posts failed'], 'ops_notified': False"
                    "})); "
                    "sys.exit(1)"
                ),
            ],
        )
        assert run_id == 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert len(calls) == 1
        assert calls[0][0] == "daily-briefing"
        assert calls[0][1] == 1
        assert "exit code 1" in calls[0][2]


class TestSubprocessArgs:
    @pytest.mark.asyncio
    async def test_run_id_flag_is_appended_for_default_argv(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """argv_override を使わない通常パスでは ``--run-id N`` が args に含まれること。"""
        captured: dict[str, list[str]] = {}
        original = asyncio.create_subprocess_exec

        async def fake_subprocess_exec(
            *args: str,
            **kwargs: object,  # noqa: ARG001
        ) -> object:
            captured["args"] = list(args)
            # 即座に exit する fake proc を返す (パッチ前の関数を呼ぶ)
            return await original(
                sys.executable,
                "-c",
                "pass",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            fake_subprocess_exec,
        )

        runner = PipelineRunner(repo=repo)
        # 幽霊 daily-briefing 撤去 (2026-07-05) 後は有効 pipeline 名を使う
        run_id = await runner.start_subprocess_run(
            dry_run=True,
            pipeline_name="direct-rss-fetch",
        )
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        assert "--run-id" in captured["args"]
        assert str(run_id) in captured["args"]
        assert "--dry-run" in captured["args"]


class TestSubprocessTimeout:
    """A: subprocess wallclock timeout (PIPELINE_TIMEOUT_SECONDS)。"""

    @pytest.mark.asyncio
    async def test_subprocess_exceeding_timeout_is_killed_and_marked_failed(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """sleep 10s の subprocess を 0.5s timeout で起動 → kill → failed + note にも反映。"""
        monkeypatch.setenv("PIPELINE_TIMEOUT_SECONDS", "0.5")
        runner = PipelineRunner(repo=repo)
        # 何も出力せず 10 秒 sleep する subprocess
        long_argv = [sys.executable, "-c", "import time; time.sleep(10)"]
        run_id = await runner.start_subprocess_run(argv_override=long_argv)
        for task in list(runner._tasks):  # noqa: SLF001
            await task

        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert loaded.note is not None
        assert "timeout exceeded" in loaded.note

    @pytest.mark.asyncio
    async def test_subprocess_within_timeout_completes_normally(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """通常 echo は timeout 設定の影響を受けず succeeded で完了する。"""
        monkeypatch.setenv("PIPELINE_TIMEOUT_SECONDS", "5.0")
        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(argv_override=_echo_argv("ok"))
        for task in list(runner._tasks):  # noqa: SLF001
            await task
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "succeeded"


class TestShutdownGraceful:
    """C: shutdown graceful wait (PIPELINE_SHUTDOWN_GRACE_SECONDS)。"""

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_short_running_subprocess(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """grace 内に終わる run は自然完了して succeeded のまま残る。"""
        monkeypatch.setenv("PIPELINE_SHUTDOWN_GRACE_SECONDS", "5.0")
        runner = PipelineRunner(repo=repo)
        short_argv = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        run_id = await runner.start_subprocess_run(argv_override=short_argv)
        # graceful shutdown は 5s 待つ → 0.3s の subprocess は自然完了する
        await runner.shutdown()
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "succeeded"
        assert loaded.note != "cancelled by shutdown"

    @pytest.mark.asyncio
    async def test_shutdown_cancels_long_running_subprocess(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """grace を超える run は cancel されて failed に倒される。"""
        monkeypatch.setenv("PIPELINE_SHUTDOWN_GRACE_SECONDS", "0.3")
        runner = PipelineRunner(repo=repo)
        long_argv = [sys.executable, "-c", "import time; time.sleep(10)"]
        run_id = await runner.start_subprocess_run(argv_override=long_argv)
        # 0.3s grace → 10s sleep には足りない → cancel
        await runner.shutdown()
        loaded = repo.get_run(run_id)
        assert loaded is not None
        assert loaded.status == "failed"
        assert loaded.note == "cancelled by shutdown"


class TestUnknownPipelineGuard:
    """防御 (2026-07-05): 撤去済の幽霊 pipeline 名で run させない。

    既定 pipeline_name='daily-briefing' は pipelines.yaml から撤去済。無効名の run は
    main.py が既定で失敗し、親側失敗通知が ops に「daily-briefing run 失敗」を投稿する
    罠になる。run 生成前に fail-fast することを固定する。
    """

    @pytest.mark.asyncio
    async def test_rejects_ghost_daily_briefing(self, repo: RunHistoryRepository) -> None:
        from src.ui.services.pipeline_runner import UnknownPipelineError

        runner = PipelineRunner(repo=repo)
        with pytest.raises(UnknownPipelineError):
            await runner.start_subprocess_run(pipeline_name="daily-briefing")
        # 幽霊 run が DB に記録されていない (ops スパムの前段の run 生成も起きない)
        assert repo.list_runs(limit=10) == []

    @pytest.mark.asyncio
    async def test_accepts_valid_pipeline(self, repo: RunHistoryRepository) -> None:
        runner = PipelineRunner(repo=repo)
        # 実 subprocess は起動するが echo override で即終了 (valid 名 + override)
        run_id = await runner.start_subprocess_run(
            pipeline_name="direct-rss-fetch",
            argv_override=_echo_argv("ok"),
        )
        assert run_id >= 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task

    @pytest.mark.asyncio
    async def test_argv_override_bypasses_validation(self, repo: RunHistoryRepository) -> None:
        # argv_override 指定時は名前検証を skip (既存テスト互換の担保)
        runner = PipelineRunner(repo=repo)
        run_id = await runner.start_subprocess_run(argv_override=_echo_argv("x"))
        assert run_id >= 1
        for task in list(runner._tasks):  # noqa: SLF001
            await task


class TestPerPipelineTimeout:
    """P2 (2026-07-06): per-pipeline timeout の解決順序 (env個別→env global→override→既定)。"""

    def test_default_for_normal_pipeline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PIPELINE_TIMEOUT_SECONDS", raising=False)
        assert pipeline_runner._resolve_pipeline_timeout("direct-rss-fetch") == 1800.0

    def test_weekly_synthesis_gets_headroom(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PIPELINE_TIMEOUT_SECONDS", raising=False)
        # コード override で重い weekly synthesis のみ headroom (pir-spotlight 03:30 を侵さない範囲)
        assert pipeline_runner._resolve_pipeline_timeout("weekly-status-synthesis") == 2400.0

    def test_env_global_overrides_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIPELINE_TIMEOUT_SECONDS", "600")
        assert pipeline_runner._resolve_pipeline_timeout("weekly-status-synthesis") == 600.0

    def test_env_per_pipeline_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PIPELINE_TIMEOUT_SECONDS", "600")
        monkeypatch.setenv("PIPELINE_TIMEOUT_SECONDS_WEEKLY_RECAP", "3300")
        assert pipeline_runner._resolve_pipeline_timeout("weekly-recap") == 3300.0


def test_monthly_synthesis_timeout_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    """monthly は最重のため per-pipeline timeout で 3600s の headroom を持つ。"""
    monkeypatch.delenv("PIPELINE_TIMEOUT_SECONDS", raising=False)
    assert pipeline_runner._resolve_pipeline_timeout("monthly-status-synthesis") == 3600.0
