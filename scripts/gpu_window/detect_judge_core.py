"""DETECT-JUDGE の裁定と採点の純粋部分 (仕様: docs/replay_prereg/detect_decompose.md)。

I/O も Opus も持たない (決定論でテストできる)。標準ライブラリのみ。
"""

from __future__ import annotations

import hashlib
import math
import os
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PROFILE_ENV = "DETECT_GPU_PROFILE"
#: 前回 (decompose) の乱数種と、再判定 (rejudge、prereg §4) の乱数種
SEEDS = {"decompose": 20261104, "rejudge": 20261105}
PROFILE_NAME = os.environ.get(PROFILE_ENV, "decompose").strip() or "decompose"
if PROFILE_NAME not in SEEDS:
    raise SystemExit(f"{PROFILE_ENV}={PROFILE_NAME!r} は未知 (decompose / rejudge)")
SEED = SEEDS[PROFILE_NAME]
MAX_EXCLUDED_SHARE = 0.50  # rejudge §6: R18 で外れる単位が U のこれを超えたら判定不能
SAMPLE_TOTAL = 260
SECOND_TOTAL = 40
BOOTSTRAP_ITERATIONS = 2000
MIN_DETERMINABLE_SHARE = 0.80
MIN_WINDOWS = 18
MIN_P3_DENOMINATOR = 30
P1_LOWER_FLOOR = -0.5
P2_UPPER_CEIL = 0.05
P3_CEIL = 0.20
P4_FLOOR = 0.90
P5_CEIL = 1.5
ALPHA = 0.05

CORRECT = "correct"
WRONG = "wrong"
UNDECIDED = "undecided"


# ---- 単位 ---------------------------------------------------------------------------------


def unit_key(
    window_id: str, article_ids: Sequence[str], claim: str
) -> tuple[str, tuple[str, ...], str]:
    return (window_id, tuple(sorted(article_ids)), claim)


def unit_id(window_id: str, article_ids: Sequence[str], claim: str) -> str:
    """畳んだ単位の id (鍵の sha256 先頭 16 桁)。記事 id は集合として扱う。"""
    w, ids, c = unit_key(window_id, article_ids, claim)
    blob = "\x1f".join([w, "\x1e".join(ids), c])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Unit:
    uid: str
    window_id: str
    article_ids: tuple[str, ...]
    claim: str
    arms: tuple[str, ...]

    @property
    def stratum(self) -> str:
        return "+".join(self.arms)


def collect_units(records: Sequence[Mapping[str, Any]]) -> dict[str, Unit]:
    """(窓, 記事集合, claim) で畳む。単位の arms = その単位を開いた腕の集合。"""
    arms_of: dict[str, set[str]] = defaultdict(set)
    meta: dict[str, tuple[str, tuple[str, ...], str]] = {}
    for rec in records:
        if rec.get("fallback"):
            continue
        for o in rec.get("opened", []):
            k = unit_key(rec["window_id"], o["article_ids"], o["claim"])
            uid = unit_id(*k)
            arms_of[uid].add(rec["arm"])
            meta[uid] = k
    return {
        uid: Unit(uid, meta[uid][0], meta[uid][1], meta[uid][2], tuple(sorted(arms)))
        for uid, arms in arms_of.items()
    }


def excluded_by_closure_plan(
    units: Mapping[str, Unit], contaminated: frozenset[str], universe: frozenset[str]
) -> set[str]:
    """rejudge §7.2: 汚染記事を含む単位、または凍結の母集合の外の記事を含む単位 (保守側) を除く。

    閉包は凍結時に closure_plan.json へ固定済みで、ここでは計算し直さない。
    """
    return {
        uid
        for uid, u in units.items()
        if any(a in contaminated or a not in universe for a in u.article_ids)
    }


# ---- 抽出 ---------------------------------------------------------------------------------


