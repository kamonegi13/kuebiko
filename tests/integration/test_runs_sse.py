"""``/runs/{id}/stream`` (SSE) の integration テスト (Phase 1.5b)。

実 subprocess は使わず、PipelineRunner.start_subprocess_run を argv_override で
短い echo 系コマンドに差し替えて、実際の SSE 出力を検証する。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from src.storage.run_history import RunHistoryRepository, RunRecord
from src.ui.app import create_app
from src.ui.services.pipeline_runner import PipelineRunner
from src.ui.services.run_registry import LogEvent, RunRegistry


def _make_app(tmp_path: Path):  # type: ignore[no-untyped-def]
    """テスト用の最小 FastAPI アプリ。lifespan を介さず state を直接埋める。"""
    app = create_app()
    repo = RunHistoryRepository(db_path=tmp_path / "test_runs_sse.db")
    registry = RunRegistry()
    runner = PipelineRunner(repo=repo, registry=registry)
    app.state.repo = repo
    app.state.runner = runner
    return app, repo, runner


@pytest.mark.asyncio
async def test_log_endpoint_returns_text(tmp_path: Path) -> None:
    app, repo, _ = _make_app(tmp_path)
    from datetime import UTC, datetime

    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )
    repo.append_log_line(run_id, "alpha")
    repo.append_log_line(run_id, "beta")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/runs/{run_id}/log")
        assert resp.status_code == 200
        assert resp.text == "alpha\nbeta"

        resp_json = await client.get(f"/runs/{run_id}/log?fmt=json")
        assert resp_json.status_code == 200
        data = resp_json.json()
        assert [d["line"] for d in data] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_status_endpoint(tmp_path: Path) -> None:
    app, repo, _ = _make_app(tmp_path)
    from datetime import UTC, datetime

    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/runs/{run_id}/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == run_id
        assert body["status"] == "running"
        assert body["dry_run"] is False


@pytest.mark.asyncio
async def test_stream_finished_run_replays_and_closes(tmp_path: Path) -> None:
    """既に finish した run を stream すると、全行 + done を流して閉じる。"""
    app, repo, _ = _make_app(tmp_path)
    from datetime import UTC, datetime

    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )
    repo.append_log_line(run_id, "first")
    repo.append_log_line(run_id, "second")
    repo.finish_run(
        run_id,
        status="succeeded",
        finished_at=datetime.now(UTC),
        total_fetched=0,
        summarized=0,
        posted=0,
        marked_read=0,
        error_count=0,
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = ""
        async with client.stream("GET", f"/runs/{run_id}/stream") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for chunk in resp.aiter_text():
                body += chunk
                if "event: done" in body:
                    break

    assert "event: line" in body
    assert "first" in body
    assert "second" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_running_run_receives_live_events(tmp_path: Path) -> None:
    """running 状態で broadcast すると subscriber に届く。"""
    app, repo, runner = _make_app(tmp_path)
    from datetime import UTC, datetime

    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )

    transport = httpx.ASGITransport(app=app)

    async def push_events_after_delay() -> None:
        await asyncio.sleep(0.1)
        repo.append_log_line(run_id, "live-1")
        await runner.registry.broadcast(
            run_id,
            LogEvent(type="line", seq=1, line="live-1"),
        )
        await asyncio.sleep(0.05)
        repo.append_log_line(run_id, "live-2")
        await runner.registry.broadcast(
            run_id,
            LogEvent(type="line", seq=2, line="live-2"),
        )
        await runner.registry.complete(run_id, status="succeeded")

    pusher = asyncio.create_task(push_events_after_delay())
    body = ""
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
        client.stream("GET", f"/runs/{run_id}/stream") as resp,
    ):
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            body += chunk
            if "event: done" in body:
                break

    await pusher
    assert "live-1" in body
    assert "live-2" in body
    assert "event: done" in body


@pytest.mark.asyncio
async def test_stream_returns_404_for_missing_run(tmp_path: Path) -> None:
    app, _, _ = _make_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/runs/9999/stream")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_run_detail_redirects_to_spa(tmp_path: Path) -> None:
    """GET /runs/{id} は React SPA の詳細画面へリダイレクトする。

    Jinja → React 全面移行 (b284dd80) で HTML レンダリングは SPA に移り、この経路は
    legacy URL の互換 stub になった。ログ本文は ``/runs/{id}/log`` と SSE が担う。
    """
    app, repo, _ = _make_app(tmp_path)
    from datetime import UTC, datetime

    run_id = repo.start_run(
        RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=True),
    )
    repo.append_log_line(run_id, "preface")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/runs/{run_id}")
        assert resp.status_code == 302
        assert resp.headers["Location"] == f"/app/run/{run_id}"
        # ログ本文は専用経路から取得できる (SPA が SSE と併せて読む)
        log_resp = await client.get(f"/runs/{run_id}/log")
        assert log_resp.status_code == 200
        assert "preface" in log_resp.text


@pytest.mark.asyncio
async def test_run_detail_redirect_does_not_probe_existence(tmp_path: Path) -> None:
    """存在しない run でも stub は SPA に流す (存在判定は SPA 側の API が行う)。"""
    app, _, _ = _make_app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/runs/99999")
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/app/run/99999"
