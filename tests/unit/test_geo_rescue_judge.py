"""地政学の救済の判定 v2 (語の関門 → QW・QC・QD、影の記録で使う) のテスト。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.pipeline.geo_rescue_judge as gj
from src.pipeline.geo_rescue_judge import (
    GEO_JUDGE_THRESHOLD,
    Q_ALLIED_RESPONSE,
    Q_CYBER_GEO,
    Q_WATCHED_ACTION,
    GeoJudgeResult,
    judge_geo_rescue,
    judge_prompt,
    judged_rescued,
    unusable_body_reason,
)
from src.storage.config_store import save_config
from src.tools.model_tiers import (
    MODEL_TIERS_CONFIG_KEY,
    STEP_INHERITS,
    STEP_REGISTRY,
    Step,
    invalidate_model_tiers_cache,
    resolve_step_model,
)

_CUE = ("nation:RU",)
_YES = GeoJudgeResult(cues=_CUE, p_watched_action=0.9, p_allied_response=0.1)


_D_CUE = ("cyber_geo:influence",)


def _r(
    pw: float | None,
    pa: float | None,
    cues: tuple[str, ...] = _CUE,
    pd: float | None = None,
) -> GeoJudgeResult:
    return GeoJudgeResult(cues=cues, p_watched_action=pw, p_allied_response=pa, p_cyber_geo=pd)


@pytest.mark.parametrize(
    ("cues", "pw", "pa", "passed", "failed"),
    [
        (_CUE, 0.9, 0.1, True, False),  # QW だけ Yes でも通す (OR)
        (_CUE, 0.1, 0.9, True, False),  # QC だけ Yes でも通す
        (_CUE, GEO_JUDGE_THRESHOLD, 0.0, True, False),  # しきい値ちょうどは Yes
        (_CUE, 0.49, 0.49, False, False),
        ((), 0.99, 0.99, False, False),  # 手掛かりが無ければ問いが Yes でも通さない
        ((), None, None, False, False),  # 本番で問わなかった記事は失敗に数えない
        (_CUE, None, 0.9, True, True),  # 読めない問いは No、もう一方の Yes で通る
        (_CUE, None, None, False, True),
    ],
)
def test_judge_result_is_cue_and_either_question(
    cues: tuple[str, ...], pw: float | None, pa: float | None, passed: bool, failed: bool
) -> None:
    r = _r(pw, pa, cues)
    assert r.passed is passed
    assert r.failed is failed


@pytest.mark.parametrize(
    ("cues", "pw", "pa", "pd", "passed", "failed"),
    [
        (_D_CUE, None, None, 0.9, True, False),  # D の手掛かりだけ → QD の Yes で通る
        (_D_CUE, None, None, 0.1, False, False),
        (_D_CUE, None, None, None, False, True),
        # D の手掛かりが無い記事の QD は効かない (測定で読んでも)
        (_CUE, 0.1, 0.1, 0.99, False, False),
        # A〜C の手掛かりが無い記事の QW・QC は効かない
        (_D_CUE, 0.99, 0.99, 0.1, False, False),
        (_CUE + _D_CUE, 0.1, 0.1, 0.9, True, False),  # 両方ある → どれかが Yes で通る
        (_CUE + _D_CUE, 0.9, 0.1, None, True, True),
    ],
)
def test_cyber_geo_cue_routes_to_qd(
    cues: tuple[str, ...],
    pw: float | None,
    pa: float | None,
    pd: float | None,
    passed: bool,
    failed: bool,
) -> None:
    r = _r(pw, pa, cues, pd)
    assert r.passed is passed
    assert r.failed is failed


@pytest.mark.parametrize(
    ("flat", "pir", "judge", "expected"),
    [
        (True, False, _r(None, None, ()), True),  # flat の救済は判定に関係なく残す
        (False, True, _YES, True),
        (False, True, _r(0.2, 0.2), False),
        (False, True, _r(0.9, 0.9, ()), False),
        (False, False, _YES, False),  # pir が救わなければ判定が Yes でも救わない
    ],
)
def test_judged_rescued_formula(
    flat: bool, pir: bool, judge: GeoJudgeResult, expected: bool
) -> None:
    assert judged_rescued(flat_rescued=flat, pir_rescued=pir, judge=judge) is expected


def test_judge_prompt_has_title_text_and_question() -> None:
    out = judge_prompt("T", "本文の冒頭", Q_WATCHED_ACTION)
    assert out.startswith("タイトル: T\n\n本文:\n本文の冒頭\n\n")
    assert out.endswith(Q_WATCHED_ACTION)


def test_questions_split_watched_action_and_allied_response() -> None:
    """QW = 注視国 (と代理勢力) の安全保障の行動、QC = 記事に結び付きの書かれた同盟国の対応。"""
    for term in ("フーシ派", "ヒズボラ", "ワグネル", "ハクティビスト", "相手側の軍事行動"):
        assert term in Q_WATCHED_ACTION
    # 誤りの型 B・C・D (国内・経済・発言) を除外の文で受ける
    for term in ("国内の政治", "経済・貿易・投資・産業政策", "発言・論評・見通しの紹介だけ"):
        assert term in Q_WATCHED_ACTION
    assert "注視国の兵器が他国に渡った" in Q_WATCHED_ACTION
    # 定義が数える経済的な威圧・代理の網は除外しない (2026-10-30 改訂)
    for term in ("制裁や報復の輸出規制などの経済的な威圧", "北朝鮮の IT 労働者", "海上民兵"):
        assert term in Q_WATCHED_ACTION
    assert "威圧や報復を伴わない経済" in Q_WATCHED_ACTION
    assert "訓練・組織改編・支援" in Q_ALLIED_RESPONSE
    for term in ("記事自身が", "AUKUS", "NATO の東側", "バルト海", "侵入・領空侵犯"):
        assert term in Q_ALLIED_RESPONSE
    assert "国や地域の名前が出ていても No" in Q_ALLIED_RESPONSE
    for term in (
        "サイバー戦略",
        "影響工作",
        "海底ケーブル",
        "ハイブリッド",
        "新しい決定や事案が無いなら No",
    ):
        assert term in Q_CYBER_GEO
    for q in (Q_WATCHED_ACTION, Q_ALLIED_RESPONSE, Q_CYBER_GEO):
        assert q.startswith("質問: この記事が主に報じている出来事は")
        assert q.rstrip().endswith("と答える。")


_ARTICLE = "The ministry announced a new procurement programme for coastal patrol vessels. " * 5


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", "empty"),
        ("short text", "empty"),
        (
            "example.org\nPerforming security verification\nThis website uses a security "
            "service to protect against malicious bots. This page is displayed while the "
            "website verifies you are not a bot." + " " * 10 + "x" * 150,
            "block_page",
        ),
        ("Just a moment... Enable JavaScript and cookies to continue. " + "x" * 200, "block_page"),
        ("We value your privacy. We and our partners use cookies. " + "x" * 200, "consent_page"),
        ("The post Some headline appeared first on Example Site." + " " * 200, "empty"),
        (_ARTICLE, None),
        # 長い本文の中に語が出ても画面とは見なさない (本番と同じ 2,000 字の上限)
        ("verify you are human " + _ARTICLE * 6, None),
        # 冒頭 600 字より後の語は見ない
        (_ARTICLE * 2 + "accept all cookies", None),
    ],
)
def test_unusable_body_reason(body: str, expected: str | None) -> None:
    assert unusable_body_reason(body) == expected


def test_unusable_body_reason_feed_boilerplate() -> None:
    body = "Lead line. The post " + "A" * 150 + " appeared first on Example Site."
    assert unusable_body_reason(body) == "feed_boilerplate"


def _spy_probe(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Step, str]]:
    asked: list[tuple[Step, str]] = []

    async def _probe(step: Step, prompt: str, choices: tuple[str, ...], *, config: Any) -> Any:
        asked.append((step, prompt))
        assert choices == ("Yes", "No")
        return None if Q_ALLIED_RESPONSE in prompt else {"Yes": 0.7, "No": 0.3}

    monkeypatch.setattr(gj, "probe_choice", _probe)
    return asked


@pytest.mark.asyncio
async def test_judge_geo_rescue_reads_both_questions_when_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    asked = _spy_probe(monkeypatch)

    # Act
    r = await judge_geo_rescue("Russian drills near the Baltic", "x", config=object())  # type: ignore[arg-type]

    # Assert
    assert {s for s, _ in asked} == {Step.GEO_RESCUE_JUDGE}
    assert len(asked) == 2  # D の手掛かりが無いので QD は問わない
    assert not any(Q_CYBER_GEO in p for _, p in asked)
    assert "nation:RU" in r.cues and "theatre:baltic" in r.cues
    assert (r.p_watched_action, r.p_allied_response, r.p_cyber_geo) == (0.7, None, None)
    assert r.passed is True
    assert r.failed is True


@pytest.mark.asyncio
async def test_judge_geo_rescue_asks_only_qd_for_cyber_geo_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注視国の名前が無い D の記事 (同盟国の国家サイバー戦略) は QD だけを問い、その Yes で通す。"""
    asked = _spy_probe(monkeypatch)

    r = await judge_geo_rescue(
        "Government publishes national cyber strategy",
        "The ministry released the document.",
        config=object(),  # type: ignore[arg-type]
    )

    assert [Q_CYBER_GEO in p for _, p in asked] == [True]
    assert r.has_cyber_geo_cue and not r.has_geo_cue
    assert r.passed is True


