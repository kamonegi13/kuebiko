#!/usr/bin/env python3
"""article_summary の SFT 教師対を外部 LLM で生成する (多タスク SFT の材料)。

単一タスク (事象ニュース) だけで学習した SFT モデルは、**学習時に見ていない
スキーマ**に直面すると縮退した (同一 ID の重複 56.8% / 空欄に文字列 "null")。
教師データ側は重複ゼロなので学習で覚えたのではなく、その課題を一度も見ていない
ことが原因。よって article_summary の対を作って混ぜる。

不変条件:
- **評価用 goldset の記事は除外する** (train/test 汚染の回避)。学習後に同じ
  goldset で測るため、混ざると測定が無意味になる。
- **ローカルへの fallback を無効化して実行する** (``LLM_LOCAL_FALLBACK=0``)。
  外部が落ちたとき黙ってローカル出力が「教師」として保存されるのが最悪の事故。
- プロンプトは**本番と同一のテンプレート**で組む (学習と推論の書式を一致させる)。

使用例 (コンテナ内で実行 — rubric の SSoT は DB):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_summaries.py --model claudecode:opus --limit 800
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.eval.goldset import GoldArticle, load_goldset, select_goldset  # noqa: E402
from src.pipeline.summary import SummaryOutput  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

GOLDSET_PATH = Path("data/eval/goldset.jsonl")
DEFAULT_OUT = Path("data/mlx/teacher/article_summary.jsonl")

# 教師として保存してはいけない値 (学習させると生徒が真似る)。
_PLACEHOLDERS = {"null", "none", "n/a", "na", "不明", "なし", "-"}

# 連続失敗がこの数に達したら中断する (率でなく連続数で見る — 恒久障害の早期検出)。
_MAX_CONSECUTIVE_FAILURES = 5


def _fetch_candidates(days: int, limit: int) -> list[GoldArticle]:
    """eval_goldset と同じ母集団 (本文と分類が揃った記事) を広めの窓で取る。"""
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    sql = """
        SELECT id, title, body, feed_title, published_at, category, importance
        FROM articles
        WHERE created_at > NOW() - INTERVAL '%s days'
          AND body IS NOT NULL AND body <> ''
          AND summary IS NOT NULL AND summary <> ''
          AND category IS NOT NULL AND importance IS NOT NULL
          -- 文字化け本文を除外 (2026-09-04 実測: 28,378 件中 241 件が U+FFFD を含み、
          -- ほぼ全件が @IT / ITmedia の 2 feed に集中する取り込み側の不具合)。教師に
          -- 混ぜると「化けた本文から推測する」振る舞いを学ばせてしまう。
          AND position(chr(65533) in body) = 0
        ORDER BY id
        LIMIT %s
    """
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
        rows = conn.execute(sql % (days, limit)).fetchall()
    out: list[GoldArticle] = []
    for r in rows:
        vals = list(dict(r).values()) if not isinstance(r, (list, tuple)) else list(r)
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


def _reject_reason(payload: dict[str, Any]) -> str | None:
    """教師として不適格な出力を弾く。黙って直さず、理由を返して除外する。"""
    for key, value in payload.items():
        values = value if isinstance(value, list) else ([value] if value else [])
        flat = [v for v in values if isinstance(v, str)]
        if any(v.strip().casefold() in _PLACEHOLDERS for v in flat):
            return f"{key} に placeholder 文字列"
        if isinstance(value, list) and len(flat) != len(set(flat)) and flat:
            return f"{key} に重複項目"
    return None


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["article_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    import importlib.util

    spec = importlib.util.spec_from_file_location("eg", "scripts/eval_goldset.py")
    assert spec and spec.loader
    eg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eg)
    template = eg._build_template([], None)  # noqa: SLF001 — 本番と同一の組み立てを共有

    excluded = {a.article_id for a in load_goldset(GOLDSET_PATH)}
    print(f"評価 goldset の {len(excluded)} 件を学習対象から除外", file=sys.stderr)

    candidates = [
        a
        for a in _fetch_candidates(args.days, args.candidate_limit)
        if a.article_id not in excluded
    ]
    picked = select_goldset(candidates, per_stratum=args.per_stratum)[: args.limit]
    done = _done_ids(args.out)
    todo = [a for a in picked if a.article_id not in done]
    print(
        f"候補 {len(candidates)} → 層化抽出 {len(picked)} / 済 {len(done)} / 今回 {len(todo)}",
        file=sys.stderr,
    )

    llm = build_llm_for_ref(args.model, Step.ARTICLE_SUMMARY, load_app_config())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = rejected = failed = 0
    consecutive = 0

    with args.out.open("a", encoding="utf-8") as fh:
        for i, art in enumerate(todo, start=1):
            prompt = template.render(article=art, body=art.body)
            try:
                out = await llm.generate_structured(prompt, schema=SummaryOutput, think=False)
            except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を落とさない
                failed += 1
                consecutive += 1
                print(f"  {i}/{len(todo)} FAIL {type(exc).__name__}: {str(exc)[:90]}", flush=True)
                if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                    print("連続失敗が上限に達したため中断 (経路の恒久障害を疑う)", file=sys.stderr)
                    break
                continue
            consecutive = 0
            payload = out.model_dump()
            reason = _reject_reason(payload)
            if reason is not None:
                rejected += 1
                print(f"  {i}/{len(todo)} 除外 ({reason})", flush=True)
                continue
            fh.write(
                json.dumps(
                    {
                        "article_id": art.article_id,
                        "prompt": prompt,
                        "completion": json.dumps(payload, ensure_ascii=False),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            ok += 1
            if i % 20 == 0:
                print(f"  {i}/{len(todo)} ok={ok} 除外={rejected} 失敗={failed}", flush=True)

    print(f"\n完了: 採用 {ok} / 除外 {rejected} / 失敗 {failed} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="教師モデル ref (例 claudecode:opus)")
    ap.add_argument("--limit", type=int, default=800, help="生成する上限件数")
    ap.add_argument("--days", type=int, default=120, help="候補を取る期間")
    ap.add_argument("--candidate-limit", type=int, default=20000)
    ap.add_argument(
        "--per-stratum", type=int, default=40, help="(importance x category) ごとの上限"
    )
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
