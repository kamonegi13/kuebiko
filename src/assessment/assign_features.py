"""台帳の割当判定 ML の特徴量 (2026-09-24)。**学習と本番が同じ関数を使う (SSoT)**。

学習データ (Opus 盲検の組) の特徴量もこの関数で作り直す — 学習と本番で計算が食い違うと、
木が別の意味の値を読んで静かに誤判定する (群化 ML の列ずれと同型)。
"""

from __future__ import annotations

from src.assessment.assignment import STRONG_ANCHOR_TYPES, ArticleKeys, SituationKeys, _score

_STRONG_TYPES: tuple[str, ...] = tuple(sorted(STRONG_ANCHOR_TYPES))
_RULES: tuple[str, ...] = ("anchor", "nation", "token")
_CLAIM_TYPES: tuple[str, ...] = ("discrete_event", "ongoing_activity", "structural")

#: 特徴量の並び (モデルの JSON と照合する)
FEATURE_NAMES: tuple[str, ...] = (
    "cos_title",
    "cos_seed",
    "seed_missing",
    *(f"strong_{t}" for t in _STRONG_TYPES),
    "strong_total",
    "sit_strong_size",
    "nation_share",
    "token_share",
    "sit_tokens",
    *(f"rule_{r}" for r in _RULES),
    "age_days",
    *(f"ct_{c}" for c in _CLAIM_TYPES),
)


def _count_type(keys: frozenset[str], type_: str) -> float:
    return float(sum(1 for k in keys if k.startswith(f"{type_}:")))


def assign_feature_vector(
    art: ArticleKeys,
    sit: SituationKeys,
    *,
    cos_title: float,
    cos_seed: float | None,
    age_days: float,
) -> list[float]:
    """(記事, 情勢) の組の特徴量。

    cos_title = 記事の要約埋込 × 情勢の題名、cos_seed = 記事 × 開設時の記事 (seed) の平均。
    開設時の記事が無ければ cos_title で代用し ``seed_missing`` を立てる (学習時と同じ扱い)。
    age_days = 記事の日時 − 情勢の開設 (日)。
    """
    shared = art.strong & sit.strong
    scored = _score(art, sit)
    rule = scored[1] if scored is not None else ""
    values: dict[str, float] = {
        "cos_title": cos_title,
        "cos_seed": cos_title if cos_seed is None else cos_seed,
        "seed_missing": float(cos_seed is None),
        **{f"strong_{t}": _count_type(shared, t) for t in _STRONG_TYPES},
        "strong_total": float(len(shared)),
        "sit_strong_size": float(len(sit.strong)),
        "nation_share": float(len(art.nations & sit.nations)),
        "token_share": float(len(art.tokens & sit.tokens)),
        "sit_tokens": float(len(sit.tokens)),
        **{f"rule_{r}": float(rule == r) for r in _RULES},
        "age_days": age_days,
        **{f"ct_{c}": float(sit.claim_type == c) for c in _CLAIM_TYPES},
    }
    return [values[n] for n in FEATURE_NAMES]
