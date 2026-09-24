"""台帳の割当判定 ML (2026-09-24)。特徴量の SSoT と、木の JSON 推論。

実測 (Opus 盲検 645 組・情勢で分割した 5 分割): 規則の候補の中で、埋込の関門 (0.6) は
精度 67% / 回収 86%、RF は 0.6 で 73% / 81%、0.7 で 81% / 69%。効く特徴量は
記事×情勢題名の類似度 (0.35) と記事×開設時の記事の類似度 (0.22)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.assessment.assign_features import FEATURE_NAMES, assign_feature_vector
from src.assessment.assignment import ArticleKeys, SituationKeys


def _keys() -> tuple[ArticleKeys, SituationKeys]:
    from src.assessment.situation_store import SituationRow

    art = ArticleKeys(
        article_id="a1",
        title="t",
        strong=frozenset({"cve:CVE-2026-1", "actor:x"}),
        nations=frozenset({"US", "JP"}),
        tokens=frozenset({"脆弱性", "悪用", "製品"}),
    )
    row = SituationRow(
        situation_id="s1",
        title="t",
        domain="cyber_incident",
        status="active",
        anchors=frozenset(),
        pir_ids=(),
        opened_at="2026-09-01T00:00:00+00:00",
        last_evidence_at="2026-09-01T00:00:00+00:00",
    )
    sit = SituationKeys(
        row=row,
        strong=frozenset({"cve:CVE-2026-1", "victim_org:y"}),
        nations=frozenset({"US"}),
        tokens=frozenset({"脆弱性", "悪用"}),
        claim_type="discrete_event",
    )
    return art, sit


class TestFeatureVector:
    def test_length_matches_feature_names(self) -> None:
        art, sit = _keys()
        x = assign_feature_vector(art, sit, cos_title=0.7, cos_seed=None, age_days=3.0)

        assert len(x) == len(FEATURE_NAMES)

    def test_values_follow_the_names(self) -> None:
        art, sit = _keys()
        v = assign_feature_vector(art, sit, cos_title=0.7, cos_seed=0.5, age_days=3.0)
        x = dict(zip(FEATURE_NAMES, v, strict=True))

        assert x["cos_title"] == pytest.approx(0.7)
        assert x["cos_seed"] == pytest.approx(0.5)
        assert x["seed_missing"] == 0.0
        assert x["strong_cve"] == 1.0
        assert x["strong_total"] == 1.0
        assert x["nation_share"] == 1.0
        assert x["token_share"] == 2.0
        assert x["rule_anchor"] == 1.0  # 強い鍵を共有 → anchor 規則が当てはまる
        assert x["ct_discrete_event"] == 1.0
        assert x["age_days"] == pytest.approx(3.0)

    def test_missing_seed_falls_back_to_title_similarity_and_flags_it(self) -> None:
        """⭐ 開設時の記事の埋込が無ければ題名の類似度で代用し、欠測を旗で示す。"""
        art, sit = _keys()
        v = assign_feature_vector(art, sit, cos_title=0.7, cos_seed=None, age_days=0.0)
        x = dict(zip(FEATURE_NAMES, v, strict=True))

        assert x["cos_seed"] == pytest.approx(0.7)
        assert x["seed_missing"] == 1.0


class TestModel:
    @staticmethod
    def _write(tmp_path: Path, names: list[str], threshold: float = 0.6) -> Path:
        # 1 本の木: cos_title <= 0.6 なら確率 0.1、でなければ 0.9
        idx = names.index("cos_title")
        tree = {
            "left": [1, -1, -1],
            "right": [2, -1, -1],
            "feature": [idx, -2, -2],
            "threshold": [0.6, -2.0, -2.0],
            "proba": [0.0, 0.1, 0.9],
        }
        p = tmp_path / "assign_model.json"
        p.write_text(json.dumps({"feature_names": names, "threshold": threshold, "trees": [tree]}))
        return p

    def test_probability_walks_the_tree(self, tmp_path: Path) -> None:
        from src.assessment.assign_model import load_assign_model

        m = load_assign_model(str(self._write(tmp_path, list(FEATURE_NAMES))))
        assert m is not None
        x = [0.0] * len(FEATURE_NAMES)
        x[FEATURE_NAMES.index("cos_title")] = 0.8

        assert m.probability(x) == pytest.approx(0.9)
        assert m.assigns(x) is True

    def test_feature_mismatch_refuses_the_model(self, tmp_path: Path) -> None:
        """⚠ 並びの違うモデルを黙って使うと木が別の列を読む (群化 ML と同じ防御)。"""
        from src.assessment.assign_model import load_assign_model

        names = list(FEATURE_NAMES)[::-1]

        assert load_assign_model(str(self._write(tmp_path, names))) is None

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        from src.assessment.assign_model import load_assign_model

        assert load_assign_model(str(tmp_path / "none.json")) is None