def allocate(sizes: Mapping[str, int], total: int) -> dict[str, int]:
    """最大剰余法の比例配分。層の大きさを超えない。"""
    n_all = sum(sizes.values())
    if n_all <= total:
        return dict(sizes)
    exact = {s: total * n / n_all for s, n in sizes.items()}
    out = {s: min(sizes[s], math.floor(v)) for s, v in exact.items()}
    left = total - sum(out.values())
    for s in sorted(exact, key=lambda x: (-(exact[x] - math.floor(exact[x])), x)):
        if left <= 0:
            break
        if out[s] < sizes[s]:
            out[s] += 1
            left -= 1
    return out


def sample_units(
    units: Mapping[str, Unit], *, total: int = SAMPLE_TOTAL, seed: int = SEED
) -> tuple[list[str], dict[str, float]]:
    """U ≤ total なら全数。そうでなければ層ごとに比例配分し shuffle 順の先頭から取る。

    戻り: (抽出した uid の列, 層 → 重み = 層の U / 抽出数)。
    """
    ordered = sorted(units)
    random.Random(seed).shuffle(ordered)
    by_stratum: dict[str, list[str]] = defaultdict(list)
    for uid in ordered:
        by_stratum[units[uid].stratum].append(uid)
    quota = allocate({s: len(v) for s, v in by_stratum.items()}, total)
    picked: list[str] = []
    weights: dict[str, float] = {}
    for s in sorted(by_stratum):
        take = by_stratum[s][: quota[s]]
        picked.extend(take)
        weights[s] = len(by_stratum[s]) / len(take) if take else 0.0
    return picked, weights


def pick_second(sampled: Sequence[str], *, n: int = SECOND_TOTAL, seed: int = SEED) -> list[str]:
    pool = sorted(sampled)
    return sorted(random.Random(seed).sample(pool, min(n, len(pool))))


def shuffled_order(uids: Sequence[str], *, seed: int = SEED + 1) -> list[str]:
    """裁定の提示順 (盲検: 腕・層の並びを残さない)。"""
    out = sorted(uids)
    random.Random(seed).shuffle(out)
    return out


# ---- 裁定の合成 -----------------------------------------------------------------------------


def combine(q1: str, q2: str, q3: str) -> str:
    """1 つでも no → 誤り。no が無く 1 つでも undecidable → 判断不能。全部 yes → 正しい。"""
    qs = (q1, q2, q3)
    if "no" in qs:
        return WRONG
    if all(q == "yes" for q in qs):
        return CORRECT
    return UNDECIDED


def cohen_kappa(a: Sequence[str], b: Sequence[str]) -> float | None:
    if not a or len(a) != len(b):
        return None
    n = len(a)
    po = sum(x == y for x, y in zip(a, b, strict=True)) / n
    labels = set(a) | set(b)
    pe = sum((a.count(lab) / n) * (b.count(lab) / n) for lab in labels)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else None
    return (po - pe) / (1 - pe)


# ---- 窓 × 腕の値 -----------------------------------------------------------------------------


def arm_rates(
    units: Mapping[str, Unit], sampled: Sequence[str], verdicts: Mapping[str, str]
) -> dict[str, tuple[float, float]]:
    """腕ごとの (P(正しい), P(誤り)) = 抽出した単位に対する割合 (判断不能・失敗は分母に残す)。"""
    cnt: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for uid in sampled:
        for arm in units[uid].arms:
            cnt[arm][2] += 1
            v = verdicts.get(uid)
            if v == CORRECT:
                cnt[arm][0] += 1
            elif v == WRONG:
                cnt[arm][1] += 1
    return {a: (c[0] / c[2], c[1] / c[2]) for a, c in cnt.items() if c[2]}


def window_arm_values(
    records: Sequence[Mapping[str, Any]],
    units: Mapping[str, Unit],
    verdicts: Mapping[str, str],
    sampled: set[str],
    rates: Mapping[str, tuple[float, float]],
    excluded: set[str],
) -> dict[tuple[str, str], tuple[float, float]]:
    """(窓, 腕) → (C, W): 3 回の run の平均。フォールバックの run は除く。

    抽出した単位は裁定値 (判断不能は 0/0)、抜かれなかった単位は腕の割合で補う。R18 除外は 0/0。
    """
    per_run: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for rec in records:
        if rec.get("fallback"):
            continue
        c = w = 0.0
        for o in rec.get("opened", []):
            uid = unit_id(rec["window_id"], o["article_ids"], o["claim"])
            if uid in excluded:
                continue
            if uid in sampled:
                v = verdicts.get(uid)
                c += v == CORRECT
                w += v == WRONG
            else:
                pc, pw = rates.get(rec["arm"], (0.0, 0.0))
                c += pc
                w += pw
        per_run[(rec["window_id"], rec["arm"])].append((c, w))
    return {
        k: (sum(x[0] for x in v) / len(v), sum(x[1] for x in v) / len(v))
        for k, v in per_run.items()
    }


