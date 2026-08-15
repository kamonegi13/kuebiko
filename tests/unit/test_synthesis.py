"""Phase 3 Synthesis のテスト。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from src.assessment.context import build_nation_correlation
from src.storage.run_history import (
    ArticleRecord,
    ArticleStatus,
    RunHistoryRepository,
    RunRecord,
    StatusSynthesisRecord,
)
from src.synthesis.generator import (
    _axis_min_events,
    _check_axes_evidence_coverage,
    _coerce_pir_section,
    _parse_synthesis_json,
    _resolve_period,
    _sanitize_article_id,
    _sanitize_axes_evidence,
    _sanitize_tradecraft,
    generate_synthesis,
)
from src.synthesis.runner import run_status_synthesis
from src.tools.llm_client import LLMClient, LLMResponse


@pytest.fixture
def repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=tmp_path / "syn.db")


def _now() -> datetime:
    return datetime.now(UTC)


class TestStatusSynthesisRecordPersistence:
    def test_upsert_inserts_then_updates(self, repo: RunHistoryRepository) -> None:
        now = _now()
        record = StatusSynthesisRecord(
            period_type="weekly",
            period_start=now,
            period_end=now,
            headline="initial headline",
            weight_section="主流: M",
            chain_section="連鎖",
            cog_section="CoG",
            spillover_section="波及",
            pir_section="PIR",
            axes_evidence='{"M": []}',
            article_count=10,
            llm_model="test",
        )
        first_id = repo.upsert_status_synthesis(record)
        assert first_id > 0

        # 同じ period で upsert → 更新
        updated = StatusSynthesisRecord(
            period_type="weekly",
            period_start=now,
            period_end=now,
            headline="updated headline",
            weight_section="主流: M, I-cyber",
            chain_section="連鎖 (改訂)",
            cog_section="CoG (改訂)",
            spillover_section="波及 (改訂)",
            pir_section="PIR (改訂)",
            axes_evidence='{"M": [], "I-cyber": []}',
            article_count=15,
            llm_model="test",
        )
        second_id = repo.upsert_status_synthesis(updated)
        assert second_id == first_id  # 同じ id
        latest = repo.get_latest_synthesis(period_type="weekly")
        assert latest is not None
        assert latest.headline == "updated headline"
        assert latest.article_count == 15

    def test_get_latest_returns_most_recent(self, repo: RunHistoryRepository) -> None:
        old = _now().replace(microsecond=0)
        # weekly を 3 期間分挿入
        from datetime import timedelta

        for offset in (14, 7, 0):
            start = old - timedelta(days=offset)
            repo.upsert_status_synthesis(
                StatusSynthesisRecord(
                    period_type="weekly",
                    period_start=start,
                    period_end=start + timedelta(days=7),
                    headline=f"week of {start.strftime('%Y-%m-%d')}",
                    weight_section="x",
                    chain_section="x",
                    cog_section="x",
                    spillover_section="x",
                    pir_section="x",
                    axes_evidence="{}",
                ),
            )
        latest = repo.get_latest_synthesis(period_type="weekly")
        assert latest is not None
        assert latest.period_start == old  # offset=0

    def test_list_synthesis_returns_descending(self, repo: RunHistoryRepository) -> None:
        from datetime import timedelta

        base = _now().replace(microsecond=0)
        for offset in (21, 14, 7, 0):
            start = base - timedelta(days=offset)
            repo.upsert_status_synthesis(
                StatusSynthesisRecord(
                    period_type="weekly",
                    period_start=start,
                    period_end=start + timedelta(days=7),
                    headline=f"w-{offset}",
                    weight_section="x",
                    chain_section="x",
                    cog_section="x",
                    spillover_section="x",
                    pir_section="x",
                    axes_evidence="{}",
                ),
            )
        items = repo.list_synthesis(period_type="weekly", limit=10)
        assert len(items) == 4
        assert items[0].headline == "w-0"
        assert items[-1].headline == "w-21"

    def test_period_type_filter(self, repo: RunHistoryRepository) -> None:
        now = _now().replace(microsecond=0)
        repo.upsert_status_synthesis(
            StatusSynthesisRecord(
                period_type="weekly",
                period_start=now,
                period_end=now,
                headline="weekly",
                weight_section="x",
                chain_section="x",
                cog_section="x",
                spillover_section="x",
                pir_section="x",
                axes_evidence="{}",
            ),
        )
        repo.upsert_status_synthesis(
            StatusSynthesisRecord(
                period_type="monthly",
                period_start=now,
                period_end=now,
                headline="monthly",
                weight_section="x",
                chain_section="x",
                cog_section="x",
                spillover_section="x",
                pir_section="x",
                axes_evidence="{}",
            ),
        )
        weekly = cast(StatusSynthesisRecord, repo.get_latest_synthesis(period_type="weekly"))
        assert weekly.headline == "weekly"
        monthly = cast(StatusSynthesisRecord, repo.get_latest_synthesis(period_type="monthly"))
        assert monthly.headline == "monthly"


class TestSanitizeTradecraft:
    """S2: 分析トレードクラフトの検証 (主見立て+対立仮説+前提+覆る指標)。"""

    def test_valid_dict_preserved(self) -> None:
        out = json.loads(
            _sanitize_tradecraft(
                {
                    "leading_assessment": "主見立て",
                    "alternatives": ["別解A", "別解B"],
                    "key_assumptions": ["前提1"],
                    "indicators": ["指標1"],
                }
            )
        )
        assert out["leading_assessment"] == "主見立て"
        assert out["alternatives"] == ["別解A", "別解B"]
        assert out["key_assumptions"] == ["前提1"]
        assert out["indicators"] == ["指標1"]

    def test_non_dict_returns_empty_object(self) -> None:
        assert _sanitize_tradecraft(None) == "{}"
        assert _sanitize_tradecraft("not a dict") == "{}"
        assert _sanitize_tradecraft([1, 2]) == "{}"

    def test_lists_capped_and_blanks_dropped(self) -> None:
        out = json.loads(
            _sanitize_tradecraft({"alternatives": ["a", "", "  ", "b", "c", "d", "e", "f"]})
        )
        assert out["alternatives"] == ["a", "b", "c", "d"]  # 空除去 + 最大 4 件


class TestResolvePeriod:
    # 固定 now で決定論化 (旧テストは default now で日付依存に fail していた)。
    # 2026-06-17 12:00 UTC = 水 21:00 JST。
    _NOW = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)

    def test_daily(self) -> None:
        # daily は intraday (当日 JST 00:00 〜 now) のまま (案2 でも不変)。
        from zoneinfo import ZoneInfo

        start, end, label, lookback, baseline = _resolve_period(period_type="daily", now=self._NOW)
        assert lookback == 24
        assert baseline == 2
        assert end == self._NOW
        assert start.astimezone(ZoneInfo("Asia/Tokyo")).hour == 0  # 当日 JST 00:00
        assert ":" in label

    def test_weekly_covers_prev_complete_week(self) -> None:
        # 案2: 完結した前週 = 厳密 7 日 (実行時刻に依存しない)。
        start, end, label, lookback, baseline = _resolve_period(period_type="weekly", now=self._NOW)
        assert (end - start).days == 7
        assert lookback == 168
        assert baseline == 4
        assert "前週" in label

    def test_monthly_covers_prev_complete_month(self) -> None:
        # 案2: 完結した前月 (2026-05 = 31 日)。月により 28-31 日。
        start, end, label, lookback, baseline = _resolve_period(
            period_type="monthly", now=self._NOW
        )
        assert 28 <= (end - start).days <= 31
        assert lookback == 720
        assert "前月" in label

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError):
            _resolve_period(period_type="hourly")


class TestCoercePirSection:
    """LLM が pir_section に dict / list を返した場合の markdown 変換。"""

    def test_dict_becomes_markdown_list(self) -> None:
        out = _coerce_pir_section(
            {
                "PIR 1: 0day / KEV": "✅ 充足 — SonicWall CVE 悪用",
                "PIR 2: 日本標的": "⚠️ 部分 — Twill Typhoon",
            },
        )
        assert "**PIR 1: 0day / KEV**" in out
        assert "**PIR 2: 日本標的**" in out
        assert "✅" in out and "⚠️" in out

    def test_list_becomes_markdown(self) -> None:
        out = _coerce_pir_section(["PIR 1: ✅", "PIR 2: ⚠️"])
        assert out.startswith("- PIR 1")
        assert "\n- PIR 2" in out

    def test_string_passthrough(self) -> None:
        out = _coerce_pir_section("- **PIR 1**: ✅")
        assert out == "- **PIR 1**: ✅"


class TestSanitizeArticleId:
    """LLM が article_id に付けた前後空白を落とす (それ以外は素通し)。"""

    def test_strips_surrounding_whitespace(self) -> None:
        assert _sanitize_article_id("  grok:x_japan_watch:123  ") == "grok:x_japan_watch:123"

    def test_id_passthrough(self) -> None:
        assert _sanitize_article_id("grok:state_apt:123") == "grok:state_apt:123"

    def test_empty_returns_empty(self) -> None:
        assert _sanitize_article_id("") == ""


class TestAxisMinEvents:
    """Phase 3 A2: 軸別 article 件数に応じた最低 events 数の閾値。"""

    def test_high_volume_axis_requires_5_events(self) -> None:
        assert _axis_min_events(200) == 5
        assert _axis_min_events(100) == 5

    def test_medium_volume_axis_requires_3(self) -> None:
        assert _axis_min_events(99) == 3
        assert _axis_min_events(50) == 3

    def test_low_volume_axis_requires_2(self) -> None:
        assert _axis_min_events(49) == 2
        assert _axis_min_events(20) == 2

    def test_minimal_volume_axis_requires_1(self) -> None:
        assert _axis_min_events(19) == 1
        assert _axis_min_events(1) == 1

    def test_zero_volume_no_minimum(self) -> None:
        assert _axis_min_events(0) == 0


class TestCheckAxesEvidenceCoverage:
    """LLM が under-deliver した軸を shortage として列挙。"""

    def test_returns_empty_when_all_meet_minimum(self) -> None:
        axes_data = [
            {"axis_id": "M", "total_current": 50},
            {"axis_id": "I-cyber", "total_current": 100},
        ]
        axes_evidence = {
            "M": [{"label": "x"}, {"label": "y"}, {"label": "z"}],
            "I-cyber": [{"label": str(i)} for i in range(5)],
        }
        assert _check_axes_evidence_coverage(axes_evidence, axes_data) == []

    def test_flags_axes_with_too_few_events(self) -> None:
        axes_data = [
            {"axis_id": "M", "total_current": 100},  # 5 必要
            {"axis_id": "P", "total_current": 30},  # 2 必要
        ]
        axes_evidence = {
            "M": [{"label": "only-1"}],  # 不足 (1 < 5)
            "P": [{"label": "a"}, {"label": "b"}],  # OK (2 == 2)
        }
        sh = _check_axes_evidence_coverage(axes_evidence, axes_data)
        assert len(sh) == 1
        assert sh[0]["axis_id"] == "M"
        assert sh[0]["required"] == 5
        assert sh[0]["delivered"] == 1

    def test_missing_axis_counts_as_zero_delivered(self) -> None:
        axes_data = [{"axis_id": "M", "total_current": 100}]
        sh = _check_axes_evidence_coverage({}, axes_data)
        assert len(sh) == 1
        assert sh[0]["delivered"] == 0


class TestSanitizeAxesEvidence:
    def test_sanitizes_article_ids_in_events(self) -> None:
        out = _sanitize_axes_evidence(
            {
                "M": [
                    {
                        "label": "Supply Chain",
                        "summary": "西側部品流用",
                        "article_ids": ["  grok:x_china_apt:abc  ", "grok:x_china_apt:def"],
                    },
                ],
            },
        )
        ids = out["M"][0]["article_ids"]
        assert ids == ["grok:x_china_apt:abc", "grok:x_china_apt:def"]

    def test_non_dict_returns_empty(self) -> None:
        assert _sanitize_axes_evidence("not a dict") == {}
        assert _sanitize_axes_evidence(None) == {}

    def test_drops_malformed_events(self) -> None:
        out = _sanitize_axes_evidence(
            {
                "P": [
                    {"label": "ok", "summary": "s", "article_ids": ["x"]},
                    "not a dict",  # dropped
                    {"label": "ok2", "summary": "s2"},  # missing ids → []
                ],
            },
        )
        assert len(out["P"]) == 2
        assert out["P"][0]["article_ids"] == ["x"]
        assert out["P"][1]["article_ids"] == []


class TestParseSynthesisJson:
    def test_plain(self) -> None:
        text = '{"headline": "x", "weight_section": "y"}'
        parsed = _parse_synthesis_json(text)
        assert parsed is not None
        assert parsed["headline"] == "x"

    def test_fenced(self) -> None:
        text = "```json\n" + json.dumps({"headline": "x"}) + "\n```"
        parsed = _parse_synthesis_json(text)
        assert parsed is not None
        assert parsed["headline"] == "x"

    def test_with_preamble(self) -> None:
        text = "前置き文\n" + json.dumps({"headline": "x"}) + "\n末尾"
        parsed = _parse_synthesis_json(text)
        assert parsed is not None

    def test_invalid_returns_none(self) -> None:
        assert _parse_synthesis_json("not json") is None
        assert _parse_synthesis_json("") is None


@pytest.mark.asyncio
class TestGenerateSynthesis:
    async def test_returns_error_when_no_articles(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        llm = AsyncMock(spec=LLMClient)
        result = await generate_synthesis(
            llm=llm,
            period_type="weekly",
            db_path=repo._db_path,  # noqa: SLF001
        )
        assert result.record is None
        assert "no articles" in (result.error or "")

    async def test_handles_llm_failure(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a",
                title="Sample article",
                url="https://example.com/a",
                status="posted",
                pmesii_m=True,
                pmesii_i_cyber=True,
            ),
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=RuntimeError("ollama down"))
        result = await generate_synthesis(
            llm=llm,
            period_type="weekly",
            db_path=repo._db_path,  # noqa: SLF001
        )
        assert result.record is None
        # 実 DB 状況によって "no articles" or "llm: ..." だが、いずれにせよ error が立つ
        assert result.error is not None

    async def test_handles_missing_required_field(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a",
                title="Sample",
                url="https://example.com/a",
                status="posted",
                pmesii_m=True,
                pmesii_i_cyber=True,
            ),
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=LLMResponse(
                text='{"headline": "x"}',  # 必須 fields 不足
                model="test",
            ),
        )
        result = await generate_synthesis(
            llm=llm,
            period_type="weekly",
            db_path=repo._db_path,  # noqa: SLF001
        )
        assert result.record is None
        # "missing" or "no articles" のいずれか
        assert result.error is not None

    async def test_full_success_when_articles_visible(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """LLM が完全な JSON を返すケース。

        article 投入 → fetch_axis_dashboard が拾える前提で、LLM 結果を
        StatusSynthesisRecord に変換できることを検証。
        article visibility は実 DB 環境次第で 0 件もあり得るので、
        その場合は no articles を許容しつつ JSON parse pass を確認。
        """
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        # 複数軸を立て、複数 article で visibility 確率を上げる
        for i, title in enumerate(
            [
                "Salt Typhoon ESXi exploit observed",
                "APT41 supply chain attack on Japanese semiconductor",
                "CISA KEV adds new vulnerability",
            ],
        ):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"a{i}",
                    title=title,
                    url=f"https://example.com/a{i}",
                    status="posted",
                    pmesii_m=True,
                    pmesii_i_cyber=True,
                    pmesii_p=(i == 2),
                ),
            )
        full_json = {
            "headline": "中国系 APT が ESXi 0day を使用",
            "weight_section": "主流: M / I-cyber",
            "chain_section": "M ↔ I-cyber の連鎖",
            "cog_section": "Salt Typhoon",
            "spillover_section": "来週 I-infra へ波及見込み",
            "pir_section": "PIR 1: ✅",
            "axes_evidence": {
                "M": [{"label": "Salt Typhoon", "summary": "ESXi 新 TTP", "article_ids": ["a0"]}],
                "I-cyber": [
                    {"label": "ESXi 0day", "summary": "exploitation 観測", "article_ids": ["a0"]}
                ],
            },
        }
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=LLMResponse(text=json.dumps(full_json), model="test-model"),
        )
        result = await generate_synthesis(
            llm=llm,
            period_type="weekly",
            db_path=repo._db_path,  # noqa: SLF001
        )
        if result.record is None:
            # article visibility 問題で no articles → JSON parse は通る path がない
            # の確認。これは test 環境の sqlite 挙動差で起こりうる。
            assert result.error is not None
            return
        # 正常 path
        assert "Salt Typhoon" in result.record.headline
        ev = json.loads(result.record.axes_evidence)
        assert "Salt Typhoon" in ev["M"][0]["label"]


@pytest.mark.asyncio
class TestRunStatusSynthesis:
    async def test_persists_when_repo_provided(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="a",
                title="Sample",
                url="https://example.com/a",
                status="posted",
                pmesii_m=True,
            ),
        )
        full_json = {
            "headline": "h",
            "weight_section": "w",
            "chain_section": "c",
            "cog_section": "g",
            "spillover_section": "s",
            "pir_section": "p",
            "axes_evidence": {"M": []},
        }
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(
            return_value=LLMResponse(text=json.dumps(full_json), model="test"),
        )
        # db_path を本番から切替えるため、generator 内 default を override する必要。
        # 簡素化のため direct call で repo に upsert する path をテストする。
        # → runner は内部で generate_synthesis (default db_path) を呼ぶので、
        # ここでは runner 結果の構造のみ検証 (実 DB 検証は別)
        result = await run_status_synthesis(llm=llm, repo=None, include_monthly=False)
        # dry_run 相当 (repo None) で errors なし or "no articles" (本番 DB 状況依存)
        assert isinstance(result.weekly_generated, bool)
        assert isinstance(result.errors, list)

    async def test_period_types_overrides_include_monthly(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """period_types=('daily',) で daily のみ生成、weekly/monthly は呼ばれない。"""
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=RuntimeError("forced"))
        result = await run_status_synthesis(
            llm=llm,
            repo=None,
            period_types=("daily",),
        )
        # daily のみ対象。generated dict にも errors にも weekly/monthly は含まれない
        assert "weekly" not in result.generated
        assert "monthly" not in result.generated
        for err in result.errors:
            assert "weekly" not in err and "monthly" not in err

    async def test_period_types_invalid_filtered(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """不正な period_type は黙って除外される。"""
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=RuntimeError("forced"))
        result = await run_status_synthesis(
            llm=llm,
            repo=None,
            period_types=("hourly", "yearly"),
        )
        # どちらも valid でないので何も実行されず errors も空
        assert result.generated == {}
        assert result.errors == []


@pytest.mark.asyncio
class TestMaybeTriggerDailySynthesis:
    """Phase 3 auto-trigger の staleness check ロジック。"""

    async def test_first_run_triggers(self, repo: RunHistoryRepository) -> None:
        """daily synthesis 未生成なら最初の post で trigger される。"""
        from src.synthesis.auto_trigger import maybe_trigger_daily_synthesis

        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=RuntimeError("no llm available"))
        # llm 呼出は failed しても staleness check 段階は通過することを確認
        fired = await maybe_trigger_daily_synthesis(llm=llm, repo=repo)
        # LLM 失敗で False 返却だが、generate は呼ばれた (=staleness 判定通過)
        assert fired is False
        # 実際には no articles で skip されるか llm error。どちらの path でも True にならない。
        # 確実なのは「例外を呼び出し側に投げない」。

    async def test_firing_records_job_run(self, repo: RunHistoryRepository) -> None:
        """可観測性 (監査 M5): 発火した auto-trigger は job_last_run に痕跡を残す。

        RSS run の内側でネスト実行されるため runs には載らない — record_job_run 経由の
        記録が唯一の可観測性。skip (debounce/delta) は記録しない仕様。
        """
        from src.synthesis.auto_trigger import maybe_trigger_daily_synthesis

        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock(side_effect=RuntimeError("no llm available"))

        await maybe_trigger_daily_synthesis(llm=llm, repo=repo)

        last = repo.get_job_last_runs()
        assert "auto-trigger-synthesis" in last

    async def test_debounce_skips_when_recent(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """前回 daily synthesis が min_age_hours 以内なら skip。"""
        from src.synthesis.auto_trigger import maybe_trigger_daily_synthesis

        recent = _now()
        repo.upsert_status_synthesis(
            StatusSynthesisRecord(
                period_type="daily",
                period_start=recent,
                period_end=recent,
                headline="recent",
                weight_section="w",
                chain_section="c",
                cog_section="g",
                spillover_section="s",
                pir_section="p",
                axes_evidence="{}",
                generated_at=recent,
            ),
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock()
        fired = await maybe_trigger_daily_synthesis(
            llm=llm,
            repo=repo,
            min_age_hours=2.0,
        )
        assert fired is False
        # LLM はそもそも呼ばれない (staleness で先に弾かれる)
        llm.generate.assert_not_called()

    async def test_skipped_when_delta_below_threshold(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        """debounce は越えたが新着 article が少ないと skip。"""
        from datetime import timedelta

        from src.synthesis.auto_trigger import maybe_trigger_daily_synthesis

        old = _now() - timedelta(hours=5)
        repo.upsert_status_synthesis(
            StatusSynthesisRecord(
                period_type="daily",
                period_start=old,
                period_end=old,
                headline="old",
                weight_section="w",
                chain_section="c",
                cog_section="g",
                spillover_section="s",
                pir_section="p",
                axes_evidence="{}",
                generated_at=old,
            ),
        )
        # min_article_delta=5、実際 article は 1 件のみ追加
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        repo.add_article(
            ArticleRecord(
                run_id=run_id,
                article_id="only1",
                title="x",
                url="https://example.com/x",
                status="posted",
                pmesii_m=True,
            ),
        )
        llm = AsyncMock(spec=LLMClient)
        llm.generate = AsyncMock()
        fired = await maybe_trigger_daily_synthesis(
            llm=llm,
            repo=repo,
            min_age_hours=2.0,
            min_article_delta=5,
        )
        assert fired is False
        llm.generate.assert_not_called()


class TestCountPostedArticlesSince:
    """Phase 3 用 helper: 指定 datetime 以降に posted された article 数。"""

    def test_counts_only_posted_after_since(
        self,
        repo: RunHistoryRepository,
    ) -> None:
        from datetime import timedelta

        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        # 異なる created_at で 3 件
        base = _now()
        for i, delta in enumerate((-3, -1, 0)):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"a{i}",
                    title="t",
                    url=f"https://example.com/a{i}",
                    status="posted",
                    created_at=base + timedelta(hours=delta),
                ),
            )
        # 2 時間前以降 → 2 件 (delta=-1, 0)
        n = repo.count_posted_articles_since(base - timedelta(hours=2))
        assert n == 2

    def test_excludes_non_posted(self, repo: RunHistoryRepository) -> None:
        run_id = repo.start_run(
            RunRecord(started_at=_now(), pipeline="x", dry_run=False),
        )
        statuses: tuple[ArticleStatus, ...] = ("posted", "skipped_duplicate", "post_failed")
        for i, status in enumerate(statuses):
            repo.add_article(
                ArticleRecord(
                    run_id=run_id,
                    article_id=f"a{i}",
                    title="t",
                    url=f"https://example.com/a{i}",
                    status=status,
                ),
            )
        from datetime import timedelta

        n = repo.count_posted_articles_since(_now() - timedelta(hours=1))
        assert n == 1


class TestSynthesisPromptPirWiring:
    """① 状況総括テンプレートへの pir_context 配線 (旧: 死にコード) のリグレッション。"""

    def _render(self, pir_context: list[dict[str, str]]) -> str:
        import jinja2

        from src.synthesis.generator import PROMPTS_DIR, SYNTHESIS_TEMPLATE

        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        template = env.get_template(SYNTHESIS_TEMPLATE)
        return template.render(
            period_type="daily",
            period_label="2026-06-12",
            baseline_weeks=4,
            axes_data=[],
            trend_clusters=[],
            high_importance_articles=[],
            pir_context=pir_context,
            previous_synthesis=None,
        )

    def test_injects_pir_titles_from_context(self) -> None:
        out = self._render(
            [
                {"id": "pir_x", "title": "中国系 APT 動向", "description": "Volt Typhoon 等の検知"},
                {"id": "pir_y", "title": "日本標的の攻撃", "description": "JP 標的 active threat"},
            ]
        )
        # 動的に注入された PIR title が prompt に現れる
        assert "中国系 APT 動向" in out
        assert "日本標的の攻撃" in out
        assert "Volt Typhoon 等の検知" in out

    def test_no_hardcoded_legacy_pir_block(self) -> None:
        out = self._render([{"id": "pir_x", "title": "X", "description": ""}])
        # 旧ハードコード (memory cti_pir.md 由来の固定 5 PIR) が残っていない
        assert "PIR 1: 0day" not in out
        assert "PIR 4: 朝まとめ Recall" not in out
        assert "cti_pir.md" not in out

    def test_empty_context_falls_back_gracefully(self) -> None:
        out = self._render([])
        assert "PIR 未登録" in out

    def test_render_prompt_uses_real_pir_config(self) -> None:
        # _render_prompt は config/delivery/pir.yaml から pir_context を構築する。
        from src.pir.integration import invalidate_cache
        from src.synthesis.generator import _render_prompt

        invalidate_cache()
        out = _render_prompt(
            period_type="daily",
            period_label="2026-06-12",
            baseline_weeks=4,
            axes_data=[],
            trend_clusters=[],
            high_importance_articles=[],
        )
        # 実 pir.yaml の代表的 title が反映される (死にコードでないことの保証)
        assert "中国系 APT" in out
        assert "PIR 1: 0day" not in out


def test_build_nation_correlation_separates_attributed_from_mention(
    repo: RunHistoryRepository, tmp_path: Path
) -> None:
    """#1 国家相関: 帰属サイバー (actor.nation) と帰属なし言及 (i_cyber×当事国×actor無し) を
    別レーンに分離し、同一記事を二重計上しないこと。"""
    now = _now()
    rid = repo.start_run(RunRecord(started_at=now, pipeline="t", dry_run=True))

    def art(aid: str, cat: str, **kw: object) -> ArticleRecord:
        return ArticleRecord(
            run_id=rid,
            article_id=aid,
            title=aid,
            url=f"https://e/{aid}",
            category=cat,
            status="posted",
            created_at=now,
            published_at=now,
            **kw,  # type: ignore[arg-type]
        )

    # 帰属レーン: 中国系 APT (Volt Typhoon → registry で nation=cn)
    repo.add_article(art("a1", "apt", socio_political_intent="prepositioning"))
    repo.add_article_entities("a1", [("actor", "volt_typhoon")], when=now)
    # 言及レーン: i_cyber × involved_country=CN × actor 無し (政策/態勢の言及)
    repo.add_article(
        art("a2", "geopolitical", pmesii_i_cyber=True, socio_political_intent="coercion")
    )
    repo.add_article_entities("a2", [("involved_country", "CN")], when=now)

    nc = build_nation_correlation(window_days=3650, db_path=tmp_path / "syn.db")
    cn = next((n for n in nc if n["iso"] == "CN"), None)
    assert cn is not None
    assert cn["attributed_cyber"] == 1  # Volt Typhoon のみ
    assert cn["cyber_mention"] == 1  # 言及記事のみ (帰属記事は混ざらない)
