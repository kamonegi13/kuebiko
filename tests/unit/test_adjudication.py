"""較正格子 P4: 係争の人間裁定のテスト。

裁定 = 適用の終端保証 (承認キュー 3 原則③): resolve_case が裁定記録とラベル操作を
必ず対で行う。二重裁定は関門で止まり、TTL 30 日の無応答は保守側 (E1 隔離) に倒れる。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.storage.run_history import RunHistoryRepository
from src.tuning.adjudication import expire_stale_cases, resolve_case

_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "adj.db")


def _seed_case(
    repo: RunHistoryRepository,
    *,
    case_key: str = "subjpanel:a1:qilin",
    article_id: str = "a1",
    truth: str = "qilin",
    agreement: str = "unanimous_wrong",
    panel_value: str = "",
    when: datetime = _NOW,
) -> int:
    """係争 1 件 (E1 ラベル + パネル verdict) を seed する。"""
    label_id = repo.record_tuning_label(
        dedup_key=f"feedmatch:{article_id}:{truth}",
        field="subject_actor",
        label_value=truth,
        source="E1",
        strength="strong",
        provenance="{}",
        article_id=article_id,
    )
    assert label_id is not None
    verdicts = json.dumps(
        [{"model": "main", "value": panel_value}, {"model": "second", "value": panel_value}]
    )
    repo.record_panel_verdict(
        case_key=case_key,
        field="subject_actor",
        article_id=article_id,
        truth_value=truth,
        production_value="",
        verdicts=verdicts,
        agreement=agreement,
        is_dispute=True,
        when=when,
    )
    return label_id


def _active_e1(repo: RunHistoryRepository) -> list[str]:
    return [
        r["label_value"]
        for r in repo.list_tuning_labels(field="subject_actor")
        if r["source"] == "E1" and r["superseded_by"] is None
    ]


class TestResolveCase:
    def test_label_wrong_quarantines_e1_and_records_e3(self, repo: RunHistoryRepository) -> None:
        _seed_case(repo, panel_value="")
        result = resolve_case(repo, case_key="subjpanel:a1:qilin", resolution="label_wrong")
        assert result.ok
        assert result.superseded == 1
        assert _active_e1(repo) == []  # E1 は隔離された
        e3 = [
            r
            for r in repo.list_tuning_labels(field="subject_actor")
            if r["source"] == "E3" and r["superseded_by"] is None
        ]
        assert len(e3) == 1
        assert e3[0]["label_value"] == ""  # パネル全員一致の値 (空) を E3 に記録
        assert repo.list_pending_adjudications() == []  # キューから消える

    def test_label_correct_keeps_e1_and_adds_confirmation(self, repo: RunHistoryRepository) -> None:
        _seed_case(repo, panel_value="akira")
        result = resolve_case(repo, case_key="subjpanel:a1:qilin", resolution="label_correct")
        assert result.ok
        assert result.superseded == 0
        assert _active_e1(repo) == ["qilin"]  # E1 は現行のまま
        e3 = [r for r in repo.list_tuning_labels(field="subject_actor") if r["source"] == "E3"]
        assert len(e3) == 1
        assert e3[0]["label_value"] == "qilin"  # E1 の値を確認として記録

    def test_double_resolution_is_blocked(self, repo: RunHistoryRepository) -> None:
        _seed_case(repo)
        assert resolve_case(repo, case_key="subjpanel:a1:qilin", resolution="label_wrong").ok
        second = resolve_case(repo, case_key="subjpanel:a1:qilin", resolution="label_correct")
        assert not second.ok
        assert "裁定済み" in second.reason

    def test_unknown_and_non_case_are_rejected(self, repo: RunHistoryRepository) -> None:
        assert not resolve_case(repo, case_key="nope", resolution="label_wrong").ok
        # unanimous_correct (係争でない) は裁定対象外
        _seed_case(repo, case_key="ok:1", agreement="unanimous_correct", panel_value="qilin")
        assert not resolve_case(repo, case_key="ok:1", resolution="label_wrong").ok


class TestTtlExpiry:
    def test_stale_case_expires_without_destroying_label(self, repo: RunHistoryRepository) -> None:
        """§13-2 対処: TTL はキュー整理のみ — パネルが割れただけで錨 (E1) を消さない。"""
        _seed_case(repo, when=_NOW - timedelta(days=40))
        expired = expire_stale_cases(repo, now=_NOW)
        assert expired == 1
        assert repo.list_pending_adjudications() == []
        assert _active_e1(repo) == ["qilin"]  # ラベルは非破壊で残る
        recent = repo.list_recent_resolutions()
        assert recent[0]["resolution"] == "expired"
        assert recent[0]["resolved_by"] == "ttl"

    def test_fresh_case_is_kept(self, repo: RunHistoryRepository) -> None:
        _seed_case(repo, when=_NOW - timedelta(days=5))
        assert expire_stale_cases(repo, now=_NOW) == 0
        assert len(repo.list_pending_adjudications()) == 1
        assert _active_e1(repo) == ["qilin"]
