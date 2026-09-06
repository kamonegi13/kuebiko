#!/usr/bin/env python3
"""triage の SFT 教師対を外部 LLM で生成する (多タスク SFT の 3 本目)。

triage のプロンプトは記事本文を含まず「タイトル + 要約」だけなので、article_summary の
数分の一のコストで対を作れる。多タスク混合で「どの課題か」を教えるのはプロンプトなので、
安い課題を足しておくほど「事象ニュースの書式に引きずられる」偏りが薄まる。

不変条件:
- **凍結 triage goldset の記事は除外する** (train/test 汚染の回避)。学習後に同じ
  goldset で測るため、混ざると測定が無意味になる。
- **``ArticleTriage.triage()`` は使わない**。あれは失敗時に medium を返す仕様なので、
  障害時の fallback が「教師の判断」として保存されてしまう。プロンプト組み立てだけ
  借りて ``generate_structured`` を直接呼び、失敗は失敗として落とす。
- ローカルへの fallback も無効化して実行する (``LLM_LOCAL_FALLBACK=0``)。

使用例 (コンテナ内):
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_sft_teacher_triage.py --model claudecode:opus --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.tools.article_model import Article  # noqa: E402
from src.tools.article_triage import ArticleTriage, TriageDecision  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

GOLDSET_PATH = Path("data/eval/triage_goldset.json")
DEFAULT_OUT = Path("data/mlx/teacher/triage.jsonl")

# 本番 ArticleTriage.triage() と同じ (src/tools/article_triage.py)。
_MAX_TOKENS = 400

_MAX_CONSECUTIVE_FAILURES = 5


def _excluded_ids() -> set[str]:
    if not GOLDSET_PATH.exists():
        return set()
    rows = json.loads(GOLDSET_PATH.read_text(encoding="utf-8"))["rows"]
    return {str(r["article_id"]) for r in rows}


def _fetch(days: int, limit: int) -> list[Article]:
    """triage 対象と同じ母集団。本文は使わないが、要約が揃った記事に限る。"""
    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository()
    sql = """
        SELECT id, title, body, feed_title, url, published_at
        FROM articles
        WHERE created_at > NOW() - INTERVAL '%s days'
          AND title IS NOT NULL AND title <> ''
          AND body IS NOT NULL AND body <> ''
          AND position(chr(65533) in body) = 0
        ORDER BY id
        LIMIT %s
    """
    with repo._connect() as conn:  # noqa: SLF001 — 読み取り専用
        rows = conn.execute(sql % (days, limit)).fetchall()
    from datetime import UTC, datetime

    fallback_published = datetime.now(UTC)
    out: list[Article] = []
    for r in rows:
        vals = list(dict(r).values()) if not isinstance(r, (list, tuple)) else list(r)
        aid, title, body, feed, url, published = vals[:6]
        # _build_prompt は body_text (無ければ summary_html) を preview に使う。
        # 本番の triage は本文取得後に走るので body_text 側を埋めて書式を一致させる。
        out.append(
            Article(
                id=str(aid),
                title=str(title or ""),
                url=str(url or ""),
                summary_html="",
                body_text=str(body or ""),
                # published は triage プロンプトに出ないが Article の必須項目。
                published=published or fallback_published,
                feed_title=str(feed or ""),
                feed_url="",
            )
        )
    return out


def _done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["article_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    excluded = _excluded_ids()
    print(f"評価 goldset の {len(excluded)} 件を学習対象から除外", file=sys.stderr)

    candidates = [a for a in _fetch(args.days, args.candidate_limit) if a.id not in excluded]
    done = _done_ids(args.out)
    # ⚠ ``--limit`` は **累計の目標件数** であって増分ではない (2026-09-05 修正)。
    # build_sft_teacher_summaries.py が累計で解釈するのに対し、ここだけ増分として
    # 解釈していたため、500 件済のところに --limit 750 を与えて 1250 件へ向かった。
    # 同じ名前の引数を姉妹スクリプトで別の意味にしない。
    remaining = max(0, args.limit - len(done))
    todo = [a for a in candidates if a.id not in done][:remaining]
    print(
        f"候補 {len(candidates)} / 済 {len(done)} / 目標 {args.limit} / 今回 {len(todo)}",
        file=sys.stderr,
    )

    llm = build_llm_for_ref(args.model, Step.TRIAGE, load_app_config())
    # プロンプト組み立てだけ本番から借りる (triage() は失敗時に medium を返すので使わない)。
    builder = ArticleTriage(llm)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = rejected = failed = 0
    consecutive = 0

    with args.out.open("a", encoding="utf-8") as fh:
        for i, art in enumerate(todo, start=1):
            prompt = builder._build_prompt(art)  # noqa: SLF001 — 本番と同一の書式を共有
            try:
                out = await llm.generate_structured(
                    prompt, schema=TriageDecision, think=False, max_tokens=_MAX_TOKENS
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                consecutive += 1
                print(f"  {i}/{len(todo)} FAIL {type(exc).__name__}: {str(exc)[:90]}", flush=True)
                if consecutive >= _MAX_CONSECUTIVE_FAILURES:
                    print("連続失敗が上限 — 中断 (rc=1)", file=sys.stderr)
                    return 1
                continue
            consecutive = 0
            if not out.reason.strip():
                rejected += 1
                continue
            # error は「LLM 障害で fail-open した」ことを示す内部フラグ。判断ではないので落とす。
            payload = {"importance": out.importance, "reason": out.reason}
            fh.write(
                json.dumps(
                    {
                        "article_id": art.id,
                        "prompt": prompt,
                        "completion": json.dumps(payload, ensure_ascii=False),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            ok += 1
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} ok={ok} 除外={rejected} 失敗={failed}", flush=True)

    print(f"\n完了: 採用 {ok} / 除外 {rejected} / 失敗 {failed} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--candidate-limit", type=int, default=20000)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
