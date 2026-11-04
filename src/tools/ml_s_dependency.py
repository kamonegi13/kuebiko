"""学習済み ML が使う「常駐 S 由来の特徴」の一覧をコードから導く (S-SWITCH-RETRAIN、2026-11)。

S (常駐の判定・分類・要約系 LLM、``model_registry.RESIDENT_SLOTS["S"]``) を切り替えると、
S が作る特徴 (event_kind の種別・深刻度の軸・深刻さ C の問いの確率・pair の対の判定・要約由来の
欄) の分布が変わる。これを特徴量にしている ML (``config/models/*.json``) は学び直しが要る。
このモジュールは各 ML の ``feature_names`` を、特徴量の組み立て (builder) を読んで書いた規則で
「どの step の出力か」に振り分け、**どの ML がどの S 由来の特徴を持つか**と、
**学習時の S が今の S と合っているか**を返す。純粋関数 (DB・LLM 非依存)。

⚠ 新しい特徴量は規則のどれかに当たること。当たらない特徴量は ``unmapped`` になり、
``tests/unit/test_ml_s_dependency.py`` が落ちる (新しい特徴を黙って見落とさない)。
手順書は ``docs/s_switch_retrain.md``。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

STRONG: Final = "strong"  # その step の出力そのもの (種別・軸・確率・対の判定)
WEAK: Final = "weak"  # 要約などからの派生 (category・importance・件数)
DET: Final = "det"  # 決定論 (LLM を通らない)

#: S が作る step。これらの出力を特徴にする ML は S の切替で分布が変わる。
#: embed (埋込ティア)・assign_evidence_judge (gemma4 固定)・
#: synthesis_detect (s23i 固定) は S ではない。
S_DERIVED_STEPS: Final[frozenset[str]] = frozenset(
    {"event_kind", "severity_axes", "pair_judge", "judgment_classify", "article_summary"}
)

#: 学び直しの対象 (S の切替で学び直す ML)。それ以外は ``NOT_RETRAINED`` に理由つきで置く
RETRAIN_TARGETS: Final[tuple[str, ...]] = (
    "detect_model.json",
    "deep_dive_model.json",
    "severity_s3_model.json",
    "pair_model.json",
    "relation_model.json",
)

#: 対象外の ML と理由 (S 由来の特徴を持たないか、持っても別の経路で扱う)
NOT_RETRAINED: Final[Mapping[str, str]] = {
    "assign_model.json": "S 由来は synthesis_detect (s23i 固定) の弱い特徴のみ。S では動かない",
    "assign_model_probe.json": "logprob は gemma4:26b 固定 (assign_evidence_judge)。S では動かない",
    "relevance_jp_model.json": "埋込ティアのみ (S ではない)",
    "alert_dedup_model.json": "p_pair (pair_model の確率) と要約由来の弱い特徴。pair 後に確認",
}


@dataclass(frozen=True)
class RetrainStep:
    """学び直しの 1 本。``trainer`` が None は repo 内に学習スクリプトが無い (手順書の課題)。"""

    file: str
    trainer: str | None
    #: 付け直しが要る S 由来の入力 (手順書 §2)
    reattach: tuple[str, ...]
    #: 合否の関門 (手順書 §4 の表と同じ。数値は D1 / CLAUDE.md §7 の出典つき)
    gate: str


RETRAIN_PLAN: Final[tuple[RetrainStep, ...]] = (
    RetrainStep(
        "detect_model.json",
        "scripts/train_detect_ml.py",
        ("severity_axes", "event_kind"),
        "AUC が現行より有意に悪くない (補正 t の 95% 区間の下限 > -0.01、P(悪い) < 0.2) かつ "
        "審判済みの「新だけ」の開設 >= 「現行だけ」(D1 §4)",
    ),
    RetrainStep(
        "severity_s3_model.json",
        "scripts/train_severity_s3_model.py",
        ("severity_axes", "event_kind"),
        "分割外の AUC・適合・回収が現行 (AUC 0.916・適合 0.69・回収 0.83、閾値 0.50) を下回らない "
        "(CLAUDE.md §7 10-28 の行)",
    ),
    RetrainStep(
        "deep_dive_model.json",
        "scripts/train_deep_dive_ml.py",
        ("event_kind",),
        "held-out 窓の相関が現行 (0.33) を下回らない",
    ),
    RetrainStep(
        "relation_model.json",
        "scripts/train_relation_model.py",
        ("event_kind",),
        "正解集の適合率が --min-precision (0.8) 以上で、現行の同じ閾値での回収を下回らない",
    ),
    RetrainStep(
        "pair_model.json",
        None,
        ("pair_judge", "event_kind"),
        "同じ入力の対で 'same' 率が cos の帯ごとに現行から大きくずれない (10-29 版 45 の教訓) "
        "かつ正解集の適合率が現行を下回らない",
    ),
)

#: 設定にあるが ML の重みではないもの
SKIP_FILES: Final[frozenset[str]] = frozenset({"probe_thresholds.json"})


@dataclass(frozen=True)
class FeatureRule:
    pattern: str
    step: str
    strength: str


@dataclass(frozen=True)
class MlSpec:
    file: str
    builder: str
    rules: tuple[FeatureRule, ...]
    #: step → JSON の欄 (学習時に使ったモデル名を持つ欄)
    trained_field: Mapping[str, str]


def _r(pattern: str, step: str, strength: str) -> FeatureRule:
    return FeatureRule(pattern, step, strength)


ML_SPECS: Final[tuple[MlSpec, ...]] = (
    MlSpec(
        "detect_model.json",
        "src/synthesis/grounded/detect_ml.py",
        (
            _r(r"^kind=", "event_kind", STRONG),
            _r(r"^axis_", "severity_axes", STRONG),
            _r(
                r"^(importance|category=|n_(victim_org|affected_product|involved_country|ttp)"
                r"|country=|japan_targeted)",
                "article_summary",
                WEAK,
            ),
            _r(
                r"^(tier=|n_actor|n_cve|n_malware|n_pir|actor_|n_entities|followup"
                r"|in_progress|closed|title_len)",
                "det",
                DET,
            ),
        ),
        {"severity_axes": "axes_model"},
    ),
    MlSpec(
        "deep_dive_model.json",
        "src/digest/deep_dive_ml.py",
        (
            _r(r"^kind=", "event_kind", STRONG),
            _r(
                r"^(importance|category=|n_(victim_org|affected_product|involved_country|ttp)"
                r"|country=|japan_targeted|summary_len)",
                "article_summary",
                WEAK,
            ),
            _r(
                r"^(tier=|n_actor|n_cve|n_malware|n_pir|actor_|n_entities|followup|in_progress"
                r"|closed|title_len|corroboration|age_hours|is_novel)",
                "det",
                DET,
            ),
        ),
        {},
    ),
    MlSpec(
        "severity_s3_model.json",
        "src/cti/severity_c.py (feature_vector)",
        (
            _r(r"^p\.", "severity_axes", STRONG),
            _r(r"^kind=", "event_kind", STRONG),
            _r(r"^cat=", "article_summary", WEAK),
            _r(r"^type=", "judgment_classify", WEAK),
            _r(r"^(kev|cvss|cvss_ge9|log_loss|n_cves)$", "det", DET),
        ),
        {"severity_axes": "probe_model"},
    ),
    MlSpec(
        "pair_model.json",
        "src/eventnews/pair_features.py",
        (
            _r(r"^llm_(same|known)$", "pair_judge", STRONG),
            _r(r"^kind_", "event_kind", STRONG),
            _r(r"^(cos_summary|cos_delta|same_category|shared_victim)$", "article_summary", WEAK),
            _r(
                r"^(cos|shared_(names|cves|malware|tool|actor)|victim_conflict|roundup_"
                r"|same_feed|title_|hours_apart|count_)",
                "det",
                DET,
            ),
        ),
        {"pair_judge": "trained_llm_models"},
    ),
    MlSpec(
        "relation_model.json",
        "src/eventnews/relation_pair_features.py",
        (
            _r(r"^kinds_", "event_kind", STRONG),
            _r(r"^(mindf_victims|n_victims|same_country|same_sector)$", "article_summary", WEAK),
            _r(
                r"^(centroid_cos|gap_days|mindf_|n_|overlap|roundup_|size_|subject_conflict)",
                "det",
                DET,
            ),
        ),
        {},
    ),
    # 対象外の ML も特徴の振り分けは持つ (新しい特徴の見落とし検査のため)
    MlSpec(
        "alert_dedup_model.json",
        "src/cti/alert_dedup_gate.py",
        (
            _r(r"^p_pair$", "pair_judge", WEAK),
            _r(r"^(add|miss)_victim_org$", "article_summary", WEAK),
            _r(
                r"^(cos|hours_apart|same_feed|n_ent_|add_|miss_|num_|unit_num|new_state_|tier_"
                r"|primary_added)",
                "det",
                DET,
            ),
        ),
        {},
    ),
    MlSpec(
        "assign_model.json",
        "src/assessment/assign_features.py",
        (
            _r(r"^ct_", "synthesis_detect", WEAK),
            _r(r"^cos_", "embed", STRONG),
            _r(r"^(seed_missing|strong_|sit_|nation_share|token_share|rule_|age_days)", "det", DET),
        ),
        {},
    ),
    MlSpec(
        "assign_model_probe.json",
        "src/assessment/assign_features.py (FEATURE_NAMES_WITH_PROBE)",
        (
            _r(r"^probe_logit_", "assign_evidence_judge", STRONG),
            _r(r"^ct_", "synthesis_detect", WEAK),
            _r(r"^cos_", "embed", STRONG),
            _r(r"^(seed_missing|strong_|sit_|nation_share|token_share|rule_|age_days)", "det", DET),
        ),
        {"assign_evidence_judge": "probe_model"},
    ),
)

#: 全体が 1 つの入力のモデル (feature_names を持たない)
_WHOLE_MODEL: Final[Mapping[str, tuple[str, str]]] = {"relevance_jp_model.json": ("embed", STRONG)}


@dataclass(frozen=True)
class MlDependency:
    """ML 1 本 × step 1 つ分の依存。"""

    file: str
    step: str
    strength: str
    features: tuple[str, ...]
    trained_field: str | None
    #: 学習時に記録されたモデル名 (欄が無い・空なら空タプル = 検証できない)
    trained_models: tuple[str, ...]

    @property
    def is_s_derived(self) -> bool:
        return self.step in S_DERIVED_STEPS


@dataclass(frozen=True)
class Unmapped:
    file: str
    feature: str


@dataclass(frozen=True)
class MlDependencyReport:
    dependencies: tuple[MlDependency, ...]
    unmapped: tuple[Unmapped, ...]
    #: 規則表 (ML_SPECS) に行が無い JSON
    unknown_files: tuple[str, ...]

    def s_derived(self, file: str | None = None) -> tuple[MlDependency, ...]:
        return tuple(d for d in self.dependencies if d.is_s_derived and file in (None, d.file))

    def files_with_s_derived(self, *, strength: str | None = None) -> tuple[str, ...]:
        hit = {d.file for d in self.s_derived() if strength in (None, d.strength)}
        return tuple(sorted(hit))


@dataclass(frozen=True)
class Misalignment:
    """学習時の S が今の S と違う (または記録が無く確かめられない) 依存。"""

    file: str
    step: str
    reason: str  # "mismatch" | "unrecorded"
    trained_models: tuple[str, ...]


def _as_names(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if isinstance(raw, list):
        return tuple(str(x) for x in raw if x)
    return ()


def _spec_for(file: str) -> MlSpec | None:
    return next((s for s in ML_SPECS if s.file == file), None)


def _dependencies_of(
    spec: MlSpec, raw: Mapping[str, Any]
) -> tuple[list[MlDependency], list[Unmapped]]:
    groups: dict[tuple[str, str], list[str]] = {}
    unmapped: list[Unmapped] = []
    for name in raw.get("feature_names") or []:
        rule = next((r for r in spec.rules if re.search(r.pattern, name)), None)
        if rule is None:
            unmapped.append(Unmapped(spec.file, name))
        else:
            groups.setdefault((rule.step, rule.strength), []).append(name)
    deps = []
    for (step, strength), feats in sorted(groups.items()):
        if strength == DET:
            continue
        field = spec.trained_field.get(step)
        deps.append(
            MlDependency(
                spec.file,
                step,
                strength,
                tuple(feats),
                field,
                _as_names(raw.get(field)) if field else (),
            )
        )
    return deps, unmapped


def derive_dependencies(models_dir: Path) -> MlDependencyReport:
    """``models_dir`` の ML JSON を読み、S 由来の特徴の一覧を導く。"""
    deps: list[MlDependency] = []
    unmapped: list[Unmapped] = []
    unknown: list[str] = []
    for path in sorted(models_dir.glob("*.json")):
        name = path.name
        if name in SKIP_FILES or ".bak" in name or name.endswith(".staging"):
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        if name in _WHOLE_MODEL:
            step, strength = _WHOLE_MODEL[name]
            deps.append(MlDependency(name, step, strength, (), None, ()))
            continue
        spec = _spec_for(name)
        if spec is None:
            unknown.append(name)
            continue
        d, u = _dependencies_of(spec, raw)
        deps.extend(d)
        unmapped.extend(u)
    return MlDependencyReport(tuple(deps), tuple(unmapped), tuple(unknown))


def find_misaligned(report: MlDependencyReport, current_s: str) -> tuple[Misalignment, ...]:
    """学び直しの対象の ML のうち、強い S 由来の特徴を持つのに学習時の S が今の S でないもの。

    ``unrecorded`` = そもそも学習時のモデルを記録していない (event_kind を使う ML など)。
    切替の合否は記録の有無ではなく測定で決める (手順書 §4) ので、これは警告であって失敗ではない。
    """
    out: list[Misalignment] = []
    for d in report.s_derived():
        if d.file not in RETRAIN_TARGETS or d.strength != STRONG:
            continue
        if not d.trained_models:
            out.append(Misalignment(d.file, d.step, "unrecorded", ()))
        elif current_s not in d.trained_models:
            out.append(Misalignment(d.file, d.step, "mismatch", d.trained_models))
    return tuple(out)
