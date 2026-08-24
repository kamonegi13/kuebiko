"""タイトルの無い source (X/Grok) の見出しを作り直す。

2026-08-24 の修正 (`NO_TITLE_SENTINEL`) は **これから取り込む記事** にしか効かない。
既存の x.com 記事は「投稿本文の先頭 120 字」がタイトルのままで、日本語の投稿では
特に読めない (実測 913 件中 286 件 = 31%)。

**本番と同じ経路で作り直す** — 同じテンプレ (合成版) / 同じ schema / 同じ接地検証。
別実装で作ると、以後に取り込む記事と見出しの作られ方が変わってしまう。

実行例:
  docker compose exec kuebiko python scripts/backfill_grok_titles.py --limit 5
  docker compose exec kuebiko python scripts/backfill_grok_titles.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config
from src.pipeline.briefing import NO_TITLE_SENTINEL
from src.pipeline.summary import SummaryOutput, ungrounded_title_tokens
from src.prompts.rubric_store import build_summarizer_template
from src.prompts.summarizer_composer import LEGACY_TEMPLATE_PATH
from src.storage.run_history import RunHistoryRepository
from src.tools.model_tiers import Step, build_llm_for

# 「投稿本文がそのままタイトルになっている」判定。
# ⚠ ハッシュタグ単体は根拠にしない — 「NoName057(16)、#OpJapan キャンペーンの一環として…」
# のように、正しい見出しにハッシュタグが入ることがある (実データで誤検出した)。
# 見出しには現れないもの (絵文字・URL・改行) と、切り詰め上限 (120 字) 近くの長さ、
# 日本語の告知に典型的な 【 始まりを根拠にする。
_RAW_LIKE = re.compile(r"[\U0001F300-\U0001FAFF]|https?://|\n")
_RAW_LEN = 100


def _looks_raw(title: str) -> bool:
    return bool(_RAW_LIKE.search(title)) or len(title) >= _RAW_LEN or title.startswith("【")


async def _main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き換える (既定は dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="処理する最大件数")
    ap.add_argument("--all", action="store_true", help="投稿本文らしくない見出しも作り直す")
    args = ap.parse_args()

    repo = RunHistoryRepository()
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT article_id AS article_id, title AS title, COALESCE(summary,'') AS summary"
            ", COALESCE(feed_title,'') AS feed_title"
            " FROM articles WHERE POSITION(? IN url) > 0 ORDER BY created_at DESC",
            ("//x.com/",),
        ).fetchall()
    targets = [r for r in rows if args.all or _looks_raw(str(r["title"]))]
    if args.limit:
        targets = targets[: args.limit]
    print(f"x.com 記事 {len(rows)} 件 / 作り直す対象 {len(targets)} 件", flush=True)
    if not args.apply:
        for r in targets[:5]:
            print(f"  例: {str(r['title'])[:70]}")
        print("(dry-run — --apply で書き換え)")
        return

    template = build_summarizer_template(Path(LEGACY_TEMPLATE_PATH))
    if template is None:
        raise SystemExit("summarizer テンプレを構築できない")
    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())

    done = skipped = failed = 0
    t0 = time.monotonic()
    for i, r in enumerate(targets, start=1):
        article_id = str(r["article_id"])
        body = repo.get_article_body(article_id) or str(r["summary"])
        if len(body.strip()) < 20:
            skipped += 1
            continue
        # 本番と同じ: 原タイトル欄は sentinel、本文はそのまま
        feed_title = str(r["feed_title"])
        article = type("A", (), {"title": NO_TITLE_SENTINEL, "feed_title": feed_title})()
        try:
            out = await llm.generate_structured(
                prompt=template.render(article=article, body=body[:4000]),
                schema=SummaryOutput,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を止めない
            print(f"  [{i}/{len(targets)}] 失敗 {type(exc).__name__}", flush=True)
            failed += 1
            continue
        new_title = (out.title_ja or "").strip()
        # 接地検証 (本番と同じ): 本文に無い固有名詞を含む見出しは採用しない
        # 接地材料に媒体名 (投稿者) を含める — 本番と同じ (X では組織名がアカウント名に
        # しか現れないことがあり、正しい見出しが未接地として弾かれる)。
        grounding = f"{feed_title} {r['title']} {body[:5000]}"
        if not new_title or ungrounded_title_tokens(new_title, grounding):
            skipped += 1
            continue
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                "UPDATE articles SET title = ? WHERE article_id = ?", (new_title[:120], article_id)
            )
        done += 1
        if i % 20 == 0:
            print(
                f"  [{i}/{len(targets)}] 更新 {done} / skip {skipped} / 失敗 {failed}", flush=True
            )
    print(
        f"更新 {done} / 接地不足・素材不足で skip {skipped} / 失敗 {failed}"
        f" ({time.monotonic() - t0:.0f}s)",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(_main())
