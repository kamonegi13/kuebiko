"""純関数: 審査の結果 → 自己一致 (κ)・基線 (主張 100 件あたり)・T4 の判定。

入力は run.py が書く jsonl の行 (dict)。DB にも Opus にも触れない。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from scripts.n20_audit.rubric import KINDS
from scripts.n20_audit.stats import (
    WEIGHTS,
    agreement,
    bootstrap_ci,
    cohen_kappa,
    locate_quote,
    per_100,
    reproduction,
    wilson,
)

KAPPA_MIN = 0.4  # 契約 §1: これ未満の課題はその物差しで合否を判定しない
LINE_PER_100 = 1.0  # 契約 §1: 確認された重い違反 ≤1.0/100 主張
T4_HEAVY_MAX = 0.05  # 契約 §1・U-C: 翻訳はチャンク単位で重い問題 ≤5%
HEAVY_VERDICT = "重い問題あり"


def criteria(audit: Mapping[str, Any]) -> dict[str, bool]:
    """事例単位の基準 (2 値): 重い指摘あり・指摘あり・種類ごとの指摘あり。"""
    fs = audit.get("findings") or []
    out = {
        "重い指摘あり": any(f["severity"] == "重い" for f in fs),
        "指摘あり": bool(fs),
    }
    for k in KINDS:
        out[f"種類:{k}"] = any(f["kind"] == k for f in fs)
    return out


def flagged_claims(
    audit: Mapping[str, Any], claims: Sequence[str], *, heavy_only: bool
) -> set[int]:
    """引用が出力に実在する指摘の文の番号の集合。"""
    out: set[int] = set()
    for f in audit.get("findings") or []:
        if heavy_only and f["severity"] != "重い":
            continue
        ok, idx = locate_quote(f["quote"], claims)
        if ok and idx is not None:
            out.add(idx)
    return out


def self_agreement(
    items: Mapping[str, Mapping[str, Any]],
    pass1: Mapping[str, Mapping[str, Any]],
    pass2: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """1 課題の自己一致。items/pass1/pass2 は item_id → 行。両方の審査がある事例だけ数える。"""
    ids = sorted(set(pass1) & set(pass2) & set(items))
    c1 = [criteria(pass1[i]) for i in ids]
    c2 = [criteria(pass2[i]) for i in ids]
    per: dict[str, Any] = {}
    for name in c1[0] if c1 else {}:
        a = [x[name] for x in c1]
        b = [x[name] for x in c2]
        k = cohen_kappa(a, b)
        per[name] = {
            "kappa": None if k is None else round(k, 3),
            "agreement": None if not ids else round(agreement(a, b) or 0.0, 3),
            "pos_pass1": sum(a),
            "pos_pass2": sum(b),
        }
    rep: dict[str, Any] = {}
    for label, heavy in (("重い", True), ("すべて", False)):
        f1: set[tuple[str, int]] = set()
        f2: set[tuple[str, int]] = set()
        for i in ids:
            cl = items[i]["claims"]
            f1 |= {(i, n) for n in flagged_claims(pass1[i], cl, heavy_only=heavy)}
            f2 |= {(i, n) for n in flagged_claims(pass2[i], cl, heavy_only=heavy)}
        rep[label] = reproduction(f1, f2)
    claim = claim_level_kappa(items, pass1, pass2, ids)
    heavy_k = per.get("重い指摘あり", {}).get("kappa")
    # 長い出力では「重い指摘あり」が全件 yes に張り付き事例単位の κ が定義できない。
    # そのときは主張 (文) 単位の κ で判定する (契約 §1 の「主張単位」の物差しに合わせる)。
    gate = heavy_k if heavy_k is not None else claim["重い"]
    return {
        "n": len(ids),
        "criteria": per,
        "claim_level": claim,
        "finding_reproduction": rep,
        "kappa_for_gate": gate,
        "judged_by_rubric": gate is not None and gate >= KAPPA_MIN,
    }


def claim_level_kappa(
    items: Mapping[str, Mapping[str, Any]],
    pass1: Mapping[str, Mapping[str, Any]],
    pass2: Mapping[str, Mapping[str, Any]],
    ids: Sequence[str],
) -> dict[str, float | None]:
    """全事例の全主張を 1 列に並べ、文ごとの「指摘あり」の 2 回の一致の κ (重い / すべて)。"""
    out: dict[str, float | None] = {}
    for label, heavy in (("重い", True), ("すべて", False)):
        a: list[bool] = []
        b: list[bool] = []
        for i in ids:
            cl = items[i]["claims"]
            f1 = flagged_claims(pass1[i], cl, heavy_only=heavy)
            f2 = flagged_claims(pass2[i], cl, heavy_only=heavy)
            a.extend(n in f1 for n in range(len(cl)))
            b.extend(n in f2 for n in range(len(cl)))
        k = cohen_kappa(a, b)
        out[label] = None if k is None else round(k, 3)
    return out


def baseline(
    items: Mapping[str, Mapping[str, Any]],
    pass1: Mapping[str, Mapping[str, Any]],
    confirm: Mapping[str, Mapping[str, Any]],
    *,
    seed: int,
) -> dict[str, Any]:
    """n19 の基線 (1 回目の審査 + 確認)。率は主張 100 件あたり。

    分母は審査できた事例の主張の総数。
    """
    ids = sorted(set(pass1) & set(items))
    claims_total = 0
    raw_heavy = verified_heavy = confirmed_heavy = light = 0
    weighted = 0.0
    pairs: list[tuple[float, int]] = []
    kinds: Counter[str] = Counter()
    unconfirmed_items = 0
    for i in ids:
        cl = items[i]["claims"]
        claims_total += len(cl)
        fs = pass1[i].get("findings") or []
        heavy_fs = [f for f in fs if f["severity"] == "重い"]
        raw_heavy += len(heavy_fs)
        light += sum(f["severity"] == "軽い" for f in fs)
        verified = [f for f in heavy_fs if locate_quote(f["quote"], cl)[0]]
        verified_heavy += len(verified)
        conf = confirm.get(i)
        n_conf, n_light_conf = 0, 0
        if conf is not None:
            asked = {a["id"]: a for a in conf.get("asked") or []}
            for v in conf.get("items") or []:
                if v["id"] not in asked:
                    continue
                if v["verdict"] == "重い違反":
                    n_conf += 1
                    kinds[asked[v["id"]]["kind"]] += 1
                elif v["verdict"] == "軽い違反":
                    n_light_conf += 1
        elif verified:
            unconfirmed_items += 1
        confirmed_heavy += n_conf
        w = (
            n_conf * WEIGHTS["重い"]
            + (n_light_conf + sum(f["severity"] == "軽い" for f in fs)) * WEIGHTS["軽い"]
        )
        weighted += w
        pairs.append((float(n_conf), len(cl)))
    return {
        "n": len(ids),
        "claims_total": claims_total,
        "claims_per_item": round(claims_total / len(ids), 1) if ids else None,
        "heavy_raw_per100": per_100(raw_heavy, claims_total),
        "heavy_quote_verified_per100": per_100(verified_heavy, claims_total),
        "heavy_confirmed": confirmed_heavy,
        "heavy_confirmed_per100": per_100(confirmed_heavy, claims_total),
        "heavy_confirmed_per100_ci95": bootstrap_ci(pairs, seed=seed),
        "confirm_rate": round(confirmed_heavy / verified_heavy, 3) if verified_heavy else None,
        "light_per100": per_100(light, claims_total),
        "weighted_per100": per_100(weighted, claims_total),
        "confirmed_heavy_kinds": dict(kinds),
        "items_with_unconfirmed_heavy": unconfirmed_items,
        "n20_line_per100": n20_line(per_100(confirmed_heavy, claims_total)),
    }


def n20_line(baseline_per100: float | None) -> float | None:
    """n20 の合格線: 主張 100 件あたり ≤1.0 かつ n19 基線の半分以下 = min(1.0, 基線/2)。"""
    if baseline_per100 is None:
        return None
    return round(min(LINE_PER_100, baseline_per100 / 2), 3)


def translation_agreement(
    pass1: Mapping[str, Mapping[str, Any]], pass2: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    ids = sorted(set(pass1) & set(pass2))
    v1 = [pass1[i]["verdict"] for i in ids]
    v2 = [pass2[i]["verdict"] for i in ids]
    h1 = [v == HEAVY_VERDICT for v in v1]
    h2 = [v == HEAVY_VERDICT for v in v2]
    k3 = cohen_kappa(v1, v2)
    k2 = cohen_kappa(h1, h2)
    return {
        "n": len(ids),
        "verdict3": {
            "kappa": None if k3 is None else round(k3, 3),
            "agreement": round(agreement(v1, v2) or 0.0, 3) if ids else None,
        },
        "重い問題あり": {
            "kappa": None if k2 is None else round(k2, 3),
            "agreement": round(agreement(h1, h2) or 0.0, 3) if ids else None,
            "pos_pass1": sum(h1),
            "pos_pass2": sum(h2),
        },
        "judged_by_rubric": k2 is not None and k2 >= KAPPA_MIN,
    }


def t4_decision(rows: Sequence[Mapping[str, Any]], population: Mapping[str, int]) -> dict[str, Any]:
    """U-C: 境界を除く重い問題の率 ≤5% なら採用、超えたら不合格の型だけ後で Opus で作り直す。

    率は (a) 標本そのまま と (b) 種類 (translate・actor_sync) の母集団の比で重み付け の 2 つ。
    判定は (b)。
    """
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in sorted({r["kind"] for r in rows}):
        rs = [r for r in rows if r["kind"] == kind]
        nb = [r for r in rs if not r["boundary"]]
        heavy_nb = sum(r["verdict"] == HEAVY_VERDICT for r in nb)
        by_kind[kind] = {
            "n": len(rs),
            "non_boundary": len(nb),
            "heavy_non_boundary": heavy_nb,
            "heavy_incl_boundary": sum(r["verdict"] == HEAVY_VERDICT for r in rs),
            "rate_non_boundary": round(heavy_nb / len(nb), 3) if nb else None,
            "verdicts": dict(Counter(r["verdict"] for r in rs)),
        }
    nb_all = [r for r in rows if not r["boundary"]]
    heavy_all = sum(r["verdict"] == HEAVY_VERDICT for r in nb_all)
    pop = {k: population.get(k, 0) for k in by_kind}
    pop_total = sum(pop.values())
    weighted = (
        sum((by_kind[k]["rate_non_boundary"] or 0.0) * pop[k] for k in by_kind) / pop_total
        if pop_total
        else None
    )
    failing_types: Counter[str] = Counter()
    for r in rows:
        if r["verdict"] != HEAVY_VERDICT:
            continue
        for field in ("missing", "mistranslations", "additions"):
            if r.get(field):
                failing_types[f"{r['kind']}:{field}"] += 1
    rate = weighted if weighted is not None else None
    return {
        "n": len(rows),
        "non_boundary": len(nb_all),
        "heavy_non_boundary": heavy_all,
        "rate_sample": round(heavy_all / len(nb_all), 3) if nb_all else None,
        "rate_sample_wilson95": wilson(heavy_all, len(nb_all)),
        "rate_weighted": None if rate is None else round(rate, 3),
        "boundary": len(rows) - len(nb_all),
        "heavy_incl_boundary": sum(r["verdict"] == HEAVY_VERDICT for r in rows),
        "by_kind": by_kind,
        "failing_types": dict(failing_types),
        "decision": None
        if rate is None
        else ("採用" if rate <= T4_HEAVY_MAX else "不合格の型だけ Opus で作り直す"),
    }
