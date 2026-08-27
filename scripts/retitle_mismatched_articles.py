"""見出しが本文と噛み合わない記事を、既存の再処理経路で作り直す。

背景 (2026-08-28): The Register の本文抽出がナビ列に化けていた時期
(〜2026-08-21 の修正まで)、要約器はページ枠に載っていた別記事の見出し
(SharePoint ゼロデイ等) を読んで見出しを作っていた。接地検証は
「その本文に語がある」ので通ってしまう。本文は後から正しく取り直されたが、
**見出しは更新対象に入っていなかったため残った**。

新規の取り違えは抽出修正で止まっているので、これは残骸の掃除。
判定と再生成は行わず、**通常の再処理 (reprocess_article_body) をそのまま呼ぶ** —
見出しの作り直しと接地検証は既存経路が持っている。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from src.config_loader import load_app_config, load_llm_enrichment  # noqa: E402
from src.pipeline.dispatch import _load_template  # noqa: E402
from src.pipeline.reprocess import reprocess_article_body  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.content_extractor import ContentExtractor  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for  # noqa: E402


async def main() -> None:
    ids: list[str] = json.loads(Path(sys.argv[1]).read_text())
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(ids)
    repo = RunHistoryRepository()
    cfg = load_app_config()
    # 既存の常設ジョブ (body_refetch_backlog) と同じ組み立てを使う
    llm = build_llm_for(Step.ARTICLE_SUMMARY, cfg)
    enrichment = load_llm_enrichment()
    template = _load_template()
    counts: dict[str, int] = {}
    async with ContentExtractor() as extractor:
        for i, article_id in enumerate(ids[:limit], start=1):
            row = repo.get_article(article_id)
            if row is None:
                counts["missing"] = counts.get("missing", 0) + 1
                continue
            before = row.title
            try:
                result = await reprocess_article_body(
                    repo,
                    article_id,
                    row.url,
                    extractor=extractor,
                    llm=llm,
                    template=template,
                    enrichment=enrichment,
                )
            except Exception as e:  # noqa: BLE001 — 1 件の失敗で掃除全体を止めない
                counts["error"] = counts.get("error", 0) + 1
                print(f"[{i}/{limit}] ERROR {article_id}: {e}", flush=True)
                continue
            counts[result] = counts.get(result, 0) + 1
            after = (repo.get_article(article_id) or row).title
            if after != before:
                print(f"[{i}/{limit}] {before[:40]}\n      → {after[:40]}", flush=True)
            if i % 20 == 0:
                print(f"  … {i}/{limit} {counts}", flush=True)
    print(f"完了: {counts}", flush=True)


asyncio.run(main())
