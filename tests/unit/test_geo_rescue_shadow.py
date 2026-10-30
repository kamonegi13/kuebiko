"""地政学の救済の判定 v2 の影の記録 (GEO_RESCUE_JUDGED_SHADOW、2026-10-31) のテスト。

本番の救済の判定・出力が変わらないこと、候補ごとに 1 行が残ること、上限・予算・失敗の扱い、
テーブルの読み書き・集計・公開面に出ないことを固定する。Ollama・DB (PG) には触らない。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import src.pipeline.geo_rescue_shadow as gs
from src.pipeline.filters import _rescue_geopolitical
from src.pipeline.geo_rescue_judge import GeoJudgeResult
from src.pipeline.geo_rescue_shadow import (
    GeoRescueCandidate,
    cue_types,
    input_sha256,
    is_geo_rescue_shadow_enabled,
    run_geo_rescue_shadow,
)
from src.storage.repo_geo_rescue_shadow import (
    STATUS_BUDGET,
    STATUS_CAP,
    STATUS_ERROR,
    STATUS_OK,
    GeoRescueShadowRow,
)
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.article_triage import ArticleTriage, TriageDecision
from src.ui.read_only_policy import is_public_get
from src.ui.services.geo_rescue_shadow_summary import summarize_geo_rescue_shadow

_Imp = Literal["high", "medium", "low"]
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


class _Cfg:
    ollama_base_url = "http://127.0.0.1:1"


class _Llm:
    model = "kuebiko-sft:26b"


def _spy_triage(
    monkeypatch: pytest.MonkeyPatch, verdict: dict[tuple[str, str], _Imp | Literal["error"]]
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


def _spy_judge(monkeypatch: pytest.MonkeyPatch, by_id: dict[str, GeoJudgeResult]) -> None:
    async def _fake(title: str, text: str, *, config: Any) -> GeoJudgeResult:
        return by_id[title.removeprefix("title ")]

    monkeypatch.setattr(gs, "judge_geo_rescue", _fake)

    async def _no_digest(base_url: str, model: str | None) -> str | None:
        return "sha256:abc"

    monkeypatch.setattr(gs, "model_digest", _no_digest)


@pytest.fixture(autouse=True)
def _pir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRIAGE_FLAT", "1")
    monkeypatch.setenv("PIR_DRIVEN_TRIAGE", "1")
    monkeypatch.delenv("GEO_RESCUE_SHADOW_PER_RUN", raising=False)
    monkeypatch.delenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", raising=False)


_YES = GeoJudgeResult(
    cues=("nation:RU", "theatre:baltic"), p_watched_action=0.9, p_allied_response=0.1
)
_NO = GeoJudgeResult(cues=("nation:CN",), p_watched_action=0.2, p_allied_response=0.1)
_NOCUE = GeoJudgeResult(cues=(), p_watched_action=None, p_allied_response=None)
_FAIL = GeoJudgeResult(cues=("nation:IR",), p_watched_action=None, p_allied_response=0.1)


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


@pytest.mark.asyncio
async def test_rescue_captures_candidates_without_changing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    _spy_triage(
        monkeypatch, {("flat", "a"): "medium", ("flat", "b"): "low", ("flat", "c"): "error"}
    )
    decisions = [
        (_article("a"), "low", False, "地政学"),
        (_article("b"), "low", False, "軍事"),
        (_article("c"), "low", False, "外交"),
        (_article("d"), "low", False, "サイバー要素なし"),  # 対象外の理由
    ]
    plain_out, plain_rescued = await _rescue_geopolitical(
        decisions,
        cast(Any, object()),
        keep_importance=_KEEP,
        think=False,
    )
    captured: list[GeoRescueCandidate] = []

    # Act
    out, rescued = await _rescue_geopolitical(
        decisions,
        cast(Any, object()),
        keep_importance=_KEEP,
        think=False,
        shadow_out=captured,
    )

    # Assert
    assert (out, rescued) == (plain_out, plain_rescued)  # 本番の結果は同じ
    assert rescued == {"a"}
    assert [(c.article.id, c.flat) for c in captured] == [
        ("a", "medium"),
        ("b", "low"),
        ("c", "error"),
    ]


@pytest.mark.asyncio
async def test_run_shadow_records_one_row_per_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _spy_triage(
        monkeypatch,
        {
            ("pir", "y"): "medium",
            ("pir", "n"): "medium",
            ("pir", "f"): "medium",
            ("pir", "e"): "error",
        },
    )
    _spy_judge(monkeypatch, {"y": _YES, "n": _NO, "z": _NOCUE, "f": _FAIL, "e": _YES, "r": _NO})
    cands = [
        GeoRescueCandidate(_article("y", body="b" * 300), "low"),  # pir ∧ QW → 救う
        GeoRescueCandidate(_article("n"), "low"),  # pir だが問いが No
        GeoRescueCandidate(_article("z"), "low"),  # pir も救わず手掛かりなし
        GeoRescueCandidate(_article("f"), "low"),  # 問いが読めない → error
        GeoRescueCandidate(_article("e"), "low"),  # pir の判定の失敗 → error
        GeoRescueCandidate(_article("r"), "medium"),  # flat が救う → judged は常に救う
    ]

    # Act
    rows = await run_geo_rescue_shadow(cands, rescue_llm=_Llm(), config=_Cfg())  # type: ignore[arg-type]

    # Assert
    by = {r.article_id: r for r in rows}
    assert len(rows) == 6
    assert (by["y"].status, by["y"].judged, by["y"].cue_types) == (
        STATUS_OK,
        True,
        ("nation", "theatre"),
    )
    assert by["y"].input_kind == "body" and by["n"].input_kind == "summary"
    assert (by["n"].status, by["n"].judged) == (STATUS_OK, False)
    assert (by["z"].status, by["z"].judged, by["z"].pir) == (STATUS_OK, False, "low")
    # pir が救わない / flat が救う候補には問いを当てない (手掛かりだけ。判定は式どおり)
    assert (by["z"].p_qw, by["r"].p_qw, by["r"].judge_failed) == (None, None, False)
    assert (by["f"].status, by["f"].judged, by["f"].judge_failed) == (STATUS_ERROR, None, True)
    assert (by["e"].status, by["e"].pir_error) == (STATUS_ERROR, True)
    assert by["r"].judged is True
    assert {r.model for r in rows} == {"kuebiko-sft:26b"}
    assert {r.model_digest for r in rows} == {"sha256:abc"}
    assert all(r.input_sha256 for r in rows)


@pytest.mark.asyncio
async def test_run_shadow_cap_and_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    _spy_triage(monkeypatch, {})
    _spy_judge(monkeypatch, {str(i): _NO for i in range(5)})
    monkeypatch.setenv("GEO_RESCUE_SHADOW_PER_RUN", "2")
    cands = [GeoRescueCandidate(_article(str(i)), "low") for i in range(5)]

    rows = await run_geo_rescue_shadow(cands, rescue_llm=_Llm(), config=_Cfg())  # type: ignore[arg-type]

    assert [r.status for r in rows] == [STATUS_OK, STATUS_OK, STATUS_CAP, STATUS_CAP, STATUS_CAP]

    monkeypatch.setenv("GEO_RESCUE_SHADOW_PER_RUN", "10")
    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "0")
    rows = await run_geo_rescue_shadow(cands, rescue_llm=_Llm(), config=_Cfg())  # type: ignore[arg-type]
    assert {r.status for r in rows} == {STATUS_BUDGET}
    assert all(r.judged is None for r in rows)


@pytest.mark.asyncio
async def test_run_shadow_timeout_and_exception_never_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _slow(*a: Any, **k: Any) -> Any:
        await asyncio.sleep(5)

    monkeypatch.setattr(gs, "judge_candidate", _slow)
    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "0.05")
    cands = [GeoRescueCandidate(_article("s"), "low")]
    rows = await run_geo_rescue_shadow(cands, rescue_llm=_Llm(), config=_Cfg())  # type: ignore[arg-type]
    assert [r.status for r in rows] == [STATUS_BUDGET]

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(gs, "judge_candidate", _boom)
    monkeypatch.setenv("GEO_RESCUE_SHADOW_BUDGET_SECONDS", "10")
    rows = await run_geo_rescue_shadow(cands, rescue_llm=_Llm(), config=_Cfg())  # type: ignore[arg-type]
    assert [r.status for r in rows] == [STATUS_ERROR]


@pytest.mark.asyncio
async def test_model_digest_failure_returns_none() -> None:
    assert await gs.model_digest("http://127.0.0.1:1", "m") is None
    assert await gs.model_digest("http://127.0.0.1:1", None) is None


def _row(i: str, **kw: Any) -> GeoRescueShadowRow:
    base: dict[str, Any] = {"article_id": i, "flat": "low", "status": STATUS_OK}
    base.update(kw)
    return GeoRescueShadowRow(**base)


def test_repo_record_list_purge(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    rows = [
        _row(
            "a",
            pir="medium",
            cue_types=("nation", "theatre"),
            p_qw=0.9,
            p_qc=0.1,
            judged=True,
            model="m",
            model_digest="d",
            input_kind="body",
            input_sha256="h",
        ),
        _row("b", status=STATUS_CAP),
    ]
    assert repo.record_geo_rescue_shadow(rows) == 2
    got = repo.list_geo_rescue_shadow(days=7)
    assert [r.article_id for r in got] == ["a", "b"]
    a = got[0]
    assert (a.cue_types, a.p_qw, a.p_qd, a.judged, a.input_sha256) == (
        ("nation", "theatre"),
        0.9,
        None,
        True,
        "h",
    )
    assert got[1].judged is None and got[1].cue_types == ()
    assert repo.purge_geo_rescue_shadow(days=0) == 2
    assert repo.list_geo_rescue_shadow(days=7) == []


def test_summary_counts_by_status_and_decision() -> None:
    rows = [
        _row("1", flat="medium", pir="low", judged=True, cue_types=("nation",)),
        _row("2", pir="medium", judged=True, cue_types=("nation",)),
        _row("3", pir="medium", judged=False, cue_types=()),
        _row("4", pir="low", judged=False),
        _row("5", status=STATUS_ERROR, judge_failed=True),
        _row("6", status=STATUS_BUDGET),
    ]
    out = summarize_geo_rescue_shadow(rows, days=7)
    assert out["total"] == 6
    assert out["by_status"] == {STATUS_OK: 4, STATUS_CAP: 0, STATUS_BUDGET: 1, STATUS_ERROR: 1}
    assert out["by_decision"] == {
        "flat_rescued": 1,
        "pir_only_judged_yes": 1,
        "pir_only_judged_no": 1,
        "pir_no": 1,
    }
    assert out["judged_yes"] == 2 and out["error_rate"] == round(1 / 6, 4)
    assert "article_id" not in str(out)


def test_summary_endpoint_is_not_public() -> None:
    assert is_public_get("/api/v1/geo-rescue-shadow/summary") is False


@pytest.mark.asyncio
async def test_orchestrator_helper_records_and_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import src.pipeline.orchestrator as orch

    repo = RunHistoryRepository(db_path=tmp_path / "t.db")

    async def _rows(*a: Any, **k: Any) -> list[GeoRescueShadowRow]:
        return [_row("x")]

    monkeypatch.setattr(orch, "run_geo_rescue_shadow", _rows)
    cands = [GeoRescueCandidate(_article("x"), "low")]
    await orch._record_geo_rescue_shadow(  # noqa: SLF001
        cands,
        rescue_llm=cast(Any, _Llm()),
        config=cast(Any, _Cfg()),
        think=False,
        repo=repo,
    )
    assert [r.article_id for r in repo.list_geo_rescue_shadow(days=7)] == ["x"]

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("x")

    monkeypatch.setattr(orch, "run_geo_rescue_shadow", _boom)
    await orch._record_geo_rescue_shadow(  # noqa: SLF001 — 例外を外へ出さない
        cands,
        rescue_llm=cast(Any, _Llm()),
        config=cast(Any, _Cfg()),
        think=False,
        repo=repo,
    )


# ---- 判定の入力の保存 (2026-10-31 の決定): DB にだけ置き、公開面・API・ログに出さない ----


@pytest.mark.asyncio
async def test_rows_store_judge_input_and_logs_never_contain_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_body = "UNIQUE-BODY-MARKER " * 20
    _spy_triage(monkeypatch, {("pir", "y"): "medium"})
    _spy_judge(monkeypatch, {"y": _YES})
    logged: list[str] = []

    class _Rec:
        def info(self, *a: Any, **k: Any) -> None:
            logged.append(repr((a, k)))

        warning = debug = info

    monkeypatch.setattr(gs, "_log", _Rec())
    cands = [GeoRescueCandidate(_article("y", body=secret_body), "low")]

    rows = await run_geo_rescue_shadow(
        cands, rescue_llm=cast(Any, _Llm()), config=cast(Any, _Cfg())
    )

    (row,) = rows
    assert row.title == "title y"
    assert row.input_text is not None and row.input_text.startswith("UNIQUE-BODY-MARKER")
    assert len(row.input_text) <= 1500
    assert row.input_sha256 == input_sha256("title y", row.input_text)
    assert logged and not any("UNIQUE-BODY-MARKER" in x or "title y" in x for x in logged)


def test_repo_truncates_and_round_trips_input(tmp_path: Path) -> None:
    repo = RunHistoryRepository(db_path=tmp_path / "t.db")
    repo.record_geo_rescue_shadow([_row("a", title="t" * 900, input_text="x" * 2000)])
    (got,) = repo.list_geo_rescue_shadow(days=7)
    assert got.title is not None and len(got.title) == 500
    assert got.input_text == "x" * 1500


def test_summary_returns_counts_only_never_text() -> None:
    rows = [_row("1", pir="medium", judged=True, title="SECRET-TITLE", input_text="SECRET-TEXT")]
    out = summarize_geo_rescue_shadow(rows, days=7)
    dumped = repr(out)
    assert "SECRET-TITLE" not in dumped and "SECRET-TEXT" not in dumped


def test_public_exports_never_read_the_table_and_forbid_the_key() -> None:
    root = Path(__file__).resolve().parents[2]
    for name in ("export_public_site.py", "export_mirror.py"):
        src = (root / "scripts" / name).read_text(encoding="utf-8")
        assert "FROM geo_rescue_shadow" not in src  # 表を読まない
        assert "list_geo_rescue_shadow" not in src
        assert "geo-rescue-shadow" not in src  # 集計 API も写さない
        assert '"input_text"' in src  # 万一の経路に対する禁止のキー
