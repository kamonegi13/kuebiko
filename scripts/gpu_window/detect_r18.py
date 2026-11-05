#!/usr/bin/env python3
"""DETECT-JUDGE の R18 閉包を計算し、学習側と同じ成分に入る単位を closure.json に書く (prereg §7)。

本番 DB は read-only セッションの SELECT だけ。Opus も Ollama も使わない。

    python scripts/gpu_window/detect_r18.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import detect_judge as dj  # noqa: E402
import detect_judge_core as jc  # noqa: E402
import detect_r18_core as rc  # noqa: E402

from scripts.replay._closure import (  # noqa: E402
    R18_VERSION,
    assert_components_ok,
    build_components,
    component_diagnostics,
    resolve_roots,
)

EMBED_MODEL = "snowflake-arctic-embed2"
MLX = dj.MAIN_ROOT / "data" / "mlx"
TRAIN_FILES = (
    MLX / "detect_goldset.jsonl",
    MLX / "detect_gold_kinds.jsonl",
    MLX / "detect_heldout_kinds.jsonl",
    MLX / "detect_labels.json",
)
CLOSURE_PATH = dj.EVAL_DIR / "judge_r18" / "closure.json"


def psql_rows(sql: str) -> list[dict[str, Any]]:
    """docker exec psql を read-only セッションで実行し、1 行 1 JSON の結果を返す。"""
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("read-only SELECT/WITH only")
    proc = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "exec",
            "-i",
            "-e",
            "PGOPTIONS=-c default_transaction_read_only=on",
            "postgres",
            "psql",
            "-U",
            "kuebiko",
            "-d",
            "kuebiko",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
        check=True,
    )
    return [json.loads(x) for x in proc.stdout.splitlines() if x.strip()]


def _values(items: Sequence[str]) -> str:
    return ",".join("('" + s.replace("'", "''") + "')" for s in items)


def _chunks(items: Sequence[str], n: int) -> list[Sequence[str]]:
    return [items[i : i + n] for i in range(0, len(items), n)]


def existing_ids(cands: Sequence[str]) -> set[str]:
    out: set[str] = set()
    for ch in _chunks(sorted(cands), 5000):
        sql = f"""WITH ids(article_id) AS (VALUES {_values(ch)})
SELECT row_to_json(t) FROM (SELECT DISTINCT a.article_id FROM articles a JOIN ids USING (article_id)) t;"""  # noqa: E501
        out |= {r["article_id"] for r in psql_rows(sql)}
    return out


def train_side_ids() -> tuple[set[str], dict[str, int]]:
    """学習・調整側の記事 id (goldset・train_detect_ml の入力・教師)。DB に実在する語だけ。"""
    per_source: dict[str, int] = {}
    found: set[str] = set()
    files = [*TRAIN_FILES, *sorted((MLX / "teacher").glob("detect*.jsonl"))]
    for f in files:
        if not f.is_file():
            continue
        ids = existing_ids(sorted(rc.id_tokens(f.read_text(encoding="utf-8"))))
        per_source[f.name] = len(ids)
        found |= ids
    return found, per_source


def fetch_entities(ids: Sequence[str]) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = {}
    for ch in _chunks(ids, 20000):
        sql = f"""WITH ids(article_id) AS (VALUES {_values(ch)})
SELECT row_to_json(t) FROM (SELECT e.article_id, e.entity_type, e.value FROM article_entities e
 JOIN ids USING (article_id) WHERE e.entity_type IN ('cve','victim_org')) t;"""
        for r in psql_rows(sql):
            out.setdefault(r["article_id"], {}).setdefault(r["entity_type"], set()).add(r["value"])
    return out


def fetch_sharing(pairs: Sequence[tuple[str, str]]) -> set[str]:
    found: set[str] = set()
    for ch in _chunks([f"{t}\x1f{v}" for t, v in pairs], 15000):
        vals = ",".join(
            "('"
            + p.split("\x1f")[0].replace("'", "''")
            + "','"
            + p.split("\x1f", 1)[1].replace("'", "''")
            + "')"
            for p in ch
        )
        sql = f"""WITH kv(t, v) AS (VALUES {vals})
SELECT row_to_json(x) FROM (SELECT DISTINCT e.article_id FROM article_entities e
 JOIN kv ON e.entity_type = kv.t AND e.value = kv.v) x;"""
        found |= {r["article_id"] for r in psql_rows(sql)}
    return found


def fetch_embeddings(ids: Sequence[str]) -> dict[str, list[float]]:
    import numpy as np

    out: dict[str, list[float]] = {}
    for ch in _chunks(ids, 5000):
        sql = f"""WITH ids(article_id) AS (VALUES {_values(ch)})
