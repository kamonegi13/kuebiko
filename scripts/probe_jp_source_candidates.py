"""Phase 5T-Y: 不足が疑われる日本系 source 候補の feed 実在 probe。

scripts/probe_feed_availability.py の WATCHERS を JP source 候補に差し替えた版。
出力は同じ markdown 形式。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import httpx  # noqa: E402
from probe_feed_availability import (  # noqa: E402
    TIMEOUT,
    USER_AGENT,
    probe_site,
    render_markdown,
)

CANDIDATES: list[tuple[str, str]] = [
    # 業界 aggregator (最重要)
    ("security_next", "https://www.security-next.com/"),
    ("scan_netsecurity", "https://scan.netsecurity.ne.jp/"),
    # IT メディア系
    ("itmedia_security", "https://www.itmedia.co.jp/enterprise/security/"),
    ("atmarkit_security", "https://atmarkit.itmedia.co.jp/ait/subtop/security/"),
    ("nikkei_xtech_security", "https://xtech.nikkei.com/security/"),
    # ベンダー JP blog
    ("nri_secure", "https://www.nri-secure.co.jp/blog"),
    ("trendmicro_jp", "https://www.trendmicro.com/ja_jp/research.html"),
    ("macnica_security", "https://www.macnica.co.jp/business/security/"),
    # 公的機関 / 業界協議体
    ("nca_gr_jp", "https://www.nca.gr.jp/"),  # NCA = 日本シーサート協議会
    ("npa_cyber", "https://www.npa.go.jp/bureau/cyber/"),
    ("fsa_go_jp", "https://www.fsa.go.jp/"),
]


async def main() -> None:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.9",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as client:
        tasks = [probe_site(client, name, site) for name, site in CANDIDATES]
        results = await asyncio.gather(*tasks)
    print(render_markdown(results))


if __name__ == "__main__":
    asyncio.run(main())
