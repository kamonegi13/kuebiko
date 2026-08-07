"""統合判断分類器のモデル別計測 (2026-07-26)。

判断分類器に専用ティア (別モデル) を割り当てるべきかの判断材料。同一標本で 26B (現用
FAST baseline) と候補モデル群を走らせ、**遅延** (取込ホットパスの timeout に効く) と
**質** (26B との判断フィールド差分 + 過剰帰属していないか) を測る。

usage:
    uv run python scripts/measure_judgment_model.py --n 10 \
        --models gpt-oss-safeguard:20b,gemma3:12b,mistral-small3.2:24b
"""

from __future__ import annotations

import argparse
import asyncio
import time

from src.config_loader import load_app_config
from src.cti.actor_normalizer import load_actor_aliases
from src.cti.judgment_classifier import classify_judgment
from src.pipeline.persistence import _relevant_actors
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref

_CYBER = ("apt", "malware", "incident", "breach", "advisory", "phishing", "vulnerability")
_FIELDS = ("article_type", "stance", "intent", "i_infra", "subject")


def _fields(j: object) -> dict[str, object]:
    if j is None:
        return dict.fromkeys(_FIELDS)
    return {
        "article_type": j.article_type,
        "stance": j.editorial_stance,
        "intent": j.intent,
        "i_infra": j.i_infra,
        "subject": j.subject_actor_id or "-",
    }


def _stats(xs: list[float]) -> str:
    xs2 = sorted(xs)
    if not xs2:
        return "n/a"
    avg = sum(xs2) / len(xs2)
    p90 = xs2[int(len(xs2) * 0.9)] if len(xs2) > 1 else xs2[0]
    return f"avg={avg:.1f}s max={max(xs2):.1f}s p90={p90:.1f}s"


async def _run_model(llm: object, rows: list, registry: object) -> tuple[list[float], list[dict]]:
    lat: list[float] = []
    outs: list[dict] = []
    for r in rows:
        title = str(r["title"] or "")
        body = str(r["body"] or "")
        category = str(r["category"] or "") or None
        cands = _relevant_actors(registry.find_all(body), category)
        t0 = time.monotonic()
        j = await classify_judgment(
            llm, title=title, category=category, body=body, published=None, candidates=cands
        )
        lat.append(time.monotonic() - t0)
        outs.append(_fields(j))
    return lat, outs


async def measure(n: int, models: list[str]) -> None:
    cfg = load_app_config()
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    cyber_ph = ",".join("?" for _ in _CYBER)
    sql = (
        "SELECT a.article_id, a.title, a.body, a.category, MAX(a.created_at) ts "
        "FROM articles a "
        f"WHERE a.category IN ({cyber_ph}) AND a.body IS NOT NULL AND a.body <> '' "
        "AND a.feed_url NOT LIKE ? "
        "GROUP BY a.article_id, a.title, a.body, a.category "
        "ORDER BY ts DESC LIMIT ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql, (*_CYBER, "%ransomware.live%", n)).fetchall()

    # baseline: 現用 FAST (26B)
    base_llm = build_llm_for(Step.ARTICLE_SUMMARY, cfg)
    base_lat, base_outs = await _run_model(base_llm, rows, registry)
    print(f"\n=== 判断分類器モデル計測 (n={len(rows)}) ===")
    print(f"\n[baseline] FAST (現用 26B): {_stats(base_lat)}")

    for model in models:
        try:
            llm = build_llm_for_ref(model, Step.ARTICLE_SUMMARY, cfg)
            lat, outs = await _run_model(llm, rows, registry)
        except Exception as e:  # noqa: BLE001
            print(f"\n[{model}] 計測失敗: {e}")
            continue
        agree = dict.fromkeys(_FIELDS, 0)
        diffs: list[str] = []
        for i, (b, c) in enumerate(zip(base_outs, outs, strict=False)):
            for f in _FIELDS:
                if b[f] == c[f]:
                    agree[f] += 1
                elif len(diffs) < 12:
                    diffs.append(
                        f"    {f}: 26B={b[f]} | {model.split(':')[0]}={c[f]} "
                        f"| {str(rows[i]['title'])[:38]}"
                    )
        print(f"\n[{model}] {_stats(lat)}")
        print(
            "  一致(vs26B): "
            + " ".join(f"{f}={100 * agree[f] // len(rows)}%" for f in _FIELDS)
        )
        if diffs:
            print("  差分:")
            print("\n".join(diffs))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument(
        "--models",
        type=str,
        default="gpt-oss-safeguard:20b,gemma3:12b,mistral-small3.2:24b",
        help="カンマ区切りの候補モデル ref",
    )
    args = parser.parse_args()
    asyncio.run(measure(args.n, [m.strip() for m in args.models.split(",") if m.strip()]))


if __name__ == "__main__":
    main()
