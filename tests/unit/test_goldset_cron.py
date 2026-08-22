"""tuning_evals repo + goldset 切替評価 cron のテスト。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.eval.goldset_cron import GoldsetCronOutcome, run_weekly_goldset_eval
from src.storage.config_store import ConfigVersion
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 8, 22, 3, 10, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "evals.db")


class TestTuningEvalsRepo:
    def test_record_find_list(self, repo: RunHistoryRepository) -> None:
        eid = repo.record_tuning_eval(
            prompt_id="summarizer",
            kind="goldset_cutover",
            verdict="pass",
            mode="measure",
            detail="{}",
            from_version=4,
            to_version=5,
        )
        assert eid > 0
        found = repo.find_tuning_eval(prompt_id="summarizer", kind="goldset_cutover", to_version=5)
        assert found is not None
        assert found["verdict"] == "pass"
        assert (
            repo.find_tuning_eval(prompt_id="summarizer", kind="goldset_cutover", to_version=6)
            is None
        )
        rows = repo.list_tuning_evals()
        assert len(rows) == 1
        assert rows[0]["from_version"] == 4


def _versions(latest_at: datetime) -> list[ConfigVersion]:
    return [
        ConfigVersion(version=5, note="UI から保存", created_at=latest_at.isoformat()),
        ConfigVersion(version=4, note="", created_at=(latest_at - timedelta(days=9)).isoformat()),
    ]


class TestGoldsetCron:
    @pytest.fixture(autouse=True)
    def silence_notify(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        titles: list[str] = []

        async def _capture(*, title: str, body: str, importance: str) -> None:
            titles.append(title)

        monkeypatch.setattr("src.eval.goldset_cron._notify", _capture)
        return titles

    @pytest.mark.asyncio
    async def test_no_second_version_skips(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history",
            lambda key, *, limit=50: [_versions(_NOW)[0]],
        )
        out = await run_weekly_goldset_eval(repo=repo, now=_NOW)
        assert out.status == "skipped_no_change"

    @pytest.mark.asyncio
    async def test_old_change_skips_quietly(
        self, repo: RunHistoryRepository, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history",
            lambda key, *, limit=50: _versions(_NOW - timedelta(days=30)),
        )
        out = await run_weekly_goldset_eval(repo=repo, now=_NOW)
        assert out.status == "skipped_no_change"

    @pytest.mark.asyncio
    async def test_missing_goldset_warns(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        silence_notify: list[str],
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history", lambda key, *, limit=50: _versions(_NOW)
        )
        out = await run_weekly_goldset_eval(
            repo=repo, now=_NOW, goldset_path=tmp_path / "missing.jsonl"
        )
        assert out.status == "skipped_no_goldset"
        assert any("未整備" in t for t in silence_notify)

    @pytest.mark.asyncio
    async def test_full_run_records_verdict_and_is_idempotent(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history", lambda key, *, limit=50: _versions(_NOW)
        )
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text("{}\n", encoding="utf-8")

        calls: list[list[str]] = []

        async def fake_run(args: list[str], timeout: int) -> tuple[int, str]:
            calls.append(args)
            if args[1] == "run":
                return (0, "ok")
            payload = {
                "label_a": "cron-v4",
                "label_b": "cron-v5-a",
                "shared": 86,
                "floor_shared": 86,
                "fields": [
                    {
                        "field": "analyst_note",
                        "delta_pt": -8.2,
                        "noise_pt": 3.0,
                        "is_change": True,
                    },
                    {"field": "summary", "delta_pt": 0.5, "noise_pt": 3.0, "is_change": False},
                    {
                        "field": "victim_sector",
                        "delta_pt": 6.0,
                        "noise_pt": 2.0,
                        "is_change": True,
                    },
                ],
            }
            return (0, json.dumps(payload))

        out = await run_weekly_goldset_eval(
            repo=repo, now=_NOW, goldset_path=goldset, run_script=fake_run
        )
        assert out.status == "ran"
        assert out.verdict == "degraded"  # analyst_note -8.2pt が床超え
        # 3 run とも明示版で pin されている
        run_calls = [c for c in calls if c[1] == "run"]
        assert len(run_calls) == 3
        assert all("--rubric-version" in c for c in run_calls)
        rec = repo.find_tuning_eval(prompt_id="summarizer", kind="goldset_cutover", to_version=5)
        assert rec is not None and rec["verdict"] == "degraded"

        # 2 回目は評価済みスキップ (冪等)
        out2 = await run_weekly_goldset_eval(
            repo=repo, now=_NOW, goldset_path=goldset, run_script=fake_run
        )
        assert out2.status == "skipped_already"

    @pytest.mark.asyncio
    async def test_run_failure_records_nothing(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history", lambda key, *, limit=50: _versions(_NOW)
        )
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text("{}\n", encoding="utf-8")

        async def fail_run(args: list[str], timeout: int) -> tuple[int, str]:
            return (1, "boom")

        out = await run_weekly_goldset_eval(
            repo=repo, now=_NOW, goldset_path=goldset, run_script=fail_run
        )
        assert out.status == "error"
        assert repo.list_tuning_evals() == []  # 来週再試行できる

    @pytest.mark.asyncio
    async def test_dry_run_reports_plan_without_llm(
        self,
        repo: RunHistoryRepository,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr(
            "src.storage.config_store.list_history", lambda key, *, limit=50: _versions(_NOW)
        )
        goldset = tmp_path / "goldset.jsonl"
        goldset.write_text("{}\n", encoding="utf-8")

        async def must_not_run(args: list[str], timeout: int) -> tuple[int, str]:
            raise AssertionError("dry_run で subprocess を呼んではいけない")

        out = await run_weekly_goldset_eval(
            dry_run=True, repo=repo, now=_NOW, goldset_path=goldset, run_script=must_not_run
        )
        assert isinstance(out, GoldsetCronOutcome)
        assert out.status == "dry_run"
        assert "v4→v5" in out.detail
