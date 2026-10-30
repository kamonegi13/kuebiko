"""地政学の救済の判定 v2 の影の記録 (GEO_RESCUE_JUDGED_SHADOW、2026-10-31) のテスト。

triage の時点は写すだけ (LLM なし)・判定は保守チェーンの段で古い順に上限まで・本番の救済は不変・
テーブルの読み書き・集計・公開面に出ないことを固定する。Ollama・PG には触らない。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import src.pipeline.geo_rescue_shadow as gs
from src.pipeline.filters import _rescue_geopolitical
from src.pipeline.geo_rescue_judge import GeoJudgeResult
from src.pipeline.geo_rescue_shadow import (
    GeoRescueCandidate,
    JudgeContext,
    article_from_row,
    cue_types,
    input_sha256,
    is_geo_rescue_shadow_enabled,
    judge_pending,
    judge_row,
    pending_rows,
)
from src.scheduler.job_registry import default_jobs
from src.storage.repo_geo_rescue_shadow import (
    STATUS_ERROR,
    STATUS_EXPIRED,
    STATUS_OK,
    STATUS_PENDING,
    GeoRescueShadowRow,
)
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision
from src.ui.read_only_policy import is_public_get
from src.ui.services.geo_rescue_shadow_summary import summarize_geo_rescue_shadow

_Imp = Literal["high", "medium", "low", "error"]
_KEEP = {"medium", "high"}


def _article(i: str, *, body: str | None = None) -> Article:
    return Article(
        id=i,
        url=f"https://example.test/{i}",
        title=f"title {i}",
        summary_html="<p>summary</p>",
        body_text=body,
        published=datetime(2026, 10, 30, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://example.test/feed",
    )


def _ctx() -> JudgeContext:
    class _Cfg:
        ollama_base_url = "http://127.0.0.1:1"

    return JudgeContext(
        rescue_llm=cast(Any, object()),
        config=cast(Any, _Cfg()),
        think=False,
        model="kuebiko-sft:26b",
        model_digest="sha256:aaa",
        judge_model="kuebiko-sft:26b",
        judge_model_digest="sha256:aaa",
    )


def _spy_triage(
    monkeypatch: pytest.MonkeyPatch, verdict: dict[tuple[str, str], _Imp]
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def _fake(self: ArticleTriage, article: Article) -> TriageDecision:
        calls.append((self.prompt_path, article.id))
        v = verdict.get((self.prompt_path, article.id), "low")
        if v == "error":
            return TriageDecision(importance="medium", error=True)
        return TriageDecision(importance=v)

    monkeypatch.setattr(ArticleTriage, "triage", _fake)
    return calls


def _spy_judge(monkeypatch: pytest.MonkeyPatch, by_id: dict[str, GeoJudgeResult]) -> list[str]:
    asked: list[str] = []

    async def _fake(title: str, text: str, *, config: Any) -> GeoJudgeResult:
        aid = title.removeprefix("title ")
        asked.append(aid)
        return by_id[aid]

    monkeypatch.setattr(gs, "judge_geo_rescue", _fake)
    return asked


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    monkeypatch.delenv("GEO_RESCUE_SHADOW_PER_RUN", raising=False)
    monkeypatch.delenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", raising=False)


_YES = GeoJudgeResult(cues=("nation:RU",), p_watched_action=0.9, p_allied_response=0.1)
_NO = GeoJudgeResult(cues=("nation:CN",), p_watched_action=0.2, p_allied_response=0.1)
_FAIL = GeoJudgeResult(cues=("nation:IR",), p_watched_action=None, p_allied_response=0.1)


def _pending(
    i: str, *, flat: str = "low", prompt_mode: str = "flat", **kw: Any
) -> GeoRescueShadowRow:
    base: dict[str, Any] = {
        "article_id": i,
        "flat": flat,
        "status": STATUS_PENDING,
        "prompt_mode": prompt_mode,
        "title": f"title {i}",
        "input_text": f"Russian forces body {i}",
        "feed_title": "Feed",
        "url": f"https://example.test/{i}",
    }
    base.update(kw)
    return GeoRescueShadowRow(**base)


# ---- 旗・純関数 ----


def test_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEO_RESCUE_JUDGED_SHADOW", raising=False)
    assert is_geo_rescue_shadow_enabled() is False
    monkeypatch.setenv("GEO_RESCUE_JUDGED_SHADOW", "1")
    assert is_geo_rescue_shadow_enabled() is True


def test_cue_types_and_input_sha() -> None:
    assert cue_types(("nation:RU", "nation:CN", "theatre:baltic", "cyber_geo:hybrid")) == (
        "nation",
        "theatre",
        "cyber_geo",
    )
    assert input_sha256("t", "x") == input_sha256("t", "x") != input_sha256("t", "y")


# ---- 1. triage の時点: 写すだけ ----


@pytest.mark.asyncio
async def test_rescue_captures_candidates_without_changing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _spy_triage(
        monkeypatch, {("flat", "a"): "medium", ("flat", "b"): "low", ("flat", "c"): "error"}
    )
    decisions = [
        (_article("a"), "low", False, "地政学"),
        (_article("b"), "low", False, "軍事"),
        (_article("c"), "low", False, "外交"),
        (_article("d"), "low", False, "サイバー要素なし"),  # 対象外の理由
    ]
    plain = await _rescue_geopolitical(
        decisions, cast(Any, object()), keep_importance=_KEEP, think=False
    )
    captured: list[GeoRescueCandidate] = []

    out = await _rescue_geopolitical(
        decisions, cast(Any, object()), keep_importance=_KEEP, think=False, shadow_out=captured
    )

    assert out == plain  # 本番の結果は同じ
    assert out[1] == {"a"}
    assert [(c.article.id, c.flat) for c in captured] == [
        ("a", "medium"),
        ("b", "low"),
        ("c", "error"),
    ]


def test_pending_rows_are_deterministic_and_hold_the_judge_input() -> None:
    body = "<p>Russian drones over the Baltic.</p> " + "x" * 3000
    rows = pending_rows(
        [
            GeoRescueCandidate(_article("a", body=body), "low"),
            GeoRescueCandidate(_article("b"), "error"),
        ],
        prompt_mode="flat",
    )
    a, b = rows
    assert (a.status, a.prompt_mode, a.flat_error, b.flat_error) == (
        STATUS_PENDING,
        "flat",
        False,
        True,
    )
    assert a.cue_types == ("nation", "theatre")
    assert a.input_kind == "body" and b.input_kind == "summary"
    assert a.input_text is not None and len(a.input_text) == 1500 and "<p>" not in a.input_text
    assert a.input_sha256 == input_sha256("title a", a.input_text)
    assert (a.title, a.feed_title, a.pir, a.judged) == ("title a", "Feed", None, None)


def test_orchestrator_capture_writes_pending_and_swallows_errors(tmp_path: Path) -> None:
    import src.pipeline.orchestrator as orch

    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    orch._record_geo_rescue_pending([GeoRescueCandidate(_article("x"), "low")], repo=repo)  # noqa: SLF001
    (row,) = repo.list_geo_rescue_shadow(days=7)
    assert (row.article_id, row.status) == ("x", STATUS_PENDING)

    class _Broken:
        def record_geo_rescue_shadow(self, rows: Any) -> int:
            raise RuntimeError("db down")

    orch._record_geo_rescue_pending(  # noqa: SLF001 — 例外を外へ出さない
        [GeoRescueCandidate(_article("y"), "low")], repo=cast(Any, _Broken())
    )


# ---- 2. 保守チェーンの段: 判定 ----


def test_article_from_row_rebuilds_the_same_preview() -> None:
    (row,) = pending_rows(
        [GeoRescueCandidate(_article("a", body="Iranian navy " * 50), "low")], prompt_mode="flat"
    )
    rebuilt = article_from_row(row)
    preview = ArticleTriage(cast(Any, object()))._triage_content(rebuilt)
    assert input_sha256(rebuilt.title, preview) == row.input_sha256


@pytest.mark.asyncio
async def test_judge_row_formula_and_when_questions_are_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _spy_triage(
        monkeypatch,
        {
            ("pir", "y"): "medium",
            ("pir", "n"): "medium",
            ("pir", "f"): "medium",
            ("pir", "e"): "error",
        },
    )
    asked = _spy_judge(monkeypatch, {"y": _YES, "n": _NO, "f": _FAIL})

    rows = {
        r.article_id: r
        for r in [
            await judge_row(_pending(i, flat=f), _ctx())
            for i, f in (
                ("y", "low"),
                ("n", "low"),
                ("z", "low"),
                ("f", "low"),
                ("e", "low"),
                ("r", "medium"),
            )
        ]
    }

    assert (rows["y"].status, rows["y"].judged, rows["y"].p_qw) == (STATUS_OK, True, 0.9)
    assert (rows["n"].status, rows["n"].judged) == (STATUS_OK, False)
    assert (rows["z"].status, rows["z"].judged, rows["z"].pir, rows["z"].p_qw) == (
        STATUS_OK,
        False,
        "low",
        None,
    )
    assert (rows["f"].status, rows["f"].judged, rows["f"].judge_failed) == (
        STATUS_ERROR,
        None,
        True,
    )
    assert (rows["e"].status, rows["e"].pir_error) == (STATUS_ERROR, True)
    assert (rows["r"].judged, rows["r"].judge_failed) == (True, False)  # flat が救う → 常に救う
    assert sorted(asked) == ["f", "n", "y"]  # 問いは flat が救わず pir が救う候補だけ
    assert {r.model for r in rows.values()} == {"kuebiko-sft:26b"}
    assert {r.judge_model for r in rows.values()} == {"kuebiko-sft:26b"}
    assert len(calls) == 6


@pytest.mark.asyncio
async def test_judge_row_reuses_production_pir_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _spy_triage(monkeypatch, {})
    _spy_judge(monkeypatch, {"p": _YES})

    row = await judge_row(_pending("p", flat="low", prompt_mode="pir"), _ctx())

    assert calls == []  # 本番が既に pir の prompt で判定している → 生成を繰り返さない
    assert (row.pir, row.judged) == ("low", False)


@pytest.mark.asyncio
async def test_judge_pending_oldest_first_with_cap_budget_and_expiry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_geo_rescue_shadow([_pending(str(i)) for i in range(5)])
    with repo._connect() as conn:  # noqa: SLF001 — 古い行を作る
        conn.execute(
            "UPDATE geo_rescue_shadow SET created_at = ? WHERE article_id = '4'",
            ((datetime.now(UTC) - timedelta(hours=80)).isoformat(),),
        )
    _spy_triage(monkeypatch, {})
    _spy_judge(monkeypatch, {})
    monkeypatch.setenv("GEO_RESCUE_SHADOW_PER_RUN", "2")

    counts = await judge_pending(repo, _ctx())

    assert counts == {"pending": 2, "ok": 2, "error": 0, "left": 0, "expired": 1}
    by = {r.article_id: r.status for r in repo.list_geo_rescue_shadow(days=7)}
    assert by == {
        "0": STATUS_OK,
        "1": STATUS_OK,
        "2": STATUS_PENDING,
        "3": STATUS_PENDING,
        "4": STATUS_EXPIRED,
    }

    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "0")
    counts = await judge_pending(repo, _ctx())
    assert counts["left"] == 2 and counts["ok"] == 0  # 予算切れは pending のまま持ち越す


@pytest.mark.asyncio
async def test_judge_pending_timeout_and_exception_never_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_geo_rescue_shadow([_pending("s")])

    async def _slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(gs, "judge_row", _slow)
    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "0.05")
    assert (await judge_pending(repo, _ctx()))["left"] == 1

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(gs, "judge_row", _boom)
    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "10")
    assert (await judge_pending(repo, _ctx()))["error"] == 1
    (row,) = repo.list_geo_rescue_shadow(days=7)
    assert row.status == STATUS_ERROR


@pytest.mark.asyncio
async def test_model_digests_failure_returns_none() -> None:
    assert await gs.model_digests("http://127.0.0.1:1", ["m", None]) == {"m": None}


@pytest.mark.asyncio
async def test_logs_never_contain_the_judge_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_geo_rescue_shadow([_pending("y", input_text="UNIQUE-BODY-MARKER " * 20)])
    _spy_triage(monkeypatch, {("pir", "y"): "medium"})
    _spy_judge(monkeypatch, {"y": _YES})
    logged: list[str] = []

    class _Rec:
        def info(self, *a: Any, **k: Any) -> None:
            logged.append(repr((a, k)))

        warning = debug = info

    monkeypatch.setattr(gs, "_log", _Rec())
    await judge_pending(repo, _ctx())
    assert logged and not any("UNIQUE-BODY-MARKER" in x or "title y" in x for x in logged)


# ---- テーブル・集計・公開面・チェーン ----


def test_repo_truncates_updates_only_pending_and_purges(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_geo_rescue_shadow([_pending("a", title="t" * 900, input_text="x" * 2000)])
    (row,) = repo.list_pending_geo_rescue_shadow(limit=10)
    assert row.title is not None and len(row.title) == 500
    assert row.input_text == "x" * 1500
    done = GeoRescueShadowRow(
        **{**row.__dict__, "status": STATUS_OK, "judged": True, "pir": "medium"}
    )
    repo.update_geo_rescue_judgement(done)
    repo.update_geo_rescue_judgement(GeoRescueShadowRow(**{**row.__dict__, "status": STATUS_ERROR}))
    (got,) = repo.list_geo_rescue_shadow(days=7)
    assert (got.status, got.judged, got.pir) == (STATUS_OK, True, "medium")  # 2 回目は書かない
    assert got.judged_at
    assert repo.purge_geo_rescue_shadow(days=0) == 1


def test_summary_counts_only_never_text() -> None:
    rows = [
        _pending(
            "1", flat="medium", status=STATUS_OK, pir="low", judged=True, cue_types=("nation",)
        ),
        _pending("2", status=STATUS_OK, pir="medium", judged=True, cue_types=("nation",)),
        _pending("3", status=STATUS_OK, pir="medium", judged=False),
        _pending("4", status=STATUS_OK, pir="low", judged=False),
        _pending("5", status=STATUS_ERROR, judge_failed=True),
        _pending("6"),
        _pending("7", status=STATUS_EXPIRED, title="SECRET-TITLE", input_text="SECRET-TEXT"),
    ]
    out = summarize_geo_rescue_shadow(rows, days=7)
    assert out["by_status"] == {STATUS_PENDING: 1, STATUS_OK: 4, STATUS_ERROR: 1, STATUS_EXPIRED: 1}
    assert out["by_decision"] == {
        "flat_rescued": 1,
        "pir_only_judged_yes": 1,
        "pir_only_judged_no": 1,
        "pir_no": 1,
    }
    assert out["error_rate"] == round(1 / 5, 4)  # 分母 = ok + error
    dumped = repr(out)
    assert (
        "SECRET" not in dumped
        and "Russian forces body" not in dumped
        and "article_id" not in dumped
    )


def test_summary_endpoint_is_not_public() -> None:
    assert is_public_get("/api/v1/geo-rescue-shadow/summary") is False


def test_public_exports_never_read_the_table_and_forbid_the_key() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("export_public_site.py", "export_mirror.py"):
        src = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "FROM geo_rescue_shadow" not in src
        assert "list_geo_rescue_shadow" not in src
        assert "geo-rescue-shadow" not in src
        assert '"input_text"' in src


def test_judging_step_is_last_in_hourly_upkeep_and_off_alone() -> None:
    jobs = {j.id: j for j in default_jobs()}
    upkeep = jobs["hourly-upkeep"]
    assert upkeep.steps[-1] == "geo-rescue-shadow-hourly"
    step = jobs["geo-rescue-shadow-hourly"]
    assert step.enabled is False and step.max_runtime_minutes <= 5
