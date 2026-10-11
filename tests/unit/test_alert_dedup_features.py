"""重複 alert の ML の特徴量・先行の選び方・モデル読み込み (R1h)。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from src.cti.alert_dedup_features import (
    FEATURE_NAMES,
    AlertSide,
    alert_dedup_features,
    select_prior,
)
from src.cti.alert_dedup_model import DEFAULT_THRESHOLD, load_alert_dedup_model
from src.eventnews.pair_model import PairModel

_NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
_PAIR_DIM = 25  # pair_features 23 + llm 2


def _pair_model(proba: float) -> PairModel:
    """常に ``proba`` を返す 1 枚葉の木 (閾値 0.5)。"""
    tree: dict[str, list[float]] = {
        "left": [-1],
        "right": [-1],
        "feature": [-2],
        "threshold": [0.0],
        "proba": [proba],
    }
    return PairModel(
        feature_names=tuple(f"f{i}" for i in range(_PAIR_DIM)),
        threshold_llm_on=0.5,
        threshold_llm_off=0.5,
        _trees=(tree,),
    )


def _side(
    article_id: str,
    *,
    hours_before: float = 0.0,
    vec: tuple[float, ...] = (1.0, 0.0, 0.0),
    title: str = "t",
    summary: str = "",
    feed_title: str = "feed",
    entities: frozenset[tuple[str, str]] = frozenset(),
) -> AlertSide:
    return AlertSide(
        article_id=article_id,
        title=title,
        summary=summary,
        category="apt",
        feed_title=feed_title,
        feed_url="",
        account_class="",
        time=_NOW - timedelta(hours=hours_before),
        entities=entities,
        vector=np.asarray(vec, dtype=np.float32),
    )


class TestFeatures:
    def test_length_and_order_match_feature_names(self) -> None:
        cur, prior = _side("c"), _side("p", hours_before=5)

        feats = alert_dedup_features(cur, prior, 0.8)

        assert len(feats) == len(FEATURE_NAMES)
        by_name = dict(zip(FEATURE_NAMES, feats, strict=True))
        assert by_name["p_pair"] == pytest.approx(0.8)
        assert by_name["cos"] == pytest.approx(1.0)
        assert by_name["hours_apart"] == pytest.approx(5.0)
        assert by_name["same_feed"] == 1.0

    def test_entity_add_and_miss_counts_by_type(self) -> None:
        cur = _side("c", entities=frozenset({("actor", "APT1"), ("cve", "CVE-2026-1")}))
        prior = _side("p", hours_before=1, entities=frozenset({("actor", "apt1"), ("tool", "X")}))

        by_name = dict(zip(FEATURE_NAMES, alert_dedup_features(cur, prior, 0.7), strict=True))

        assert by_name["add_cve"] == 1.0
        assert by_name["add_actor"] == 0.0
        assert by_name["miss_tool"] == 1.0

    def test_new_state_word_and_numbers_detected(self) -> None:
        cur = _side("c", summary="patch released; 1,200 servers affected")
        prior = _side("p", hours_before=1, summary="vulnerability disclosed")

        by_name = dict(zip(FEATURE_NAMES, alert_dedup_features(cur, prior, 0.7), strict=True))

        assert by_name["new_state_patch"] == 1.0
        assert by_name["new_state_total"] >= 1.0
        assert by_name["num_added"] >= 1.0

    def test_missing_material_yields_zeros_not_errors(self) -> None:
        cur = _side("c", title="", summary="", feed_title="")
        prior = _side("p", hours_before=0, title="", summary="", feed_title="")

        feats = alert_dedup_features(cur, prior, 0.0)

        assert len(feats) == len(FEATURE_NAMES)
        assert all(np.isfinite(feats))

    def test_zero_vector_gives_zero_cos(self) -> None:
        cur = _side("c", vec=(0.0, 0.0, 0.0))

        by_name = dict(
            zip(
                FEATURE_NAMES,
                alert_dedup_features(cur, _side("p", hours_before=1), 0.5),
                strict=True,
            )
        )

        assert by_name["cos"] == 0.0


class TestSelectPrior:
    def test_picks_newest_matching_candidate(self) -> None:
        cur = _side("c")
        old, new = _side("old", hours_before=30), _side("new", hours_before=3)

        choice = select_prior(cur, [old, new], _pair_model(0.9))

        assert choice is not None
        assert choice.side.article_id == "new"
        assert choice.p_pair == pytest.approx(0.9)

    def test_none_when_pair_model_rejects(self) -> None:
        assert select_prior(_side("c"), [_side("p", hours_before=2)], _pair_model(0.1)) is None

    def test_none_when_cos_below_threshold(self) -> None:
        far = _side("p", hours_before=2, vec=(0.0, 1.0, 0.0))

        assert select_prior(_side("c"), [far], _pair_model(0.9)) is None

    def test_excludes_outside_window_future_and_self(self) -> None:
        cur = _side("c")
        candidates = [
            _side("too_old", hours_before=73),
            _side("future", hours_before=-1),
            _side("c", hours_before=1),
        ]

        assert select_prior(cur, candidates, _pair_model(0.9)) is None


class TestModel:
    def _write(self, path: Path, names: tuple[str, ...], **extra: object) -> None:
        tree = {"left": [-1], "right": [-1], "feature": [-2], "threshold": [0.0], "proba": [0.8]}
        body = {"version": "v-test", "feature_names": list(names), "trees": [tree], **extra}
        path.write_text(json.dumps(body), encoding="utf-8")

    def test_loads_and_infers(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        self._write(path, FEATURE_NAMES)

        model = load_alert_dedup_model(str(path))

        assert model is not None
        p = model.p_suppress([0.0] * len(FEATURE_NAMES))
        assert p == pytest.approx(0.8)
        assert model.threshold == DEFAULT_THRESHOLD
        assert model.would_suppress(p) is True
        assert model.version == "v-test"

    def test_threshold_from_json(self, tmp_path: Path) -> None:
        path = tmp_path / "m2.json"
        self._write(path, FEATURE_NAMES, threshold=0.9)

        model = load_alert_dedup_model(str(path))

        assert model is not None
        assert model.would_suppress(0.8) is False

    def test_feature_mismatch_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "m3.json"
        self._write(path, FEATURE_NAMES[:-1])

        assert load_alert_dedup_model(str(path)) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_alert_dedup_model(str(tmp_path / "none.json")) is None

    def test_shipped_model_matches_feature_names(self) -> None:
        model = load_alert_dedup_model()

        assert model is not None
        assert model.p_suppress([0.0] * len(FEATURE_NAMES)) >= 0.0
