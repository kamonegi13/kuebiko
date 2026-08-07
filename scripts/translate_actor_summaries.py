"""Actor 辞書の英語 summary をローカル LLM で日本語に一括翻訳する (Stage 4 backfill)。

Stage 2 の enrich で MITRE 英語 summary がそのまま入った actor を対象に、
ローカル Ollama (OLLAMA_MAIN_MODEL) で和訳する。固有名詞 (アクター名・マルウェア/
ツール名等) と MITRE ID / CVE は原文のまま維持 (mitre_sync.TRANSLATE_SYSTEM_PROMPT)。

翻訳と同時に ``mitre_summary_sha1`` (翻訳元 = MITRE 英語 summary の sha1) を記録し、
以後の週次 mitre-actor-sync が「MITRE 側で summary が変わったときのみ再翻訳」できる
ようにする。

使い方:
    uv run python scripts/translate_actor_summaries.py            # review 出力 (.translated.yaml)
    uv run python scripts/translate_actor_summaries.py --apply    # config/actor_aliases.yaml に適用
    uv run python scripts/translate_actor_summaries.py --limit 3  # 動作確認 (先頭 3 件のみ)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config_loader import load_app_config  # noqa: E402
from src.cti.actor_editor import load_actors_raw, render_actors_yaml  # noqa: E402
from src.cti.mitre_sync import translate_summary  # noqa: E402
from src.tools.llm_client import OllamaClient  # noqa: E402

# 英語判定: ASCII 文字が大半なら未翻訳とみなす (和訳済は仮名・漢字で ratio が下がる)
_ASCII_RATIO_THRESHOLD = 0.9
_MIN_SUMMARY_LENGTH = 40


def _looks_english(text: str) -> bool:
    if len(text) < _MIN_SUMMARY_LENGTH:
        return False
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return ascii_chars / len(text) >= _ASCII_RATIO_THRESHOLD


async def _run(apply: bool, limit: int | None) -> None:
    config = load_app_config()
    llm = OllamaClient(
        base_url=config.ollama_base_url,
        model=config.ollama_main_model,
    )
    data = load_actors_raw()
    actors: list[dict[str, Any]] = data["actors"]

    targets = [
        a
        for a in actors
        if isinstance(a, dict) and a.get("summary") and _looks_english(str(a["summary"]))
    ]
    if limit is not None:
        targets = targets[:limit]
    print(f"翻訳対象: {len(targets)} 件 (model={config.ollama_main_model})", file=sys.stderr)

    failed: list[str] = []
    for i, actor in enumerate(targets, 1):
        original = str(actor["summary"])
        translated = await translate_summary(llm, original)
        if translated == original:
            failed.append(str(actor.get("id")))
            print(f"  [{i}/{len(targets)}] ✗ {actor.get('canonical')} (翻訳失敗)", file=sys.stderr)
            continue
        actor["summary"] = translated
        actor["mitre_summary_sha1"] = hashlib.sha1(original.encode("utf-8")).hexdigest()
        print(f"  [{i}/{len(targets)}] ✓ {actor.get('canonical')}", file=sys.stderr)

    print(f"\n翻訳完了: {len(targets) - len(failed)} 件 / 失敗: {len(failed)} 件", file=sys.stderr)
    if failed:
        print(f"  失敗 actor: {', '.join(failed)} (再実行で再試行可)", file=sys.stderr)

    content = render_actors_yaml(data)
    if apply:
        Path("config/actor_aliases.yaml").write_text(content, encoding="utf-8")
        print("\n✅ config/actor_aliases.yaml に適用しました", file=sys.stderr)
    else:
        out = Path("config/actor_aliases.translated.yaml")
        out.write_text(content, encoding="utf-8")
        print(f"\n📝 review 出力: {out} (確認後 --apply で適用)", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="config/actor_aliases.yaml に直接書き込む")
    ap.add_argument("--limit", type=int, default=None, help="先頭 N 件のみ翻訳 (動作確認用)")
    args = ap.parse_args()
    asyncio.run(_run(apply=args.apply, limit=args.limit))


if __name__ == "__main__":
    main()
