"""1 PIR について 26B vs 31B で Spotlight を並列生成し比較する。

実行例:
    uv run python scripts/compare_spotlight_models.py pir_china_apt

出力:
    /tmp/spotlight_compare_<pir_id>_<timestamp>.md に 2 model の出力を併記
    stdout に時間 / 文字数 / 出力サマリ
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from src.config_loader import load_app_config
from src.pir.loader import load_pir_config
from src.spotlight.generator import generate_spotlight
from src.spotlight.models import SpotlightRecord
from src.tools.llm_client import OllamaClient


async def run_one(pir, model: str) -> tuple[SpotlightRecord | None, float]:
    cfg = load_app_config()
    llm = OllamaClient(
        base_url=cfg.ollama_base_url,
        model=model,
        timeout_seconds=900.0,
    )
    t0 = time.monotonic()
    rec = await generate_spotlight(pir, llm=llm, period_type="weekly")
    elapsed = time.monotonic() - t0
    return rec, elapsed


def render_md(pir_id: str, results: dict[str, tuple[SpotlightRecord | None, float]]) -> str:
    lines = [f"# Spotlight Compare: {pir_id}\n"]
    for model, (rec, elapsed) in results.items():
        lines.append(f"## {model}")
        lines.append(f"\n- duration: {elapsed:.1f}s")
        if rec is None:
            lines.append("- ⚠ generate failed (no matches or error)")
            continue
        lines.append(f"- article_count: {rec.article_count}")
        lines.append(f"- key_events: {len(rec.key_events)}")
        lines.append(f"- headline chars: {len(rec.headline)}")
        lines.append(f"- outlook chars: {len(rec.outlook)}")
        lines.append("\n### Headline")
        lines.append(rec.headline)
        lines.append("\n### Outlook")
        lines.append(rec.outlook)
        lines.append("\n### Key events")
        for ke in rec.key_events:
            lines.append(f"- [{ke.importance}] {ke.title} ({ke.feed_title})")
        lines.append("\n---\n")
    return "\n".join(lines)


async def main_async(pir_id: str, models: list[str]) -> int:
    load_dotenv()
    pir_cfg = load_pir_config()
    pir = pir_cfg.find(pir_id)
    if pir is None:
        print(f"ERROR: PIR not found: {pir_id}", file=sys.stderr)
        return 1
    if not pir.spotlight.enabled:
        print(f"WARN: PIR {pir_id} の spotlight.enabled=false ですが、比較は続行します")

    results: dict[str, tuple[SpotlightRecord | None, float]] = {}
    for model in models:
        print(f"=== generating with {model}... ===")
        try:
            rec, elapsed = await run_one(pir, model)
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR: {type(e).__name__}: {e}")
            results[model] = (None, 0.0)
            continue
        print(f"  done in {elapsed:.1f}s")
        if rec:
            print(f"  headline: {rec.headline[:80]}...")
            print(f"  key_events: {len(rec.key_events)}, outlook chars: {len(rec.outlook)}")
        else:
            print("  no match articles")
        results[model] = (rec, elapsed)

    # output report
    md = render_md(pir_id, results)
    out_path = Path(f"/tmp/spotlight_compare_{pir_id}_{int(time.time())}.md")
    out_path.write_text(md, encoding="utf-8")
    print(f"\nreport saved: {out_path}")
    print(f"  view with: less {out_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pir_id", help="PIR id (例: pir_china_apt)")
    parser.add_argument(
        "--models",
        default="gemma4:26b,gemma4:31b",
        help="比較する model list (comma 区切り、default 26B + 31B)",
    )
    args = parser.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    sys.exit(asyncio.run(main_async(args.pir_id, models)))


if __name__ == "__main__":
    main()
