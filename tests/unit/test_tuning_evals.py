"""較正格子 P2: tuning_evals repo + C7 自動 rollback + goldset cron のテスト。

C7 は本設計唯一の無人適用 — decide_rollback 純関数にガード (劣化時のみ / 前版必須 /
連鎖禁止 / 一版一裁定) を集約し、ここで固定する。既定はシャドー (§10.1 シャドー先行)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.storage.config_store import ConfigVersion
from src.storage.run_history import RunHistoryRepository
from src.tuning.auto_rollback import (
    ROLLBACK_NOTE_PREFIX,
    decide_rollback,
    maybe_auto_rollback,
)
from src.tuning.goldset_cron import GoldsetCronOutcome, run_weekly_goldset_eval

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


class TestDecideRollback:
    def _decide(self, **kw: Any) -> Any:
        base: dict[str, Any] = {
            "verify_exit": 1,
            "catastrophic": True,
            "latest_version": 5,
            "latest_note": "UI から保存",
            "previous_version": 4,
            "already_decided": False,
            "auto_apply_enabled": False,
        }
        return decide_rollback(**{**base, **kw})

    def test_pass_and_inconclusive_do_nothing(self) -> None:
        assert self._decide(verify_exit=0).action == "none"
        assert self._decide(verify_exit=2).action == "none"  # 判定不能では動かない (保守側)

    def test_non_catastrophic_fail_does_not_rollback(self) -> None:
        # H6 (2026-08-22): 充足率の小幅ゲート・要約長の FAIL は無人適用に繋がない。
        # 無人で動いてよいのは破局ゲート (分布 >20pt / 全滅級 >15pt) のみ。
        d = self._decide(catastrophic=False)
        assert d.action == "none"
        assert "破局" in d.reason

    def test_no_previous_version_cannot_rollback(self) -> None:
        assert self._decide(previous_version=None).action == "none"
        assert self._decide(latest_version=None).action == "none"

    def test_rollback_of_rollback_is_refused(self) -> None:
        d = self._decide(latest_note=f"{ROLLBACK_NOTE_PREFIX} v5 劣化検出 → v4 復元")
        assert d.action == "none"
        assert "連鎖" in d.reason

    def test_one_decision_per_version(self) -> None:
        assert self._decide(already_decided=True).action == "none"

    def test_default_is_shadow_and_flag_enables_apply(self) -> None:
        shadow = self._decide()
        assert shadow.action == "shadow"
        assert (shadow.degraded_version, shadow.restore_version) == (5, 4)
        applied = self._decide(auto_apply_enabled=True)
        assert applied.action == "apply"


class TestMaybeAutoRollback:
    @pytest.fixture
    def history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        versions = [
            ConfigVersion(version=5, note="UI から保存", created_at=_NOW.isoformat()),
            ConfigVersion(version=4, note="", created_at=(_NOW - timedelta(days=8)).isoformat()),
        ]
        monkeypatch.setattr(
            "src.storage.config_store.list_history", lambda key, *, limit=50: versions[:limit]
        )

    @pytest.mark.asyncio
    async def test_shadow_records_without_applying(
        self, repo: RunHistoryRepository, history: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TUNING_AUTO_ROLLBACK", raising=False)
        saved: list[Any] = []

        def _capture_save(*a: Any, **k: Any) -> int:
            saved.append((a, k))
            return 99

        monkeypatch.setattr("src.storage.config_store.save_config", _capture_save)
        line = await maybe_auto_rollback(verify_exit=1, catastrophic=True, repo=repo)
        assert line is not None and "シャドー" in line
        assert saved == []  # 適用していない
        rec = repo.find_tuning_eval(prompt_id="summarizer", kind="auto_rollback", to_version=5)
        assert rec is not None
        assert rec["verdict"] == "would_rollback"
        assert rec["mode"] == "shadow"

    @pytest.mark.asyncio
    async def test_second_call_is_blocked_by_prior_decision(
        self, repo: RunHistoryRepository, history: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TUNING_AUTO_ROLLBACK", raising=False)
        await maybe_auto_rollback(verify_exit=1, catastrophic=True, repo=repo)
        line = await maybe_auto_rollback(verify_exit=1, catastrophic=True, repo=repo)
        assert line is not None and "見送り" in line
        assert len(repo.list_tuning_evals()) == 1  # 裁定は 1 回だけ

    @pytest.mark.asyncio
    async def test_apply_when_flag_enabled(
        self, repo: RunHistoryRepository, history: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TUNING_AUTO_ROLLBACK", "1")
        monkeypatch.setattr(
            "src.storage.config_store.get_config_version",
            lambda key, version, **k: {"sections": []},
        )
        saved: list[tuple[Any, ...]] = []

        def _fake_save(key: str, value: Any, *, note: str = "", **k: Any) -> int:
            saved.append((key, note))
            return 6

        monkeypatch.setattr("src.storage.config_store.save_config", _fake_save)
        monkeypatch.setattr("src.prompts.rubric_store.invalidate_summarizer_cache", lambda: None)
        line = await maybe_auto_rollback(verify_exit=1, catastrophic=True, repo=repo)
        assert line is not None and "適用" in line
        assert saved and saved[0][0] == "summarizer_rubric"
        assert saved[0][1].startswith(ROLLBACK_NOTE_PREFIX)
        rec = repo.find_tuning_eval(prompt_id="summarizer", kind="auto_rollback", to_version=5)
        assert rec is not None and rec["verdict"] == "rolled_back"

    @pytest.mark.asyncio
    async def test_verify_pass_returns_nothing(
        self, repo: RunHistoryRepository, history: None
    ) -> None:
        assert await maybe_auto_rollback(verify_exit=0, repo=repo) is None
        assert repo.list_tuning_evals() == []


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

        monkeypatch.setattr("src.tuning.goldset_cron._notify", _capture)
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