SELECT row_to_json(t) FROM (SELECT s.article_id, encode(s.vector, 'hex') AS v
 FROM summary_embeddings s JOIN ids USING (article_id) WHERE s.model = '{EMBED_MODEL}') t;"""
        for r in psql_rows(sql):
            out[r["article_id"]] = np.frombuffer(bytes.fromhex(r["v"]), dtype=np.float32).tolist()
    return out


def fetch_roots(ids: Sequence[str]) -> tuple[dict[str, str], dict[str, frozenset[str]]]:
    """(article -> 最新の根、article -> 所属した全事象の根)。"""
    rows = psql_rows("SELECT row_to_json(t) FROM (SELECT id, merged_into FROM event_items) t;")
    roots = resolve_roots({r["id"]: r["merged_into"] for r in rows})
    primary: dict[str, str] = {}
    every: dict[str, set[str]] = {}
    for ch in _chunks(ids, 20000):
        sql = f"""WITH ids(article_id) AS (VALUES {_values(ch)})
SELECT row_to_json(t) FROM (SELECT m.article_id, m.item_id FROM event_item_members m
 JOIN ids USING (article_id) ORDER BY m.joined_at) t;"""
        for r in psql_rows(sql):
            root = roots.get(r["item_id"], r["item_id"])
            primary[r["article_id"]] = root
            every.setdefault(r["article_id"], set()).add(root)
    return primary, {a: frozenset(v) for a, v in every.items()}


def excluded_pre(units: dict[str, jc.Unit], comp: dict[str, str], train_ids: set[str]) -> set[str]:
    return rc.contaminated_units(units, comp, frozenset(train_ids))


def main() -> int:
    from src.assessment.evidence_verify import normalize_for_match

    units = jc.collect_units(dj.read_jsonl(dj.EVAL_DIR / dj.ARMS_NAME))
    unit_articles = sorted({a for u in units.values() for a in u.article_ids})
    train_ids, per_source = train_side_ids()
    base = sorted(set(unit_articles) | train_ids)
    ents = fetch_entities(base)

    def cut(universe_ents: dict[str, dict[str, set[str]]]) -> dict[str, dict[str, frozenset[str]]]:
        return {
            t: rc.apply_hub_cut({a: frozenset(e.get(t, ())) for a, e in universe_ents.items()})
            for t in ("cve", "victim_org")
        }

    cuts = cut(ents)
    pairs = sorted({(t, v) for t, m in cuts.items() for s in m.values() for v in s})
    universe = sorted(set(base) | fetch_sharing(pairs))
    if len(universe) != len(base):
        ents = {**ents, **fetch_entities(sorted(set(universe) - set(base)))}
        cuts = cut(ents)
    primary, every = fetch_roots(universe)
    emb = fetch_embeddings(universe)
    cves = {a: v | frozenset(f"ROOT:{r}" for r in every.get(a, ())) for a, v in cuts["cve"].items()}
    for a, rs in every.items():
        cves.setdefault(a, frozenset(f"ROOT:{r}" for r in rs))
    orgs = {
        a: frozenset(normalize_for_match(x) for x in v if x) for a, v in cuts["victim_org"].items()
    }
    comp = build_components(universe, root_of=primary, cves=cves, victim_orgs=orgs, embeddings=emb)
    n_comp, largest, share = component_diagnostics(comp)
    share_breach = share > rc.MAX_COMPONENT_SHARE
    if share_breach:  # prereg §7 は中止だが、最大成分の占有を記録して続行する (逸脱として報告)
        try:
            assert_components_ok(comp, rc.MAX_COMPONENT_SHARE)
        except Exception as exc:  # noqa: BLE001
            print(f"[逸脱] {exc}", file=sys.stderr)
    excluded = excluded_pre(units, comp, train_ids)
    big = Counter(comp.values()).most_common(1)[0][0]
    via_largest = {
        uid for uid in excluded if any(comp.get(a) == big for a in units[uid].article_ids)
    }
    CLOSURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "r18_version": R18_VERSION,
        "cos_threshold": rc.COS_THRESHOLD,
        "focus_cve_max": rc.FOCUS_CVE_MAX,
        "max_component_share": rc.MAX_COMPONENT_SHARE,
        "n_units": len(units),
        "n_unit_articles": len(unit_articles),
        "n_train_ids": len(train_ids),
        "train_ids_per_source": per_source,
        "universe": len(universe),
        "n_with_embedding": sum(1 for a in universe if a in emb),
        "n_unit_articles_with_embedding": sum(1 for a in unit_articles if a in emb),
        "n_components": n_comp,
        "largest_component": largest,
        "largest_component_share": share,
        "max_component_share_breached": share_breach,
        "n_excluded_via_largest_component": len(via_largest),
        "n_excluded": len(excluded),
        "excluded_by_stratum": rc.count_by_stratum(units, excluded),
        "units_by_stratum": rc.count_by_stratum(units, set(units)),
        "excluded": sorted(excluded),
    }
    CLOSURE_PATH.write_text(json.dumps(out, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "excluded"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
