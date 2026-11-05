"""GEO-LEADLAG-MONTHLY の決定論の部品 (系列の組み立て・山・置換検定)。

仕様の SSoT は docs/replay_prereg/geo_leadlag_monthly.md。DB も乱数の副作用も持たない
(bootstrap は種つきの numpy Generator、置換は全ずらしの列挙で乱数なし)。
集合 (カテゴリ・同盟国) は prereg の字面で固定し、コードの定数を参照しない (prereg §2)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

# --- prereg §2: 字面で固定する集合 ---
GEO_CATEGORIES = frozenset({"geopolitical", "policy"})
CYBER_CATEGORIES = frozenset(
    {"breach", "incident", "apt", "apt_leak", "malware", "phishing", "ransomware", "vulnerability"}
)
ADVERSARIES = ("CN", "RU", "KP", "IR")
ALLIES = frozenset({"US", "GB", "AU", "CA", "NZ", "KR", "TW", "PH"})
JA = frozenset({"JP"}) | ALLIES
MOTIVE_INTENTS = frozenset({"espionage", "disruption"})
WEAK_CONFIDENCE = frozenset({"", "low"})

# --- prereg §3 / §4 の定数 ---
MIN_GEO_PER_DAY = 10
MIN_CYBER_PER_DAY = 20
ONSET_PERCENTILE = 90.0
ONSET_MIN_NUMERATOR = 2
PRE_DAYS = 14
WINDOWS: Mapping[str, tuple[int, int]] = {"W1": (1, 7), "W2": (8, 14)}
MIN_SHIFT = 30
BOOTSTRAP_ITERATIONS = 2000
BOOTSTRAP_SEED = 20261201
MIN_PAIRS = 20
MIN_CYBER_HITS = 60
MIN_REGIME_DAYS = 10
CONFIRMATORY_START = date(2026, 10, 29)


def _geo_key(x: str) -> str:
    return f"G_{x}"


def _c1_key(x: str) -> str:
    return f"C1_{x}"


COLUMNS: tuple[str, ...] = (
    "G",
    *(_geo_key(x) for x in ADVERSARIES),
    "G_ADVJA",
    "G_RUUA",
    "T",
    *(_c1_key(x) for x in ADVERSARIES),
    "C2",
    "C3",
)


@dataclass(frozen=True)
class ArticleFacts:
    """1 記事の分類に要る属性 (本文なし)。国は大文字 ISO2。"""

    day: date
    category: str
    parties: frozenset[str]
    victim_iso: str
    subject_nations: frozenset[str]  # 信頼条件を満たす国家系の主題の国
    has_hacktivist_subject: bool  # 信頼条件を満たす主題にハクティビストを含む
    intent: str
    intent_confidence: str


def _confident(value: str) -> bool:
    return value.strip().lower() not in WEAK_CONFIDENCE


def article_columns(a: ArticleFacts) -> frozenset[str]:
    """記事が加算される列 (prereg §3 の表)。"""
    cols: set[str] = set()
    if a.category in GEO_CATEGORIES:
        cols.add("G")
        cols.update(_geo_key(x) for x in ADVERSARIES if x in a.parties)
        if a.parties & set(ADVERSARIES) and a.parties & JA:
            cols.add("G_ADVJA")
        if a.parties & {"RU", "UA"}:
            cols.add("G_RUUA")
    elif a.category in CYBER_CATEGORIES:
        cols.add("T")
        cols.update(_c1_key(x) for x in ADVERSARIES if x in a.subject_nations)
        intent = a.intent.strip().lower()
        confident = _confident(a.intent_confidence)
        motive = intent in MOTIVE_INTENTS and confident
        if a.victim_iso in JA and (motive or a.subject_nations & set(ADVERSARIES)):
            cols.add("C2")
        if a.has_hacktivist_subject or (intent == "hacktivism" and confident):
            cols.add("C3")
    return frozenset(cols)


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        raise ValueError("end before start")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def build_counts(days: Sequence[date], facts: Sequence[ArticleFacts]) -> list[dict[str, int]]:
    """日ごとの列の件数 (範囲外の日の記事は数えない)。"""
    index = {d: i for i, d in enumerate(days)}
    out = [dict.fromkeys(COLUMNS, 0) for _ in days]
    for a in facts:
        i = index.get(a.day)
        if i is None:
            continue
        for col in article_columns(a):
            out[i][col] += 1
    return out


def ratio_series(
    counts: Sequence[Mapping[str, int]], num_key: str, den_key: str, min_den: int
) -> list[float | None]:
    """割合の系列。分母が下限未満の日は欠測 (None)。"""
    return [(c[num_key] / c[den_key]) if c[den_key] >= min_den else None for c in counts]


def missing_days(days: Sequence[date], counts: Sequence[Mapping[str, int]]) -> dict[str, list[str]]:
    """欠測の日 (山の元 G<10 / 結果 T<20)。"""

    def below(key: str, floor: int) -> list[str]:
        return [d.isoformat() for d, c in zip(days, counts, strict=True) if c[key] < floor]

    return {
        "geo_missing": below("G", MIN_GEO_PER_DAY),
        "cyber_missing": below("T", MIN_CYBER_PER_DAY),
    }


# --- グループ (閾値を計算する区間) ---
def month_groups(days: Sequence[date], start: date = CONFIRMATORY_START) -> list[list[int]]:
    """暦月 (JST) ごとの日の添字。開始が月初でないとき、開始月は次の月と合わせて 1 区間 (§3)。"""
    keys: list[tuple[int, int]] = []
    for d in days:
        y, m = d.year, d.month
        if start.day != 1 and (y, m) == (start.year, start.month):
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        keys.append((y, m))
    return _runs(keys)


def _runs(keys: Sequence[object]) -> list[list[int]]:
    groups: list[list[int]] = []
    for i, k in enumerate(keys):
        if groups and keys[groups[-1][0]] == k:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


def regime_groups(
    days: Sequence[date], breaks: Sequence[date], start: date = CONFIRMATORY_START
) -> list[list[int]]:
    """暦月 ∩ 体制の区間。10 日未満の区間は後ろへ (後ろが無ければ前へ) 合わせる (§5)。"""
    month_key = {i: k for k, g in enumerate(month_groups(days, start)) for i in g}
    sorted_breaks = sorted(breaks)
    keys = [(month_key[i], sum(1 for b in sorted_breaks if b <= d)) for i, d in enumerate(days)]
    groups = _runs(keys)
    merged: list[list[int]] = []
    carry: list[int] = []
    for g in groups:
        current = carry + g
        carry = []
        if len(current) < MIN_REGIME_DAYS:
            carry = current
        else:
            merged.append(current)
    if carry:
        if merged:
            merged[-1].extend(carry)
        else:
            merged.append(carry)
    return merged


def onset_flags(
    num: Sequence[int],
    den: Sequence[int],
    groups: Sequence[Sequence[int]],
) -> list[bool]:
    """山に入った日 (§3)。割合 s = num/den (den<10 は欠測)。"""
    n = len(num)
    cond = [False] * n
    for g in groups:
        s = {i: num[i] / den[i] for i in g if den[i] >= MIN_GEO_PER_DAY}
        if len(g) - len(s) > len(g) / 2 or not s:
            continue  # 欠測が区間の半分を超える月は山を置かない
        threshold = float(np.percentile(list(s.values()), ONSET_PERCENTILE))
        for i, value in s.items():
            cond[i] = value >= threshold and num[i] >= ONSET_MIN_NUMERATOR
    return [cond[i] and not (i > 0 and cond[i - 1]) for i in range(n)]


# --- 統計量 ---
@dataclass(frozen=True)
class Pair:
    diff: float
    pre: float


def _mean_used(values: Sequence[float | None]) -> float | None:
    used = [v for v in values if v is not None]
    if len(used) * 2 < len(values) or not used:
        return None  # 使える日が半分未満
    return sum(used) / len(used)


def usable_pairs(
    onset: Sequence[bool], r: Sequence[float | None], window: tuple[int, int]
) -> list[Pair]:
    """山の日 t ごとの (post - pre, pre)。範囲外・使える日が半分未満の組は捨てる (§4)。"""
    a, b = window
    n = len(r)
    out: list[Pair] = []
    for t, is_onset in enumerate(onset):
        if not is_onset or t - PRE_DAYS < 0 or t + b > n - 1:
            continue
        pre = _mean_used(r[t - PRE_DAYS : t])
        post = _mean_used(r[t + a : t + b + 1])
        if pre is None or post is None:
            continue
        out.append(Pair(post - pre, pre))
    return out


def effect(pairs: Sequence[Pair]) -> tuple[float, float | None] | None:
    """(E, R)。R は pre の平均が 0 のとき None。組が無ければ None。"""
    if not pairs:
        return None
    e = sum(p.diff for p in pairs) / len(pairs)
    mean_pre = sum(p.pre for p in pairs) / len(pairs)
    return e, (e / mean_pre if mean_pre > 0 else None)


@dataclass(frozen=True)
class Component:
    """1 本の (山の元, 結果) の系列の組。H1 は国ごとに 1 つ。"""

    name: str
    num_key: str
    r_key: str


HYPOTHESES: Mapping[str, tuple[Component, ...]] = {
    "H1": tuple(Component(x, _geo_key(x), _c1_key(x)) for x in ADVERSARIES),
    "H2": (Component("ADVJA", "G_ADVJA", "C2"),),
    "H3": (Component("RUUA", "G_RUUA", "C3"),),
}


@dataclass(frozen=True)
class TestResult:
    hypothesis: str
    window: str
    insufficient: bool
    n_pairs: int
    cyber_hits: int
    effect: float | None = None
    relative: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    p_raw: float | None = None
    p_holm: float | None = None


def _pairs_for(
    comps: Sequence[Component],
    onsets: Mapping[str, Sequence[bool]],
    rs: Mapping[str, Sequence[float | None]],
    window: tuple[int, int],
    shift: int = 0,
) -> list[Pair]:
    pairs: list[Pair] = []
    for c in comps:
        flags = np.roll(np.array(onsets[c.name], dtype=bool), shift).tolist()
        pairs.extend(usable_pairs(flags, rs[c.r_key], window))
    return pairs


def bootstrap_relative_ci(pairs: Sequence[Pair]) -> tuple[float, float] | None:
    """R の 95% 百分位区間 (組の復元抽出 2,000 回、種 20261201)。"""
    diffs = np.array([p.diff for p in pairs])
    pres = np.array([p.pre for p in pairs])
    if len(pairs) == 0:
        return None
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    idx = rng.integers(0, len(pairs), size=(BOOTSTRAP_ITERATIONS, len(pairs)))
    sum_pre = pres[idx].sum(axis=1)
    valid = sum_pre > 0
    if not valid.any():
        return None
    rel = diffs[idx].sum(axis=1)[valid] / sum_pre[valid]
    low, high = np.percentile(rel, [2.5, 97.5])
    return float(low), float(high)


def holm_adjust(p_values: Mapping[str, float]) -> dict[str, float]:
    """Holm の補正後 p (単調化・1 で頭打ち)。"""
    order = sorted(p_values, key=lambda k: p_values[k])
    m = len(order)
    out: dict[str, float] = {}
    running = 0.0
    for rank, key in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p_values[key]))
        out[key] = running
    return out


def run_test(
    hypothesis: str,
    window_name: str,
    counts: Sequence[Mapping[str, int]],
    groups: Sequence[Sequence[int]],
) -> TestResult:
    """1 つの検定 (観測 + 全ずらしの置換 + bootstrap)。"""
    comps = HYPOTHESES[hypothesis]
    window = WINDOWS[window_name]
    n = len(counts)
    den = [c["G"] for c in counts]
    onsets = {c.name: onset_flags([x[c.num_key] for x in counts], den, groups) for c in comps}
    rs = {
        c.r_key: ratio_series(counts, c.r_key, "T", MIN_CYBER_PER_DAY) for c in comps
    }  # fmt: skip
    hits = sum(sum(x[k] for x in counts) for k in {c.r_key for c in comps})
    pairs = _pairs_for(comps, onsets, rs, window)
    n_shifts = n - 2 * MIN_SHIFT + 1
    if len(pairs) < MIN_PAIRS or hits < MIN_CYBER_HITS or n_shifts < 1:
        return TestResult(hypothesis, window_name, True, len(pairs), hits)
    observed = effect(pairs)
    assert observed is not None  # len(pairs) >= MIN_PAIRS
    ge = 0
    for s in range(MIN_SHIFT, n - MIN_SHIFT + 1):
        shifted = effect(_pairs_for(comps, onsets, rs, window, shift=s))
        if shifted is not None and shifted[0] >= observed[0]:
            ge += 1
    ci = bootstrap_relative_ci(pairs)
    return TestResult(
        hypothesis,
        window_name,
        False,
        len(pairs),
        hits,
        effect=observed[0],
        relative=observed[1],
        ci_low=ci[0] if ci else None,
        ci_high=ci[1] if ci else None,
        p_raw=(1 + ge) / (1 + n_shifts),
    )


def run_primary_family(
    counts: Sequence[Mapping[str, int]], groups: Sequence[Sequence[int]]
) -> list[TestResult]:
    """主の族 6 検定 (3 仮説 × 2 窓)。Holm は標本不足でない検定の中だけ。"""
    results = [run_test(h, w, counts, groups) for h in HYPOTHESES for w in WINDOWS]
    keyed = {f"{r.hypothesis}/{r.window}": r.p_raw for r in results if r.p_raw is not None}
    holm = holm_adjust(keyed)
    return [
        TestResult(**{**r.__dict__, "p_holm": holm.get(f"{r.hypothesis}/{r.window}")})
        for r in results
    ]
