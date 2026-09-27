"""月例更新・カタログ追加・注意喚起の類の見出しか (決定論、2026-09-27 に detect_ml から移設)。

勧告のまとめであって、1 つの出来事ではない。台帳の追跡単位にしない (detect、2026-09-15
「勧告は見張り」) / 事象どうしの関係では「まとめ」の側として扱う (eventnews.relation_features)。
判断層 (synthesis) と事象ニュース層の両方が使うので、どちらにも属さない tools に置く
(事象ニュース層は判断層を import しない — tests/unit/test_eventnews_boundary.py)。
"""

from __future__ import annotations

import re

ROLLUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"月例|定例|セキュリティ情報公開|セキュリティ更新プログラム"),
    re.compile(r"Patch Tuesday|Security Update Guide", re.IGNORECASE),
    re.compile(r"KEV カタログに追加|KEV に追加|Known Exploited Vulnerabilities"),
    re.compile(r"注意喚起を発信|注意喚起を公開|advisory roundup", re.IGNORECASE),
)


def is_rollup_title(title: str) -> bool:
    """月例・カタログ追加・注意喚起の記事か (追跡単位にしない、決定論)。"""
    return any(p.search(title) for p in ROLLUP_PATTERNS)
