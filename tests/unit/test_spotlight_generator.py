"""src.spotlight.generator の period alignment / event 参照解決のテスト。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.assessment.context import AssessmentContext
from src.assessment.situation_store import SituationStore
from src.pir.models import Pir, StrongSignals
from src.spotlight.generator import (
    _build_prompt,
    _format_candidates,
    _LLMKeyEvent,
    _resolve_event_match,
    _resolve_period,
)
from src.spotlight.models import KeyEvent, SpotlightRecord
from src.storage.run_history import RunHistoryRepository

_JST = ZoneInfo("Asia/Tokyo")


class TestResolvePeriodAlignment:
    """period_start を JST 暦境界にアラインする (再生成 UPSERT 安定化)。"""

    def test_weekly_aligns_to_monday_jst(self) -> None:
        # 案2: 2026-06-05 は金曜。完結した前週 = 前週月曜 2026-05-25 〜 当週月曜 2026-06-01。
        now = datetime(2026, 6, 5, 21, 30, tzinfo=_JST).astimezone(UTC)
        start, end, label = _resolve_period("weekly", now=now)
        start_jst = start.astimezone(_JST)
        assert start_jst.weekday() == 0  # Monday
        assert (start_jst.hour, start_jst.minute, start_jst.second) == (0, 0, 0)
        assert start_jst.strftime("%Y-%m-%d") == "2026-05-25"  # 前週月曜
        assert end.astimezone(_JST).strftime("%Y-%m-%d") == "2026-06-01"  # 当週月曜
        assert (end - start).days == 7

    def test_weekly_same_week_runs_share_period_start(self) -> None:
        # 同一週の別時刻 2 回 → period_start 一致 (UPSERT で同一行)
        mon = datetime(2026, 6, 1, 9, 0, tzinfo=_JST).astimezone(UTC)
        fri = datetime(2026, 6, 5, 21, 0, tzinfo=_JST).astimezone(UTC)
        s1, _, _ = _resolve_period("weekly", now=mon)
        s2, _, _ = _resolve_period("weekly", now=fri)
        assert s1 == s2

    def test_daily_aligns_to_midnight_jst(self) -> None:
        now = datetime(2026, 6, 5, 21, 30, tzinfo=_JST).astimezone(UTC)
        start, end, _ = _resolve_period("daily", now=now)
        start_jst = start.astimezone(_JST)
        assert (start_jst.hour, start_jst.minute) == (0, 0)
        assert start_jst.strftime("%Y-%m-%d") == "2026-06-05"
        assert (end - start).days == 1

    def test_monthly_aligns_to_first_of_month(self) -> None:
        # 案2: 完結した前月。2026-06-05 実行 → 前月 2026-05-01 〜 当月 2026-06-01。
        now = datetime(2026, 6, 5, 21, 30, tzinfo=_JST).astimezone(UTC)
        start, end, _ = _resolve_period("monthly", now=now)
        start_jst = start.astimezone(_JST)
        assert start_jst.day == 1
        assert start_jst.strftime("%Y-%m-%d") == "2026-05-01"  # 前月 1 日
        assert end.astimezone(_JST).strftime("%Y-%m-%d") == "2026-06-01"  # 当月 1 日


@dataclass
class _FakeMatch:
    article_id: str
    title: str = "t"
    url: str = "u"
    feed_title: str = "f"
    importance: str = "high"
    created_at: str = "2026-06-05T00:00:00+00:00"


class TestResolveEventMatch:
    """key_event 参照解決 (index 優先 + article_id suffix fallback)。"""

    def _candidates(self) -> list[_FakeMatch]:
        return [
            _FakeMatch("rss:https://gbhackers.com/?p=1"),
            _FakeMatch("rss:https://cepa.org/?p=49575"),
            _FakeMatch("seculligence-com:abc"),
        ]

    def _index(self, cands: list[_FakeMatch]) -> dict[str, _FakeMatch]:
        return {m.article_id: m for m in cands}

    def test_index_reference_resolves(self) -> None:
        cands = self._candidates()
        ev = _LLMKeyEvent(index=2)
        m = _resolve_event_match(ev, cands, self._index(cands))
        assert m is not None and m.article_id == "rss:https://cepa.org/?p=49575"

    def test_article_id_exact_resolves(self) -> None:
        cands = self._candidates()
        ev = _LLMKeyEvent(article_id="seculligence-com:abc")
        m = _resolve_event_match(ev, cands, self._index(cands))
        assert m is not None and m.article_id == "seculligence-com:abc"

    def test_article_id_suffix_fallback_resolves(self) -> None:
        # LLM が rss:https:// prefix を欠落させたケース
        cands = self._candidates()
        ev = _LLMKeyEvent(article_id="cepa.org/?p=49575")
        m = _resolve_event_match(ev, cands, self._index(cands))
        assert m is not None and m.article_id == "rss:https://cepa.org/?p=49575"

    def test_out_of_range_index_falls_through(self) -> None:
        cands = self._candidates()
        ev = _LLMKeyEvent(index=99)
        assert _resolve_event_match(ev, cands, self._index(cands)) is None

    def test_unknown_reference_returns_none(self) -> None:
        cands = self._candidates()
        ev = _LLMKeyEvent(article_id="totally-unknown-xyz")
        assert _resolve_event_match(ev, cands, self._index(cands)) is None


_VALID_TIERS = {"official", "research", "news", "social", "state_media", "unknown"}


class TestFormatCandidatesReliability:
    """段2: 候補に source 信頼度 tier を事前計算して載せる。"""

    def test_reliability_present_and_valid(self) -> None:
        cands = _format_candidates([_FakeMatch("a1", feed_title="CISA")], 5, {})
        assert len(cands) == 1
        assert "reliability" in cands[0]
        assert cands[0]["reliability"] in _VALID_TIERS


def _pir() -> Pir:
    return Pir(
        id="pir_x",
        title="中国系 APT 動向",
        description="Volt Typhoon の通信網標的を監視",
        strong_signals=StrongSignals(actors=["volt_typhoon"]),
    )


class TestBuildPromptContext:
    """段2: 横断 assessment + 前期継続性 + reliability が prompt に注入される。"""

    def _assessment(self) -> AssessmentContext:
        return AssessmentContext(
            nation_correlation=[
                {
                    "iso": "CN",
                    "label": "中国",
                    "role": "adversary",
                    "attributed_cyber": 5,
                    "cyber_mention": 3,
                    "cyber_target": 1,
                    "geopol": 10,
                    "top_actors": ["Volt Typhoon"],
                    "cyber_intents": [],
                    "geopol_intents": [],
                }
            ],
            forecast_indicators=[
                {
                    "scope": "actor",
                    "target": "volt_typhoon",
                    "direction": "up",
                    "z": 2.5,
                    "count": 7,
                }
            ],
            freshness={"dated": 20, "fresh": 5, "retrospective": 14, "retrospective_pct": 70},
        )

    def test_renders_all_context_blocks(self) -> None:
        cands = _format_candidates([_FakeMatch("a1", feed_title="CISA")], 5, {})
        prev = {
            "period_label": "2026-06-15",
            "headline": "前期は通信網を標的化",
            "outlook": "来週は報復に警戒",
            "key_event_titles": ["事案A", "事案B"],
        }
        out = _build_prompt(
            _pir(),
            cands,
            "2026-06-22 週",
            assessment=self._assessment(),
            previous_spotlight=prev,
        )
        assert "信頼度=" in out  # 候補に reliability tier
        assert "前期は通信網を標的化" in out  # 前期 headline
        assert "事案A" in out  # 前期 key events
        assert "中国" in out  # 国家相関 label
        assert "volt_typhoon" in out  # forecast target
        assert "70%" in out  # freshness retrospective_pct

    def test_legacy_when_no_context(self) -> None:
        # assessment / previous なし = legacy 挙動 (context block は出ない)
        cands = _format_candidates([_FakeMatch("a1")], 5, {})
        out = _build_prompt(_pir(), cands, "2026-06-22 週")
        assert "横断情勢 context" not in out
        assert "前期" not in out


class TestGetPreviousSpotlight:
    """段2: 前期 spotlight 取得 (period_start < before、当期自身を誤認しない)。"""

    def _rec(self, pir_id: str, start: datetime, headline: str) -> SpotlightRecord:
        return SpotlightRecord(
            pir_id=pir_id,
            pir_title="t",
            period_type="weekly",
            period_start=start,
            period_end=start + timedelta(days=7),
            headline=headline,
            outlook="o",
            key_events=[KeyEvent(article_id="x", title="ev")],
        )

    def test_returns_most_recent_before(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "sp.db")
        now = datetime.now(UTC).replace(microsecond=0)
        prev_start = now - timedelta(days=7)
        repo.upsert_pir_spotlight(self._rec("pir_x", prev_start, "前期"))
        repo.upsert_pir_spotlight(self._rec("pir_x", now, "当期"))
        # before=now → 当期 (period_start==now) を除外し前期を返す
        got = repo.get_previous_spotlight(pir_id="pir_x", period_type="weekly", before=now)
        assert got is not None
        assert got.headline == "前期"

    def test_returns_none_when_nothing_earlier(self, tmp_path: Path) -> None:
        repo = RunHistoryRepository(db_path=tmp_path / "sp2.db")
        now = datetime.now(UTC).replace(microsecond=0)
        repo.upsert_pir_spotlight(self._rec("pir_x", now, "当期のみ"))
        got = repo.get_previous_spotlight(pir_id="pir_x", period_type="weekly", before=now)
        assert got is None


class TestLedgerRollbackGuard:
    """監査 2026-07-05: SYNTHESIS_STATE rollback 時に stale 台帳を canonical 注入しない。"""

    def test_ledger_context_empty_when_state_off(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.spotlight.generator import _build_ledger_context

        monkeypatch.setenv("SYNTHESIS_STATE", "0")
        assert _build_ledger_context("pir_china_apt") == []

    def test_ledger_context_empty_when_state_shadow(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        from src.spotlight.generator import _build_ledger_context

        monkeypatch.setenv("SYNTHESIS_STATE", "shadow")
        assert _build_ledger_context("pir_china_apt") == []


class TestLedgerContextPeriodSymmetry:
    """監査 backlog 2026-07-05: Spotlight 台帳注入の射影非対称 (dormant/期間外 delta) の解消。"""

    @staticmethod
    def _store(tmp_path: Path) -> tuple[SituationStore, Callable[..., str]]:
        from src.assessment.situation_store import RevisionRow

        store = SituationStore(db_path=tmp_path / "ledger_ctx.db")

        def add(sid_title: str, *, status: str, delta: str, created_iso: str) -> str:
            row = store.open_situation(
                title=sid_title,
                domain="cyber_incident",
                anchors=frozenset(),
                pir_ids=("pir_china_apt",),
                now_iso=created_iso,
            )
            store.add_revision(
                RevisionRow(
                    situation_id=row.situation_id,
                    rev=0,
                    claim=sid_title,
                    claim_type="ongoing_activity",
                    leading_hypothesis="organized_state_op",
                    confidence="moderate",
                    confidence_basis="test",
                    hypotheses_json="[]",
                    assumptions_json="[]",
                    missing_json="[]",
                    indicators_json="[]",
                    implication="",
                    delta_type=delta,  # type: ignore[arg-type]
                    delta_note="変化の注記",
                    created_at=created_iso,
                )
            )
            if status != "active":
                store.set_status(row.situation_id, status)  # type: ignore[arg-type]
            return row.situation_id

        return store, add

    def test_dormant_excluded_and_out_of_period_delta_neutralized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.spotlight.generator import _build_ledger_context

        monkeypatch.setenv("SYNTHESIS_STATE", "1")
        store, add = self._store(tmp_path)
        now = datetime.now(UTC)
        start = now - timedelta(days=7)
        # 期間内に動いた active / 期間前に動いた active / dormant の 3 件
        add(
            "期間内に強化された情勢",
            status="active",
            delta="strengthened",
            created_iso=now.isoformat(),
        )
        add(
            "期間前から不変の情勢",
            status="active",
            delta="strengthened",
            created_iso=(now - timedelta(days=30)).isoformat(),
        )
        add(
            "休眠中の情勢",
            status="dormant",
            delta="opened",
            created_iso=(now - timedelta(days=40)).isoformat(),
        )

        got = _build_ledger_context(
            "pir_china_apt",
            period_start_iso=start.isoformat(),
            period_end_iso=now.isoformat(),
            store=store,
        )

        claims = {d["claim"]: d for d in got}
        assert "休眠中の情勢" not in claims  # dormant 除外
        assert claims["期間内に強化された情勢"]["delta_type"] == "strengthened"
        # 期間外 delta は standing context に中立化 (当期の変化として語らせない)
        assert claims["期間前から不変の情勢"]["delta_type"] == "no_change"
        assert claims["期間前から不変の情勢"]["delta_note"] == ""

    def test_future_revision_not_read_for_backfill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.assessment.situation_store import RevisionRow
        from src.spotlight.generator import _build_ledger_context

        monkeypatch.setenv("SYNTHESIS_STATE", "1")
        store, add = self._store(tmp_path)
        now = datetime.now(UTC)
        sid = add(
            "過去期間の情勢",
            status="active",
            delta="opened",
            created_iso=(now - timedelta(days=10)).isoformat(),
        )
        # 期間後の revision (backfill 再生成で読んではならない)
        store.add_revision(
            RevisionRow(
                situation_id=sid,
                rev=0,
                claim="過去期間の情勢",
                claim_type="ongoing_activity",
                leading_hypothesis="organized_state_op",
                confidence="high",
                confidence_basis="test",
                hypotheses_json="[]",
                assumptions_json="[]",
                missing_json="[]",
                indicators_json="[]",
                implication="",
                delta_type="strengthened",
                delta_note="未来の変化",
                created_at=now.isoformat(),
            )
        )

        got = _build_ledger_context(
            "pir_china_apt",
            period_start_iso=(now - timedelta(days=14)).isoformat(),
            period_end_iso=(now - timedelta(days=7)).isoformat(),
            store=store,
        )

        assert len(got) == 1
        assert got[0]["confidence"] == "moderate"  # 期間内最新 (rev=1) を読む
        assert got[0]["delta_type"] == "opened"
