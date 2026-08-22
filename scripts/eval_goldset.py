#!/usr/bin/env python3
"""同一入力比較の評価 CLI — 凍結した記事集合で summarizer の出力差を見る。

``verify_prompt_cutover.py`` は**期間比較** (切替前後の同じ幅の窓)。窓が違えば入力記事も
違うため、**プロンプトの変化と収集コーパスの変化を分離できない**。本 script は入力を
凍結して「同じ記事を新旧に通す」ことでその交絡を断つ。rubric を触る判断を、デプロイも
期間 A/B も待たずに下せるようにするのが目的。

Usage::

    # 1. 層化して標本を凍結 (data/eval/goldset.jsonl)
    uv run python scripts/eval_goldset.py build --per-stratum 3

    # 2. 現行の rubric で **2 回** 走らせる (2 本目が揺らぎの床 = 対照)
    uv run python scripts/eval_goldset.py run --label baseline
    uv run python scripts/eval_goldset.py run --label baseline2

    # 3. 変種を走らせ、床つきで突き合わせる (--floor は必須)
    uv run python scripts/eval_goldset.py run --label no-article-type --drop-field article_type
    uv run python scripts/eval_goldset.py compare baseline no-article-type \
        --floor baseline baseline2

⚠ **対照 (--floor) を省くと止まる**。temperature 0.3 で出力は確率的なため、同一 rubric の
2 回実行でも summary は 0%、analyst_note は 21% しか一致しない (2026-08-17 実測)。
床を取らずに比べると揺らぎを効果と誤読する。

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
import math
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

# 床の下限 (pt)。86 件の標本では 1 件の増減が約 1.2pt なので、それ未満の差は
# 対照が完全一致していても「変化」と呼ばない。
_MIN_FLOOR_PT = 1.5


def _binomial_sd_pt(rate: float, n: int) -> float:
    """充足率 ``rate`` を n 件で測ったときの標本ばらつき (1 sd, pt)。

    ⚠ **対照ペアから測った床は、充足率そのものが動くと転用できない**。二項分散は
    p(1-p) なので、p=0.18 で測った床 (±1.2pt) を p=0.68 の比較に当てると
    ノイズを「変化」と誤読する (2026-08-18 に実際にやった)。床と併記して両方見る。
    """
    return math.sqrt(max(rate * (1.0 - rate), 0.0) / max(n, 1)) * 100.0


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


async def _run_one_judgment(llm: Any, _template: Any, art: GoldArticle) -> dict[str, Any]:
    """取込時の統合判断分類器を 1 本走らせる (summarizer と同じ標本・同じ LLM ティア)。

    本番 (``briefing.py``) の呼び出しを写す: 本文を LLM 上限で切り、辞書で候補アクターを
    引き、category を渡す。**候補は本文言及集合に限る構造ゲート**なので、ここを省くと
    subject_actor_id の挙動が本番と変わる。

    ⚠ ``summary_text`` は渡さない。本番は summarizer の要約を渡すが、これは本文が
    空/極短のときの代替入力であり、gold set は本文非空のみで構成されている。
    """
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.judgment_classifier import classify_judgment
    from src.pipeline.briefing import MAX_LLM_BODY_CHARS
    from src.pipeline.persistence import _relevant_actors

    body = art.body[:MAX_LLM_BODY_CHARS]
    candidates = _relevant_actors(load_actor_aliases().find_all(body), art.category)
    out = await classify_judgment(
        llm,
        title=art.title,
        category=art.category,
        body=body,
        published=art.published,
        candidates=candidates,
    )
    if out is None:
        return {"article_id": art.article_id, "_error": "classify_judgment returned None"}
    return {"article_id": art.article_id, **out.model_dump()}


# 評価対象 → (1 件を走らせる関数, テンプレートが要るか)。
_TARGETS = {
    "summarizer": (_run_one, True),
    "judgment": (_run_one_judgment, False),
}


def _build_template(drop: list[str], rubric_version: int | None = None) -> Any:
    """評価用のテンプレートを組む。``drop`` 指定時は **DB に保存せず** 変種を使う。

    本番の rubric を書き換えると production が即座に新版を使うため、
    「もしこのフィールドを外したら」はメモリ上の変種で試す。
    ``rubric_version`` 指定時は config_store の版履歴からその版を pin して組む
    (較正格子 P2 の cron 化 — 旧版 vs 新版の同一入力比較。DB は変更しない)。
    """
    from src.pipeline.dispatch import DEFAULT_TEMPLATE_PATH, _load_template

    if rubric_version is not None:
        if drop:
            raise SystemExit("--drop-field と --rubric-version は併用できない")
        from src.prompts.rubric_model import SummarizerRubric
        from src.prompts.summarizer_composer import build_template
        from src.storage.config_store import get_config_version

        value = get_config_version("summarizer_rubric", rubric_version)
        if value is None:
            raise SystemExit(f"summarizer_rubric v{rubric_version} が版履歴に無い")
        rubric = SummarizerRubric.model_validate(value)
        return build_template(rubric, path=DEFAULT_TEMPLATE_PATH)
    if not drop:
        return _load_template()  # 本番と同一経路
    from src.eval.rubric_variant import drop_fields
    from src.prompts.rubric_store import load_rubric
    from src.prompts.summarizer_composer import build_template

    rubric = load_rubric()
    if rubric is None:
        raise RuntimeError("DB から rubric を取得できないため変種を作れない")
    return build_template(drop_fields(rubric, drop), path=DEFAULT_TEMPLATE_PATH)


async def _run_all(
    articles: list[GoldArticle],
    label: str,
    drop: list[str],
    target: str = "summarizer",
    model: str | None = None,
    rubric_version: int | None = None,
) -> Path:
    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for, build_llm_for_ref

    run_one, needs_template = _TARGETS[target]
    config = load_app_config()
    # --model はティア解決を飛ばして明示 ref で組む (UI の一時 override と同じ経路)。
    # 「上位モデルなら判断がどれだけ変わるか」= 伸びしろの測定に使う。
    llm = (
        build_llm_for_ref(model, Step.ARTICLE_SUMMARY, config)
        if model
        else build_llm_for(Step.ARTICLE_SUMMARY, config)
    )
    if model:
        print(f"モデル override: {model}", file=sys.stderr)
    template = _build_template(drop, rubric_version) if needs_template else None
    if drop and not needs_template:
        raise SystemExit("--drop-field は summarizer のみ (judgment の判定基準はコード内)")
    if rubric_version is not None and not needs_template:
        raise SystemExit("--rubric-version は summarizer のみ")
    if rubric_version is not None:
        print(f"rubric v{rubric_version} を pin して実行 — DB は変更しない", file=sys.stderr)
    if drop:
        print(f"変種で実行 (除外フィールド: {', '.join(drop)}) — DB は変更しない", file=sys.stderr)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{label}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i, art in enumerate(articles, 1):
            try:
                rec = await run_one(llm, template, art)
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
    path = asyncio.run(
        _run_all(
            articles,
            args.label,
            args.drop_field or [],
            args.target,
            args.model,
            getattr(args, "rubric_version", None),
        )
    )
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


def _fill_rate(run: dict[str, dict[str, Any]], field: str, ids: list[str]) -> float:
    """``ids`` のうち値が入っている割合 (0-1)。"""
    if not ids:
        return 0.0
    return sum(1 for i in ids if _norm(run[i].get(field))) / len(ids)


def _agreement(
    x: dict[str, dict[str, Any]],
    y: dict[str, dict[str, Any]],
    field: str,
    ids: list[str],
) -> float:
    if not ids:
        return 1.0
    return sum(1 for i in ids if _norm(x[i].get(field)) == _norm(y[i].get(field))) / len(ids)


def _floor_error() -> int:
    print(
        "対照が指定されていません。--floor <同一設定の 2 実行> を付けてください。\n"
        "  temperature 0.3 で出力は確率的です。同じ rubric の 2 回実行でも summary は 0%、\n"
        "  analyst_note は 21% しか一致しません。床を取らずに変種と基準を比べると、\n"
        "  揺らぎを効果と誤読します (2026-08-17 に実測)。\n"
        "  例: compare 現行 変種 --floor baseline baseline2\n"
        "  床を承知で生の差分だけ見るなら --no-floor を付けてください。",
        file=sys.stderr,
    )
    return 2


def _compare_rows(
    a: dict[str, dict[str, Any]],
    b: dict[str, dict[str, Any]],
    shared: list[str],
    floor_x: dict[str, dict[str, Any]] | None,
    floor_y: dict[str, dict[str, Any]] | None,
    floor_ids: list[str],
) -> list[dict[str, Any]]:
    """フィールド別の比較指標 (表示形式に依存しない中間表現)。"""
    fields = sorted({k for r in a.values() for k in r} - {"article_id", "_error"})
    rows: list[dict[str, Any]] = []
    for fld in fields:
        changed = [aid for aid in shared if _norm(a[aid].get(fld)) != _norm(b[aid].get(fld))]
        agree = _agreement(a, b, fld, shared)
        fill_a, fill_b = _fill_rate(a, fld, shared), _fill_rate(b, fld, shared)
        delta_pt = (fill_b - fill_a) * 100
        row: dict[str, Any] = {
            "field": fld,
            "agree": agree,
            "fill_a": fill_a,
            "fill_b": fill_b,
            "delta_pt": delta_pt,
            "changed_ids": changed,
        }
        if floor_x is not None and floor_y is not None:
            floor_agree = _agreement(floor_x, floor_y, fld, floor_ids)
            floor_pt = (
                abs(_fill_rate(floor_x, fld, floor_ids) - _fill_rate(floor_y, fld, floor_ids)) * 100
            )
            # 床 = 同一設定でも動く幅 / 二項 sd = この充足率で 86 件を引いたときの
            # 標本ばらつき。**両方を超えて初めて「変化」**と呼ぶ。
            noise_pt = max(floor_pt, _binomial_sd_pt(fill_a, len(shared)), _MIN_FLOOR_PT)
            row["floor_agree"] = floor_agree
            row["noise_pt"] = noise_pt
            row["is_change"] = abs(delta_pt) > noise_pt
        rows.append(row)
    return rows


def cmd_compare(args: argparse.Namespace) -> int:
    if not args.floor and not args.no_floor:
        return _floor_error()

    a, b = _load_run(args.label_a), _load_run(args.label_b)
    shared = sorted(set(a) & set(b))
    if not shared:
        print("共通の article_id が無い", file=sys.stderr)
        return 1

    floor_x: dict[str, dict[str, Any]] | None = None
    floor_y: dict[str, dict[str, Any]] | None = None
    floor_ids: list[str] = []
    if args.floor:
        floor_x, floor_y = _load_run(args.floor[0]), _load_run(args.floor[1])
        floor_ids = sorted(set(floor_x) & set(floor_y))
        if not floor_ids:
            print("対照の 2 実行に共通の article_id が無い", file=sys.stderr)
            return 1

    rows = _compare_rows(a, b, shared, floor_x, floor_y, floor_ids)

    if getattr(args, "json", False):
        # 機械可読出力 (較正格子 P2 の cron 化用)。article_id は件数のみ (本文非掲載と同方針)
        payload = {
            "label_a": args.label_a,
            "label_b": args.label_b,
            "shared": len(shared),
            "floor_shared": len(floor_ids) if args.floor else None,
            "fields": [
                {k: (len(v) if k == "changed_ids" else v) for k, v in r.items()}
                | {"changed": len(r["changed_ids"])}
                for r in rows
            ],
        }
        for f in payload["fields"]:
            f.pop("changed_ids", None)
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    head = f"{args.label_a} vs {args.label_b}  (共通 {len(shared)} 件)"
    if args.floor:
        head += f"\n対照: {args.floor[0]} vs {args.floor[1]}  (共通 {len(floor_ids)} 件)"
    else:
        head += "\n⚠ 対照なし — 差が効果か揺らぎか判定できません"
    print(head + "\n")
    print(f"{'フィールド':<22}{'一致':>6}{'床':>6}  {'充足率 A→B':>16}{'床の揺れ':>10}  判定")
    print("-" * 74)
    for r in rows:
        if "is_change" in r:
            verdict = "★ 変化" if r["is_change"] else "ノイズ内"
            floor_cell = f"{r['floor_agree'] * 100:>5.0f}%"
            floor_pt_cell = f"±{r['noise_pt']:>4.1f}pt"
        else:
            verdict = "?"
            floor_cell = f"{'-':>6}"
            floor_pt_cell = f"{'-':>8}"
        fill_cell = f"{r['fill_a'] * 100:>5.1f}→{r['fill_b'] * 100:>5.1f}%"
        print(
            f"{r['field']:<22}{r['agree'] * 100:>5.0f}%{floor_cell}  "
            f"{fill_cell:>16}{floor_pt_cell:>10}  {verdict}"
        )

    if args.show_ids:
        print("\n変化した article_id (フィールド別、先頭 10 件):")
        for r in rows:
            ids = r["changed_ids"]
            if ids:
                print(f"  {r['field']}: {', '.join(ids[:10])}{' …' if len(ids) > 10 else ''}")
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
        "--model",
        help="ティア解決を飛ばして使うモデル ref (例 claudecode:sonnet)。伸びしろの測定用",
    )
    r.add_argument(
        "--target",
        choices=sorted(_TARGETS),
        default="summarizer",
        help="評価する呼出 (summarizer / 取込時の judgment 分類器)",
    )
    r.add_argument(
        "--drop-field",
        action="append",
        help="判定基準と出力例からこのフィールドを外して実行する (DB は変更しない)",
    )
    r.add_argument(
        "--rubric-version",
        type=int,
        help="config_store の版履歴からこの版の rubric を pin して実行する (summarizer のみ)",
    )
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("compare", help="2 つの実行結果をフィールド別に突き合わせる")
    c.add_argument("label_a")
    c.add_argument("label_b")
    c.add_argument(
        "--floor",
        nargs=2,
        metavar=("LABEL", "LABEL"),
        help="同一設定の 2 実行 (揺らぎの床)。判定はこれを超えたかで行う",
    )
    c.add_argument(
        "--no-floor",
        action="store_true",
        help="床を取らずに生の差分だけ見る (判定はできない)",
    )
    c.add_argument("--show-ids", action="store_true", help="変化した article_id も出す")
    c.add_argument(
        "--json",
        action="store_true",
        help="機械可読 JSON で出力する (cron 化用。article_id は件数のみ)",
    )
    c.set_defaults(func=cmd_compare)

    args = p.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
