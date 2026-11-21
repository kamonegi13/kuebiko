"""日報・アラートの形の記事を近さの統合候補から外す (2026-11-21)。"""

from __future__ import annotations

import pytest

from src.eventnews.digest_form import is_digest_article


@pytest.mark.parametrize(
    ("title", "original", "url", "expected"),
    [
        (
            "北韓駭客鎖定216臺PHP主機",
            "【資安日報】10月29日，北韓駭客鎖定216臺PHP主機",
            "https://x.example/1",
            True,
        ),
        (
            "ShinyHunters が主張",
            "",
            "https://codebook.example/threatreport/silobreaker-cyber-alert/47823/",
            True,
        ),
        ("World Brief: Binance", "", "https://fp.example/2026/10/30/x/", True),
        ("Ransomware Digest: Qilin が最多", "", "https://x.example/2", True),
        (
            "Binance、偽採用面接の経緯を公表",
            "Binance details fake interview",
            "https://x.example/3",
            False,
        ),
        ("ShinyHunters、FBI のポータル侵害を主張", "", "https://news.example/a", False),
    ],
)
def test_is_digest_article(title: str, original: str, url: str, expected: bool) -> None:
    assert is_digest_article(title, original, url) is expected