# ---- 検定 -----------------------------------------------------------------------------------


def sign_test_p(diffs: Sequence[float]) -> float:
    """両側の符号検定 (引き分けを除く、厳密)。"""
    nz = [d for d in diffs if d != 0]
    n = len(nz)
    if n == 0:
        return 1.0
    k = min(sum(d > 0 for d in nz), sum(d < 0 for d in nz))
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * tail))


def wilcoxon_p(diffs: Sequence[float]) -> float:
    """両側の Wilcoxon 符号順位検定 (引き分けを除く・平均順位・厳密)。

    順位を 2 倍した整数の部分和の分布を DP で数える。
    """
    nz = [d for d in diffs if d != 0]
    n = len(nz)
    if n == 0:
        return 1.0
    order = sorted(range(n), key=lambda i: abs(nz[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(nz[order[j + 1]]) == abs(nz[order[i]]):
            j += 1
        avg = (i + j) / 2 + 1
        for t in range(i, j + 1):
            ranks[order[t]] = avg
        i = j + 1
    r2 = [round(2 * r) for r in ranks]
    total = sum(r2)
    ways = [0] * (total + 1)
    ways[0] = 1
    for r in r2:
        for s in range(total, r - 1, -1):
            ways[s] += ways[s - r]
    w_plus = sum(r for r, d in zip(r2, nz, strict=True) if d > 0)
    w_obs = min(w_plus, total - w_plus)
    tail = sum(ways[: w_obs + 1]) / 2**n
    return float(min(1.0, 2 * tail))


def percentile_interval(draws: Sequence[float], confidence: float = 0.95) -> tuple[float, float]:
    s = sorted(draws)
    alpha = (1 - confidence) / 2
    return s[math.floor(alpha * (len(s) - 1))], s[math.ceil((1 - alpha) * (len(s) - 1))]


def _false_rate(rows: Sequence[tuple[float, float]]) -> float | None:
    c = sum(r[0] for r in rows)
    w = sum(r[1] for r in rows)
    return w / (c + w) if c + w > 0 else None


def analyze(
    values: Mapping[tuple[str, str], tuple[float, float]],
    *,
    seed: int = SEED,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    """窓を cluster とする主指標 (P1) と誤開設率 (P2)。両腕そろった窓だけを対にする。"""
    wins = sorted({w for (w, a) in values if (w, "current") in values and (w, "ship") in values})
    cur = [values[(w, "current")] for w in wins]
    shp = [values[(w, "ship")] for w in wins]
    diffs = [(s[0] - s[1]) - (c[0] - c[1]) for c, s in zip(cur, shp, strict=True)]
    n = len(wins)
    if n == 0:
        return {"n_windows": 0}
    rng = random.Random(seed)
    mean_draws: list[float] = []
    rate_diff_draws: list[float] = []
    for _ in range(iterations):
        idx = [rng.randrange(n) for _ in range(n)]
        mean_draws.append(sum(diffs[i] for i in idx) / n)
        rc = _false_rate([cur[i] for i in idx])
        rs = _false_rate([shp[i] for i in idx])
        if rc is not None and rs is not None:
            rate_diff_draws.append(rs - rc)
    mean_d = sum(diffs) / n
    median_d = sorted(diffs)[n // 2] if n % 2 else sum(sorted(diffs)[n // 2 - 1 : n // 2 + 1]) / 2
    rc0, rs0 = _false_rate(cur), _false_rate(shp)
    out: dict[str, Any] = {
        "n_windows": n,
        "windows": wins,
        "d_w": diffs,
        "mean_d": mean_d,
        "median_d": median_d,
        "mean_d_ci": percentile_interval(mean_draws),
        "wilcoxon_p": wilcoxon_p(diffs),
        "sign_p": sign_test_p(diffs),
        "n_pos": sum(d > 0 for d in diffs),
        "n_neg": sum(d < 0 for d in diffs),
        "n_tie": sum(d == 0 for d in diffs),
        "current": {"C": sum(c[0] for c in cur), "W": sum(c[1] for c in cur), "false_rate": rc0},
        "ship": {"C": sum(s[0] for s in shp), "W": sum(s[1] for s in shp), "false_rate": rs0},
    }
    if rc0 is not None and rs0 is not None and rate_diff_draws:
        out["false_rate_diff"] = rs0 - rc0
        out["false_rate_diff_ci"] = percentile_interval(rate_diff_draws)
    return out


# ---- 合否 -----------------------------------------------------------------------------------


def p3_share(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """出荷腕の全 run: n_gate_blocked / (n_opened + n_gate_blocked)。"""
    ship = [r for r in records if r["arm"] == "ship"]
    blocked = sum(int(r.get("n_gate_blocked", 0)) for r in ship)
    opened = sum(int(r.get("n_opened", 0)) for r in ship)
    den = blocked + opened
    return {
        "blocked": blocked,
        "opened": opened,
        "denominator": den,
        "share": blocked / den if den else None,
        # 参考 (判定に使わない、rejudge §6 P3): 書き手の直し・単発の塊で止めた分
        "fidelity_blocked": sum(int(r.get("n_fidelity_blocked", 0)) for r in ship),
        "one_off_skipped": sum(int(r.get("n_one_off_skipped", 0)) for r in ship),
    }


def p4_share(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ship = [r for r in records if r["arm"] == "ship"]
    applied = sum(1 for r in ship if not r.get("fallback") and r.get("variant") == "decompose")
    return {"applied": applied, "runs": len(ship), "share": applied / len(ship) if ship else None}


def p5_ratio(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    cur = sum(int(r.get("n_opened", 0)) for r in records if r["arm"] == "current")
    shp = sum(int(r.get("n_opened", 0)) for r in records if r["arm"] == "ship")
    return {"current": cur, "ship": shp, "ratio": shp / cur if cur else None}


def decide(
    stats: Mapping[str, Any],
    p3: Mapping[str, Any],
    p4: Mapping[str, Any],
    p5: Mapping[str, Any],
    determinable_share: float,
    *,
    excluded_share: float | None = None,
) -> dict[str, Any]:
    """§6 の合否。判定不能の条件を先に見る。

    excluded_share (R18 で外した単位 / U) は rejudge だけが渡す (50% 超で判定不能)。
    """
    reasons: list[str] = []
    if excluded_share is not None and excluded_share > MAX_EXCLUDED_SHARE:
        reasons.append("R18 で外れる単位が U の 50% 超")
    if stats.get("n_windows", 0) < MIN_WINDOWS:
        reasons.append("窓が 18 未満")
    if p3["denominator"] < MIN_P3_DENOMINATOR:
        reasons.append("P3 の分母が 30 未満")
    if p4["share"] is None or p4["share"] < P4_FLOOR:
        reasons.append("P4 が 90% 未満")
    if determinable_share < MIN_DETERMINABLE_SHARE:
        reasons.append("裁定できた単位が 80% 未満")
    if reasons:
        return {"verdict": "undetermined", "reasons": reasons}
    lower = stats["mean_d_ci"][0]
    p1 = lower >= P1_LOWER_FLOOR and not (stats["wilcoxon_p"] < ALPHA and stats["median_d"] < 0)
    p2_upper = stats.get("false_rate_diff_ci", (None, None))[1]
    p2 = p2_upper is not None and p2_upper <= P2_UPPER_CEIL
    p3_ok = p3["share"] <= P3_CEIL
    p5_ok = p5["ratio"] is not None and p5["ratio"] <= P5_CEIL
    checks = {"P1": p1, "P2": p2, "P3": p3_ok, "P4": True, "P5": p5_ok}
    return {
        "verdict": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "p1_lower": lower,
        "p2_upper": p2_upper,
    }
