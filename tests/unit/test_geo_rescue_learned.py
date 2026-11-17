"""地政学の救済の学習版 (GEO_RESCUE_LEARNED=shadow) の部品・配線のテスト。

特徴量・JSON の読み込み・確率は純粋関数。旗 0 で何もしない・shadow でも本番の出力 (行・判定) が
変わらないことを固定する。Ollama・PG には触らない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

import src.pipeline.geo_rescue_learned as gl
import src.pipeline.geo_rescue_shadow as gs
from src.pipeline.geo_rescue_judge import GeoJudgeResult
from src.pipeline.geo_rescue_shadow import JudgeContext
from src.storage.repo_geo_rescue_shadow import STATUS_ERROR, STATUS_OK, GeoRescueShadowRow
from src.storage.run_history import RunHistoryRepository

EMB_DIM = 3


def _model_json(**over: Any) -> dict[str, Any]:
    n = len(gl.FEATURE_NAMES)
    base: dict[str, Any] = {
        "version": 1,
        "feature_names": list(gl.FEATURE_NAMES),
        "scale": [1.0] * n,
        "embedding_dim": EMB_DIM,
        "coef": [0.0] * (EMB_DIM + n),
        "intercept": 0.0,
        "threshold": 0.5,
        "probe_model": "kuebiko-sft:s26r",
        "embedding_model": "snowflake-arctic-embed2",
        "cv": {"recall": 0.85, "false_rescue": 0.15},
    }
    return {**base, **over}


def _write(tmp_path: Path, **over: Any) -> Path:
    p = tmp_path / "m.json"
    p.write_text(json.dumps(_model_json(**over)), encoding="utf-8")
    return p


def test_build_features_matches_training_layout() -> None:
    v = gl.build_features(
        pir="medium",
        flat="low",
        cues=["nation:RU", "war:ukraine", "cyber_geo:x"],
        p_qw=0.25,
        p_qc=None,
        p_qd=0.75,
        is_summary=True,
    )
    assert len(v) == len(gl.FEATURE_NAMES)
    d = dict(zip(gl.FEATURE_NAMES, v, strict=True))
    assert d["pir"] == 1 and d["flat"] == 0 and d["n_cues"] == 3
    assert d["has_cyber_geo_cue"] == 1
    assert d["cue_nation"] == 1 and d["cue_war"] == 1 and d["cue_theatre"] == 0
    assert d["qw"] == 0.25 and d["qc"] == 0.0 and d["qd"] == 0.75  # None は 0
    assert d["is_summary"] == 1


def test_build_features_unknown_levels_are_zero() -> None:
    v = gl.build_features(
        pir="error", flat="error", cues=[], p_qw=None, p_qc=None, p_qd=None, is_summary=False
    )
    assert set(v) == {0.0}


def test_load_model_and_predict(tmp_path: Path) -> None:
    n = len(gl.FEATURE_NAMES)
    coef = [1.0, 0.0, 0.0] + [0.0] * n
    model = gl.load_model(_write(tmp_path, coef=coef, intercept=-1.0))
    assert model is not None
    feats = tuple(0.0 for _ in range(n))
    assert gl.predict_proba(model, np.array([1.0, 5.0, 5.0]), feats) == pytest.approx(0.5)
    assert gl.predict_proba(model, np.array([3.0, 0.0, 0.0]), feats) > 0.5


def test_predict_applies_scale(tmp_path: Path) -> None:
    n = len(gl.FEATURE_NAMES)
    scale = [1.0] * n
    scale[0] = 2.0  # pir
    coef = [0.0] * EMB_DIM + [2.0] + [0.0] * (n - 1)
    model = gl.load_model(_write(tmp_path, scale=scale, coef=coef))
    assert model is not None
    feats = (2.0,) + (0.0,) * (n - 1)
    assert gl.predict_proba(model, np.zeros(EMB_DIM), feats) == pytest.approx(
        1 / (1 + np.exp(-2.0))
    )


def test_load_model_missing_returns_none(tmp_path: Path) -> None:
    assert gl.load_model(tmp_path / "nope.json") is None


@pytest.mark.parametrize(
    "over",
    [{"coef": [0.0]}, {"scale": [1.0]}, {"feature_names": ["x"]}, {"threshold": "a"}],
)
def test_load_model_rejects_inconsistent_json(tmp_path: Path, over: dict[str, Any]) -> None:
    assert gl.load_model(_write(tmp_path, **over)) is None


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEO_RESCUE_LEARNED", raising=False)
    assert gl.learned_mode() == "0"
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "shadow")
    assert gl.learned_mode() == "shadow"
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "1")  # 適用は未実装: shadow 以外は 0
    assert gl.learned_mode() == "0"


def test_embedding_input_matches_training() -> None:
    assert gl.embedding_input("T", "x" * 5000) == ("T\n" + "x" * 5000)[:2000]
    assert gl.embedding_input(None, None) == "\n"


# ---- 配線 ----


class _Emb:
    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str, *, kind: str = "document") -> Any:
        self.calls += 1

        class _R:
            vector = (1.0, 0.0, 0.0)

        return _R()


def _row(
    i: str, *, flat: str = "low", status: str = STATUS_OK, pir: str = "medium"
) -> GeoRescueShadowRow:
    return GeoRescueShadowRow(
        article_id=i,
        flat=flat,
        status=status,
        pir=pir,
        cue_types=("nation",),
        judged=True,
        title="Russia drills in Baltic",
        input_text="Russian forces held exercises near NATO members " * 6,
        input_kind="body",
        judge_model="kuebiko-sft:s26r",
    )


def _ctx() -> JudgeContext:
    return JudgeContext(
        rescue_llm=cast(Any, object()),
        config=cast(Any, object()),
        think=False,
        model="m",
        model_digest=None,
        judge_model="kuebiko-sft:s26r",
        judge_model_digest=None,
    )


async def test_learned_shadow_off_does_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GEO_RESCUE_LEARNED", raising=False)
    repo = RunHistoryRepository(tmp_path / "r.db")
    emb = _Emb()
    out = await gs.run_learned_shadow(_row("a"), repo, _ctx(), embedder=cast(Any, emb))
    assert out is None and emb.calls == 0
    assert repo.list_geo_rescue_learned_shadow(days=1) == []


async def test_learned_shadow_records_without_touching_production_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "shadow")
    n = len(gl.FEATURE_NAMES)
    model = gl.load_model(_write(tmp_path, coef=[5.0, 0.0, 0.0] + [0.0] * n, intercept=-1.0))
    monkeypatch.setattr(gl, "load_model", lambda *a, **k: model)
    probed: list[bool] = []

    async def _judge(
        title: str, text: str, *, config: Any, probe_all: bool = False
    ) -> GeoJudgeResult:
        probed.append(probe_all)
        return GeoJudgeResult(
            cues=("nation:RU",), p_watched_action=0.9, p_allied_response=0.1, p_cyber_geo=0.2
        )

    monkeypatch.setattr(gs, "judge_geo_rescue", _judge)
    monkeypatch.setattr(gs, "geo_cue_reasons", lambda t, x: ("nation:RU",))
    repo = RunHistoryRepository(tmp_path / "r.db")
    row = _row("a")
    before = row
    res = await gs.run_learned_shadow(row, repo, _ctx(), embedder=cast(Any, _Emb()))
    assert row == before  # 本番の行は不変
    assert probed == [True]  # 手掛かりがあれば 3 問とも当てる
    assert res is not None and res.ml_rescued is True and res.v2_judged is True
    rows = repo.list_geo_rescue_learned_shadow(days=1)
    assert len(rows) == 1 and rows[0].article_id == "a"
    assert rows[0].probe_model_match is True


async def test_learned_shadow_skips_when_model_missing_or_row_errored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "shadow")
    monkeypatch.setattr(gl, "load_model", lambda *a, **k: None)
    repo = RunHistoryRepository(tmp_path / "r.db")
    emb = _Emb()
    assert await gs.run_learned_shadow(_row("a"), repo, _ctx(), embedder=cast(Any, emb)) is None
    n = len(gl.FEATURE_NAMES)
    model = gl.load_model(_write(tmp_path, coef=[0.0] * (EMB_DIM + n)))
    monkeypatch.setattr(gl, "load_model", lambda *a, **k: model)
    bad = _row("b", status=STATUS_ERROR)
    assert await gs.run_learned_shadow(bad, repo, _ctx(), embedder=cast(Any, emb)) is None
    assert emb.calls == 0


async def test_learned_shadow_failure_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GEO_RESCUE_LEARNED", "shadow")
    n = len(gl.FEATURE_NAMES)
    model = gl.load_model(_write(tmp_path, coef=[0.0] * (EMB_DIM + n)))
    monkeypatch.setattr(gl, "load_model", lambda *a, **k: model)

    class _Boom:
        async def embed(self, text: str, *, kind: str = "document") -> Any:
            raise RuntimeError("ollama down")

    repo = RunHistoryRepository(tmp_path / "r.db")
    res = await gs.run_learned_shadow(_row("a"), repo, _ctx(), embedder=cast(Any, _Boom()))
    assert res is None
    assert repo.list_geo_rescue_learned_shadow(days=1) == []


def test_summary_counts_only_and_purge_follows_retention(tmp_path: Path) -> None:
    from src.storage.repo_geo_rescue_learned import GeoRescueLearnedRow
    from src.ui.services.geo_rescue_shadow_summary import summarize_geo_rescue_learned

    repo = RunHistoryRepository(tmp_path / "r.db")

    def mk(i: str, ml: bool, v2: bool | None, match: bool = True) -> GeoRescueLearnedRow:
        return GeoRescueLearnedRow(
            article_id=i,
            p_ml=0.6 if ml else 0.1,
            ml_rescued=ml,
            v2_judged=v2,
            flat="low",
            pir="low",
            threshold=0.425,
            probe_model="m",
            probe_model_match=match,
        )

    repo.record_geo_rescue_learned(
        [mk("a", True, True), mk("b", True, False), mk("c", False, None)]
    )
    repo.record_geo_rescue_learned([mk("a", False, False)])  # 記事ごとに 1 行
    rows = repo.list_geo_rescue_learned_shadow(days=1)
    assert [r.article_id for r in rows] == ["a", "b", "c"]
    s = summarize_geo_rescue_learned(rows)
    assert s["total"] == 3 and s["ml_rescued"] == 2 and s["v2_judged_yes"] == 1
    assert s["agreement"] == {"ml_yes__v2_yes": 1, "ml_yes__v2_no": 1}
    repo.purge_geo_rescue_shadow(days=-1)  # 未来を期限にして全部消える (同じ期限で一緒に消す)
    assert repo.list_geo_rescue_learned_shadow(days=1) == []


def test_fill_rate_audit_registers_learned_metric() -> None:
    from src.ui.services.fill_rate_audit import METRICS

    assert "geo_rescue_learned_shadow" in {m.key for m in METRICS}
