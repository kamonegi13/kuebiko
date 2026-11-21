"""同じ実行の中の別々の Grok レポートが同じ投稿を含むとき、最初のレポートだけが扱う (2026-11-21)。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from src.pipeline.grok_convert import RunClaims, SeenFilter, grok_run_claim_enabled


def _ledger(known: set[str]) -> SeenFilter:
    def f(keys: Sequence[str]) -> set[str]:
        return {k for k in keys if k in known}

    return f


def test_second_report_sees_keys_claimed_by_first() -> None:
    claims = RunClaims()
    first = claims.filter_for("report-1", None)
    second = claims.filter_for("report-2", None)

    assert first(["t1", "t2"]) == set()
    assert second(["t2", "t3"]) == {"t2"}


def test_owner_does_not_block_itself_on_second_check() -> None:
    # drop_seen_records (要約の前) と filter_expanded_by_seen (展開の後) は同じレポートで 2 回照会する
    claims = RunClaims()
    f = claims.filter_for("report-1", None)

    assert f(["t1"]) == set()
    assert f(["t1"]) == set()


def test_ledger_known_keys_are_returned_and_not_claimed() -> None:
    claims = RunClaims()
    first = claims.filter_for("report-1", _ledger({"old"}))
    second = claims.filter_for("report-2", None)

    assert first(["old", "new"]) == {"old"}
    assert second(["old"]) == set()  # 台帳の既知は予約しない (台帳側で落ちる)
    assert second(["new"]) == {"new"}


def test_flag_default_on_and_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GROK_RUN_CLAIM", raising=False)
    assert grok_run_claim_enabled() is True
    monkeypatch.setenv("GROK_RUN_CLAIM", "0")
    assert grok_run_claim_enabled() is False
