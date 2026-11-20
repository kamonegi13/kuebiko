"""ボット確認・アクセス拒否の画面の判定 (SSoT、2026-11-20 に briefing から移した)。

本文の関門 (``briefing._degenerate_body_reason``)・地政学の救済の入力 (``geo_rescue_judge``)・
triage の材料 (``pipeline/triage_material``) が同じ判定を使う。語の一覧はここだけに置く。

画面は短い。長い本文に語が出るのは記事の中身 (例: Cloudflare についての記事) とみなすため、
``BLOCK_PAGE_MAX_LEN`` 字以下の本文の先頭 ``BLOCK_PAGE_SCAN_CHARS`` 字だけを見る。
"""

from __future__ import annotations

import re

BLOCK_PAGE_RE = re.compile(
    r"security service to protect (?:itself|against)|enable javascript and cookies|"
    r"checking your browser|verify you are human|are you a robot|"
    r"just a moment\.\.\.|attention required!|access to this page has been denied|"
    r"please complete the security check|cf-browser-verification|"
    # 11-20: 本番で取れた画面の文面 (Cloudflare の確認画面、11-19 監査 D2 P1-8a)
    r"performing security verification|verifies you are not a bot|"
    r"needs to review the security of your connection",
    re.IGNORECASE,
)
BLOCK_PAGE_SCAN_CHARS = 600
BLOCK_PAGE_MAX_LEN = 2000


def is_block_page(text: str) -> bool:
    """本文 (前後の空白は除く) が確認・拒否の画面なら True。"""
    body = (text or "").strip()
    return len(body) <= BLOCK_PAGE_MAX_LEN and bool(
        BLOCK_PAGE_RE.search(body[:BLOCK_PAGE_SCAN_CHARS])
    )


__all__ = ["BLOCK_PAGE_MAX_LEN", "BLOCK_PAGE_RE", "BLOCK_PAGE_SCAN_CHARS", "is_block_page"]
