"""THIN-GOLD の決定論の部分 (帯分け・不足時の規則・抽出・重み・汚染の除外)。

仕様の SSoT は docs/replay_prereg/thin_body_s.md (§4 標本と重み、§7 汚染の鍵)。
DB・LLM には触れない純粋関数のみ (テストは tests/unit/test_thin_gold_core.py)。
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

SEED = 20261103
PER_BAND = 50
MAX_BODY_CHARS = 600  # 2 段目の対象 (THIN_BODY_PROBE_MAX_BODY_CHARS) と同じ上限 (未満)

#: 短い帯から順。(名前, 下限 (以上), 上限 (未満))
BANDS: tuple[tuple[str, int, int], ...] = (
    ("lt300", 1, 300),
    ("300_450", 300, 450),
    ("450_600", 450, MAX_BODY_CHARS),
)
BAND_NAMES: tuple[str, ...] = tuple(b[0] for b in BANDS)
LOW, MID, HIGH = BAND_NAMES

GATE_POSTED = "posted"
GATE_PROBE_REJECTED = "probe_rejected"
PROBE_FAILURE_REASON = "degenerate_body: thin_body_probe"


def band_of(body_chars: int) -> str | None:
    """len(body.strip()) の帯。対象外 (0 以下・600 以上) は None。"""
    for name, lo, hi in BANDS:
        if lo <= body_chars < hi:
            return name
    return None


def gate_status_of(status: str | None, failure_reason: str | None) -> str | None:
    """prereg §2 の状態。posted でも 2 段目の落ちでもなければ None (母集団外)。"""
    if failure_reason == PROBE_FAILURE_REASON:
        return GATE_PROBE_REJECTED
    if status == "posted":
        return GATE_POSTED
    return None


def allocate_targets(available: Mapping[str, int], per_band: int = PER_BAND) -> dict[str, int]:
    """各帯の抜き出し件数 (§4 の不足時の規則)。

    <300 の不足は 300-450 へ、300-450 の不足は 450-600 へ、450-600 の不足は 300-450 へ足す。
    順に <300 -> 300-450 -> 450-600 と繰り上げ、最後の 450-600 の不足は 300-450 の余りに戻す。
    どの帯も適格な件数を超えて取らない。計は per_band * 3 を超えない。
    """
    avail = {b: max(0, int(available.get(b, 0))) for b in BAND_NAMES}
    low = min(avail[LOW], per_band)
    mid_target = per_band + (per_band - low)
    mid = min(avail[MID], mid_target)
    high_target = per_band + (mid_target - mid)
    high = min(avail[HIGH], high_target)
    high_deficit = high_target - high
    if high_deficit > 0:
        mid = min(avail[MID], mid + high_deficit)
    total_cap = per_band * len(BANDS)
    out = {LOW: low, MID: mid, HIGH: high}
    excess = sum(out.values()) - total_cap
    if excess > 0:  # 規則どおりなら起きないが、上限を守る保険
        out[MID] -= excess
    return out


def draw_sample(
    candidates: Mapping[str, Sequence[str]],
    targets: Mapping[str, int],
    seed: int = SEED,
) -> dict[str, list[str]]:
    """帯ごとに article_id 昇順 -> 同じ乱数の列で (短い帯から) shuffle -> 先頭から targets 件。"""
    rng = random.Random(seed)
    out: dict[str, list[str]] = {}
    for band in BAND_NAMES:
        pool = sorted(set(candidates.get(band, ())))
        rng.shuffle(pool)
        out[band] = pool[: targets.get(band, 0)]
    return out


def band_weights(population: Mapping[str, int], sample: Mapping[str, int]) -> dict[str, float]:
    """帯 h の重み = N_h / n_h (母集団の件数 / 標本の件数)。標本が空の帯は含めない。"""
    return {
        b: population[b] / sample[b] for b in BAND_NAMES if sample.get(b, 0) > 0 and b in population
    }


@dataclass(frozen=True)
class ContaminationResult:
    kept: frozenset[str]
    excluded: frozenset[str]


def split_contaminated(
    population: Sequence[str],
    component_of: Mapping[str, str],
    train_ids: frozenset[str],
) -> ContaminationResult:
    """学習・調整側の記事と同じ成分に入る母集団の記事を除く (§7、成分は割らない)。"""
    train_components = {component_of.get(a, a) for a in train_ids}
    excluded = frozenset(a for a in population if component_of.get(a, a) in train_components)
    return ContaminationResult(frozenset(population) - excluded, excluded)


def apply_hub_cut(values: Mapping[str, frozenset[str]], cap: int) -> dict[str, frozenset[str]]:
    """欄の値が cap を超える記事は、その欄を鍵にしない (RP-9 と同じハブの切り方)。"""
    return {a: v for a, v in values.items() if 0 < len(v) <= cap}