@pytest.mark.asyncio
async def test_judge_geo_rescue_skips_llm_without_cue(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _spy_probe(monkeypatch)

    r = await judge_geo_rescue("Navy orders patrol boats", "a contract", config=object())  # type: ignore[arg-type]

    assert asked == []
    assert r == GeoJudgeResult(
        cues=(), p_watched_action=None, p_allied_response=None, p_cyber_geo=None
    )
    assert r.passed is False


@pytest.mark.asyncio
async def test_judge_geo_rescue_probe_all_reads_questions_without_cue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """測定は手掛かりが無くても両問を読む。最終判定は同じ (通さない)。"""
    asked = _spy_probe(monkeypatch)

    r = await judge_geo_rescue(
        "Navy orders patrol boats",
        "a contract",
        config=object(),  # type: ignore[arg-type]
        probe_all=True,
    )

    assert len(asked) == 3
    assert r.p_watched_action == 0.7 and r.p_cyber_geo == 0.7
    assert r.passed is False


def test_judge_step_is_a_small_fast_probe_inheriting_the_rescue_model() -> None:
    assert STEP_INHERITS[Step.GEO_RESCUE_JUDGE] is Step.TRIAGE_GEO_RESCUE
    assert STEP_REGISTRY[Step.GEO_RESCUE_JUDGE].tier == STEP_REGISTRY[Step.TRIAGE_GEO_RESCUE].tier
    assert STEP_REGISTRY[Step.GEO_RESCUE_JUDGE].timeout_seconds == 120.0


def test_judge_step_resolves_to_the_rescue_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODEL_TIERS_CONFIG_DB", raising=False)
    db_path = tmp_path / "tiers.db"
    invalidate_model_tiers_cache()
    doc = {"fast": "kuebiko-sft:s24ci65", "step:triage_geo_rescue": "kuebiko-sft:26b"}
    save_config(MODEL_TIERS_CONFIG_KEY, doc, db_path=db_path)
    invalidate_model_tiers_cache()

    # Act
    model = resolve_step_model(Step.GEO_RESCUE_JUDGE, db_path=db_path)

    # Assert
    assert model == "kuebiko-sft:26b"
    invalidate_model_tiers_cache()
