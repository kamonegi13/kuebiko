"""S26-ML-GOLD の決定論の部分 (層・割当・抽出・重み・窓・汚染の判定)。

仕様の SSoT は docs/replay_prereg/s26_ml_gold.md (§2 母集団、§4 標本と重み、§7 汚染)。
DB・LLM には触れない純粋関数のみ (テストは tests/unit/test_s26_ml_gold_core.py)。
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SEED = 20261106
WINDOW_START = datetime(2026, 10, 27, tzinfo=UTC)
WINDOW_END = datetime(2026, 11, 4, tzinfo=UTC)
TITLE_MIN = 8  # s26 の教師との見出しの部分一致に使う最短の字数 (make_guard_s26 と同じ)

#: 判定集 → 層の順と割当 (§4 の表)
ALLOCATION: dict[str, tuple[tuple[str, int], ...]] = {
    "detect": (("A", 24), ("B", 20), ("C", 16)),
    "sevc": (("H", 24), ("M", 20), ("L", 16)),
    "relation": (("V", 30), ("K", 15), ("S", 15)),
}
SEVC_HIGH = 0.50
SEVC_MID = 0.20

#: deep_dive の 7 窓 (終わり = 日曜 16:00 UTC、168 時間、§2.4)
DD_LOOKBACK = timedelta(hours=168)
DD_WINDOW_ENDS: tuple[datetime, ...] = tuple(
    datetime(2026, 11, 1, 16, tzinfo=UTC) + timedelta(days=7 * i) for i in range(7)
)
DD_POOL_MAX = 180
DD_INSIDE = 60  # 関門 A の内側 (順位 1〜60)


def dd_allocation(window_no: int) -> tuple[tuple[str, int], ...]:
    """窓 (1 始まり) の層の割当。窓 1〜4 は I 6 / O 3、窓 5〜7 は I 5 / O 3 (計 60)。"""
    if not 1 <= window_no <= len(DD_WINDOW_ENDS):
        raise ValueError(f"window out of range: {window_no}")
    return (("I", 6), ("O", 3)) if window_no <= 4 else (("I", 5), ("O", 3))


def dd_seed(window_no: int) -> int:
    return SEED + window_no * 10


def dd_stratum(rank: int) -> str:
    """母集団の順位 (1 始まり) の層。"""
    return "I" if rank <= DD_INSIDE else "O"


def in_window(ts: datetime, start: datetime = WINDOW_START, end: datetime = WINDOW_END) -> bool:
    return start <= ts < end


def detect_stratum(*, in_shadow: bool, importance: str) -> str | None:
    """detect の層 (§4)。high / medium 以外は母集団外 (None)。"""
    if importance not in ("high", "medium"):
        return None
    if in_shadow:
        return "A"
    return "B" if importance == "high" else "C"


def sevc_stratum(p_s3: float) -> str:
    if p_s3 >= SEVC_HIGH:
        return "H"
    return "M" if p_s3 >= SEVC_MID else "L"


def relation_stratum(
    *, shared_victims_or_cves: bool, shared_capability: bool, shared_subjects: bool
) -> str | None:
    """relation の層。どの珍しい指標も共有しない対は候補でない (None)。"""
    if shared_victims_or_cves:
        return "V"
    if shared_capability:
        return "K"
    return "S" if shared_subjects else None


def pair_key(a: str, b: str) -> str:
    """事象の対の鍵 (順序に依らない。学習の正解集と同じ 'x|y' の形)。"""
    x, y = sorted((a, b))
    return f"{x}|{y}"


def allocate(available: Mapping[str, int], plan: Sequence[tuple[str, int]]) -> dict[str, int]:
    """層ごとの抜き出し件数 (§4)。不足分は表の次の層へ、最後の層の不足は最初の層へ 1 周だけ回す。

    どの層も母集団を超えて取らない。計は割当の合計を超えない。
    """
    names = [n for n, _ in plan]
    avail = {n: max(0, int(available.get(n, 0))) for n in names}
    take = {n: min(avail[n], want) for n, want in plan}
    carry = sum(want for _, want in plan) - sum(take.values())
    if carry <= 0:
        return take
    order = names[1:] + names[:1]
    for n in order:
        room = avail[n] - take[n]
        add = min(room, carry)
        take[n] += add
        carry -= add
        if carry == 0:
            break
    return take


def draw(
    pools: Mapping[str, Iterable[str]],
    targets: Mapping[str, int],
    plan: Sequence[tuple[str, int]],
    seed: int,
) -> dict[str, list[str]]:
    """層ごとに id 昇順 → Random(seed + 層の通し番号) で shuffle → 先頭から targets 件。"""
    out: dict[str, list[str]] = {}
    for i, (name, _) in enumerate(plan):
        pool = sorted(set(pools.get(name, ())))
        random.Random(seed + i).shuffle(pool)
        out[name] = pool[: targets.get(name, 0)]
    return out


def weights(population: Mapping[str, int], sample: Mapping[str, int]) -> dict[str, float]:
    """層 h の重み = N_h / n_h。標本が空の層は含めない。"""
    return {h: population[h] / n for h, n in sample.items() if n > 0 and h in population}


def contaminated(
    targets: Iterable[str], component_of: Mapping[str, str], train_ids: Iterable[str]
) -> set[str]:
    """targets のうち、学習側の記事と同じ成分に入る記事 (成分は割らない)。"""
    train_comp = {component_of.get(a, a) for a in train_ids}
    return {a for a in targets if component_of.get(a, a) in train_comp}


def group_contaminated(members: Iterable[str], dirty: set[str]) -> bool:
    """対・事象の構成記事のどれかが汚染していれば True。"""
    return any(a in dirty for a in members)


@dataclass(frozen=True)
class TeacherIndex:
    """s26 の学習データの本文 (部分一致の照合用)。"""

    text: str

    def hits(self, *, article_id: str, url: str, title: str) -> bool:
        keys = [article_id, url]
        t = (title or "").strip()
        if len(t) >= TITLE_MIN:
            keys.append(t)
        return any(k and k in self.text for k in keys)


def url_of(article_id: str) -> str:
    """'rss:<url>' 形の記事 id の URL (それ以外は空)。"""
    head, _, rest = article_id.partition(":")
    return rest if head == "rss" and rest.startswith("http") else ""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def jsonl_text(rows: Iterable[Mapping[str, object]]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
