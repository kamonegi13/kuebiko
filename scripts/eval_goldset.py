#!/usr/bin/env python3
"""同一入力比較の評価 CLI — 凍結した記事集合で summarizer の出力差を見る。

``verify_prompt_cutover.py`` は**期間比較** (切替前後の同じ幅の窓)。窓が違えば入力記事も
違うため、**プロンプトの変化と収集コーパスの変化を分離できない**。本 script は入力を
凍結して「同じ記事を新旧に通す」ことでその交絡を断つ。rubric を触る判断を、デプロイも
期間 A/B も待たずに下せるようにするのが目的。

Usage::

    # 1. 層化して標本を凍結 (data/eval/goldset.jsonl)
    uv run python scripts/eval_goldset.py build --per-stratum 3

    # 2. 現行の rubric で走らせて基準を作る
    uv run python scripts/eval_goldset.py run --label baseline

    # 3. rubric を編集した後、もう一度走らせて突き合わせる
    uv run python scripts/eval_goldset.py run --label no-article-type
    uv run python scripts/eval_goldset.py compare baseline no-article-type

exit code: 0=正常 / 1=エラー。**合否は出さない** — 何がどう変わったかを見せる道具で、
閾値判定は用途ごとに人が決める (期間比較の閾値をここに複製しない)。

出力に記事本文・タイトルは出さない (article_id とフィールド値のみ)。本文を含む
gold set 自体は ``data/eval/`` (gitignore 済) に置く — 公開リポに収集記事を載せない
(CLAUDE.md §9)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.eval.goldset import (  # noqa: E402
    GoldArticle,
    load_goldset,
    save_goldset,
    select_goldset,
)

GOLDSET_PATH = Path("data/eval/goldset.jsonl")
RUNS_DIR = Path("data/eval/runs")


# ---------------- build ----------------


def _fetch_candidates(days: int, limit: int) -> list[GoldArticle]:
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    sql = """
        SELECT id, title, body, feed_title, published_at, category, importance
        FROM articles
        WHERE created_at > NOW() - INTERVAL '%s days'
          AND body IS NOT NULL AND body <> ''
          AND summary IS NOT NULL AND summary <> ''
          AND category IS NOT NULL AND importance IS NOT NULL
        ORDER BY id
        LIMIT %s
    """
    with repo._connect() as conn:  # noqa: SLF001 — 評価用の読み取り専用アクセス
        cur = conn.execute(sql % (days, limit))
        rows = cur.fetchall()
    out: list[GoldArticle] = []
    for r in rows:
        d = dict(r) if not isinstance(r, dict) else r
        vals = list(d.values()) if not isinstance(r, (list, tuple)) else list(r)
        aid, title, body, feed, pub, cat, imp = vals[:7]
        out.append(
            GoldArticle(
                article_id=str(aid),
                title=str(title or ""),
                body=str(body or ""),
                feed_title=str(feed or ""),
                published=str(pub) if pub else None,
                category=str(cat or "unknown"),
                importance=str(imp or "unknown"),
            )
        )
    return out


def cmd_build(args: argparse.Namespace) -> int:
    cands = _fetch_candidates(args.days, args.candidate_limit)
    picked = select_goldset(cands, per_stratum=args.per_stratum)
    n = save_goldset(GOLDSET_PATH, picked)
    print(f"候補 {len(cands)} 件 → 凍結 {n} 件  ({GOLDSET_PATH})")
    strata = Counter(a.stratum() for a in picked)
    print(f"\n{'importance':<12}{'category':<18}{'件数':>5}")
    for (imp, cat), c in sorted(strata.items()):
        print(f"{imp:<12}{cat:<18}{c:>5}")
    print(
        f"\n層の数 {len(strata)} / 平均本文長 {sum(len(a.body) for a in picked) // max(n, 1):,} 字"
    )
    return 0


# ---------------- run ----------------


async def _run_one(llm: Any, template: Any, art: GoldArticle) -> dict[str, Any]:
    from src.pipeline.summary import SummaryOutput

    prompt = template.render(article=art, body=art.body)
    out = await llm.generate_structured(prompt, schema=SummaryOutput, think=False)
    return {"article_id": art.article_id, **out.model_dump()}


def _build_template(drop: list[str]) -> Any:
    """評価用のテンプレートを組む。``drop`` 指定時は **DB に保存せず** 変種を使う。

    本番の rubric を書き換えると production が即座に新版を使うため、
    「もしこのフィールドを外したら」はメモリ上の変種で試す。
    """
    from src.pipeline.dispatch import DEFAULT_TEMPLATE_PATH, _load_template

    if not drop:
        return _load_template()  # 本番と同一経路
    from src.eval.rubric_variant import drop_fields
    from src.prompts.rubric_store import load_rubric
    from src.prompts.summarizer_composer import build_template

    rubric = load_rubric()
    if rubric is None:
        raise RuntimeError("DB から rubric を取得できないため変種を作れない")
    return build_template(drop_fields(rubric, drop), path=DEFAULT_TEMPLATE_PATH)


async def _run_all(articles: list[GoldArticle], label: str, drop: list[str]) -> Path:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for

    config = load_app_config()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, config)
    template = _build_template(drop)
    if drop:
        print(f"変種で実行 (除外フィールド: {', '.join(drop)}) — DB は変更しない", file=sys.stderr)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{label}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i, art in enumerate(articles, 1):
            try:
                rec = await _run_one(llm, template, art)
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で評価全体を止めない
                rec = {"article_id": art.article_id, "_error": f"{type(e).__name__}: {e}"[:200]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            print(f"  {i}/{len(articles)}", end="\r", file=sys.stderr)
    return path


def cmd_run(args: argparse.Namespace) -> int:
    if not GOLDSET_PATH.exists():
        print(f"gold set が無い。先に build を実行する: {GOLDSET_PATH}", file=sys.stderr)
        return 1
    articles = load_goldset(GOLDSET_PATH)
    path = asyncio.run(_run_all(articles, args.label, args.drop_field or []))
    print(f"{len(articles)} 件を実行 → {path}")
    return 0


# ---------------- compare ----------------


def _load_run(label: str) -> dict[str, dict[str, Any]]:
    path = RUNS_DIR / f"{label}.jsonl"
    with path.open(encoding="utf-8") as f:
        return {json.loads(line)["article_id"]: json.loads(line) for line in f if line.strip()}


def _norm(v: Any) -> str:
    """比較用の正規化 (list は順序非依存、None と空を同一視)。"""
    if v is None or v == "" or v == [] or v == {}:
        return ""
    if isinstance(v, list):
        return "|".join(sorted(str(x) for x in v))
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def cmd_compare(args: argparse.Namespace) -> int:
    a, b = _load_run(args.label_a), _load_run(args.label_b)
    shared = sorted(set(a) & set(b))
    if not shared:
        print("共通の article_id が無い", file=sys.stderr)
        return 1

    fields = sorted({k for r in a.values() for k in r} - {"article_id", "_error"})
    print(f"{args.label_a} vs {args.label_b}  (共通 {len(shared)} 件)\n")
    print(f"{'フィールド':<24}{'一致':>6}{'変化':>6}  {'空→値':>6}{'値→空':>6}")
    print("-" * 56)
    changed_ids: dict[str, list[str]] = {}
    for fld in fields:
        same = filled = emptied = 0
        for aid in shared:
            va, vb = _norm(a[aid].get(fld)), _norm(b[aid].get(fld))
            if va == vb:
                same += 1
            else:
                changed_ids.setdefault(fld, []).append(aid)
                if not va and vb:
                    filled += 1
                elif va and not vb:
                    emptied += 1
        pct = same / len(shared) * 100
        mark = "" if pct == 100 else " *"
        print(f"{fld:<24}{pct:>5.0f}%{len(shared) - same:>6}  {filled:>6}{emptied:>6}{mark}")

    if args.show_ids:
        print("\n変化した article_id (フィールド別、先頭 10 件):")
        for fld, ids in sorted(changed_ids.items()):
            print(f"  {fld}: {', '.join(ids[:10])}{' …' if len(ids) > 10 else ''}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="層化して標本を凍結する")
    b.add_argument("--per-stratum", type=int, default=3, help="(importance x category) ごとの上限")
    b.add_argument("--days", type=int, default=14, help="候補を取る期間")
    b.add_argument("--candidate-limit", type=int, default=5000)
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("run", help="凍結した標本を現行 rubric で走らせる")
    r.add_argument("--label", required=True, help="実行結果の名前 (例 baseline)")
    r.add_argument(
        "--drop-field",
        action="append",
        help="判定基準と出力例からこのフィールドを外して実行する (DB は変更しない)",
    )
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="2 つの実行結果をフィールド別に突き合わせる")
    c.add_argument("label_a")
    c.add_argument("label_b")
    c.add_argument("--show-ids", action="store_true", help="変化した article_id も出す")
    c.set_defaults(func=cmd_compare)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
