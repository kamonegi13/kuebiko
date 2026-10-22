"""常設の問いの根拠の「直接 (日本・同盟国) / 間接 (それ以外)」の印 (2026-10-22)。

印を付けるだけで答えの重みは変えない。消費者 = 答えの画面 (posture カード) の件数、
および fill_rate_audit の被害国の充足率。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.assessment.situation_store import SituationStore
from src.assessment.standing import ensure_standing_situations
from src.assessment.standing_proximity import count_proximity, summarize_proximity
from src.storage.run_history import RunHistoryRepository

_NOW = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
_NOW_ISO = _NOW.isoformat()
_SID = "s-standing-prepos-cn"


class TestSummarize:
    def test_splits_by_victim_country(self) -> None:
        got = summarize_proximity(["JP", "us", "PL", "UA", None, ""])
        assert got == {"direct": 2, "indirect": 2, "unknown": 2}

    def test_empty_input_gives_zero_counts(self) -> None:
        assert summarize_proximity([]) == {"direct": 0, "indirect": 0, "unknown": 0}


@pytest.fixture
def store(tmp_path: Path) -> SituationStore:
    db = tmp_path / "prox.db"
    RunHistoryRepository(db_path=db)
    return SituationStore(db_path=db)


def _evidence(store: SituationStore, aid: str, country: str | None, *, created: str) -> None:
    with store._repo._connect() as conn:  # noqa: SLF001
        conn.execute(
            "INSERT INTO runs (started_at, pipeline, dry_run, status) VALUES (?, 't', 0, 'done')",
            (_NOW_ISO,),
        )
        rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
        conn.execute(
            "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
            " victim_country_iso) VALUES (?,?,?,?, 'posted', ?, ?)",
            (rid, aid, f"t-{aid}", f"https://x/{aid}", created, country),
        )
    store.record_assignment(
        situation_id=_SID, article_id=aid, added_at=_NOW_ISO, assigned_by="standing"
    )


class TestCountProximity:
    def test_counts_evidence_in_window_by_proximity(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _evidence(store, "a1", "JP", created=_NOW_ISO)
        _evidence(store, "a2", "US", created=_NOW_ISO)
        _evidence(store, "a3", "PL", created=_NOW_ISO)
        _evidence(store, "a4", None, created=_NOW_ISO)
        _evidence(store, "old", "JP", created="2026-08-01T00:00:00+00:00")
        with store._repo._connect() as conn:  # noqa: SLF001
            got = count_proximity(conn, _SID, since_iso="2026-09-22T00:00:00+00:00")
        assert got == {"direct": 2, "indirect": 1, "unknown": 1}

    def test_duplicate_article_rows_are_counted_once(self, store: SituationStore) -> None:
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _evidence(store, "dup", "PL", created=_NOW_ISO)
        with store._repo._connect() as conn:  # noqa: SLF001
            rid = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
            conn.execute(
                "INSERT INTO articles (run_id, article_id, title, url, status, created_at,"
                " victim_country_iso) VALUES (?,?,?,?, 'posted', ?, ?)",
                (rid, "dup", "t", "https://x/dup", _NOW_ISO, "PL"),
            )
            got = count_proximity(conn, _SID, since_iso="2026-09-22T00:00:00+00:00")
        assert got == {"direct": 0, "indirect": 1, "unknown": 0}


class TestPostureCard:
    def test_card_exposes_proximity_when_enabled(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.ui.services.standing_posture import build_standing_posture

        monkeypatch.delenv("STANDING_EVIDENCE_PROXIMITY", raising=False)
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _evidence(store, "a1", "JP", created=_NOW_ISO)
        _evidence(store, "a2", "PL", created=_NOW_ISO)
        card = build_standing_posture(db_path=store._repo.db_path, now=_NOW)[0]  # noqa: SLF001
        assert card["evidence_proximity_30d"] == {"direct": 1, "indirect": 1, "unknown": 0}

    def test_card_omits_proximity_when_flag_is_off(
        self, store: SituationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.ui.services.standing_posture import build_standing_posture

        monkeypatch.setenv("STANDING_EVIDENCE_PROXIMITY", "0")
        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        card = build_standing_posture(db_path=store._repo.db_path, now=_NOW)[0]  # noqa: SLF001
        assert card["evidence_proximity_30d"] is None


class TestFillRateRegistration:
    def test_metric_is_registered(self) -> None:
        from src.ui.services.fill_rate_audit import METRICS

        metric = next(m for m in METRICS if m.key == "standing_evidence_victim_country")
        assert "victim_country_iso" in metric.condition
        assert metric.population is not None and "situation_evidence" in metric.population


class TestProximityByQuestion:
    def test_groups_rows_per_question(self) -> None:
        from src.assessment.standing_proximity import proximity_by_question

        rows = [
            ("s1", "問い1", "a", "JP"),
            ("s1", "問い1", "b", "PL"),
            ("s2", "問い2", "c", None),
        ]
        assert proximity_by_question(rows) == [
            ("s1", "問い1", {"direct": 1, "indirect": 1, "unknown": 0}),
            ("s2", "問い2", {"direct": 0, "indirect": 0, "unknown": 1}),
        ]

    def test_window_sql_runs_and_uses_assessed_evidence_only(self, store: SituationStore) -> None:
        from src.assessment.standing_proximity import window_rows_sql

        ensure_standing_situations(store=store, now_iso=_NOW_ISO)
        _evidence(store, "a1", "JP", created=_NOW_ISO)
        _evidence(store, "a2", "PL", created=_NOW_ISO)
        with store._repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE situation_evidence SET assessed_at=? WHERE article_id='a1'", (_NOW_ISO,)
            )
            rows = conn.execute(window_rows_sql(), ("2026-09-22T00:00:00+00:00",)).fetchall()
        assert [(r[0], r[2], r[3]) for r in rows] == [(_SID, "a1", "JP")]
