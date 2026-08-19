"""PWA アイコン PNG を icon.svg から生成する (SSoT = frontend/public/pwa/icon.svg)。

PNG を手で置くと SVG との乖離に気付けないため、必ずこのスクリプトで再生成する。
Playwright (既存依存) で SVG を等倍レンダリングして screenshot する方式。

    uv run python scripts/generate_pwa_icons.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

PWA_DIR = Path("frontend/public/pwa")
SVG_PATH = PWA_DIR / "icon.svg"

# (出力ファイル名, 一辺 px)。180 = iOS apple-touch-icon、192/512 = manifest、32 = favicon 代替
TARGETS: tuple[tuple[str, int], ...] = (
    ("apple-touch-icon.png", 180),
    ("icon-192.png", 192),
    ("icon-512.png", 512),
    ("favicon-32.png", 32),
)


async def render_all() -> None:
    svg = SVG_PATH.read_text(encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            for filename, size in TARGETS:
                page = await browser.new_page(viewport={"width": size, "height": size})
                # margin/背景を殺して SVG だけを viewport 一杯に敷く
                await page.set_content(
                    "<style>html,body{margin:0;padding:0;background:#0b0d11}"
                    f"svg{{display:block;width:{size}px;height:{size}px}}</style>{svg}"
                )
                out = PWA_DIR / filename
                await page.screenshot(path=str(out), omit_background=False)
                await page.close()
                print(f"  {out} ({size}x{size})")
        finally:
            await browser.close()


def main() -> None:
    if not SVG_PATH.exists():
        raise SystemExit(f"not found: {SVG_PATH}")
    print(f"rendering from {SVG_PATH}")
    asyncio.run(render_all())


if __name__ == "__main__":
    main()
