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


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Headline\nWelcome back to World Brief, where we’re looking at a heist.", True),
        ("Intro.\nThis Week in Cybersecurity: five stories.", True),
        ("Quarterly report. In this edition we cover three threats.", True),
        ("Kornilov wrote that in this issue the Dutch government failed.", False),
        ("Hackers stole $6 billion from the exchange on Thursday.", False),
    ],
)
def test_is_digest_article_body_markers(body: str, expected: bool) -> None:
    assert is_digest_article("題", "", "https://x.example/a", body) is expected


@pytest.mark.parametrize(
    ("title", "original", "body", "expected"),
    [
        ("北韓駭客鎖定216臺", "【資安日報】10月29日，北韓駭客", "", True),
        ("The World's Biggest Crypto Heist", "", "Welcome back to World Brief, where we're", True),
        # 強い印に入れない: 裸の「日報」・まとめの題の判定・Cyber Alert・日次アラートの URL
        ("韓国情報機関員の駐米赴任でビザ保留報道——朝鮮日報は指摘", "", "", False),
        ("Critical Capacitor Flaw Hits Framework With 5.5M Weekly Downloads", "", "", False),
        ("Cyber Alert Update Japan - Example Co., Ltd", "", "", False),
        ("Okta Blog 第21回 パスキーとは", "", "", False),
    ],
)
def test_is_newsletter_article_strong_markers_only(
    title: str, original: str, body: str, expected: bool
) -> None:
    from src.eventnews.digest_form import is_newsletter_article

    url = "https://cb.example/silobreaker-cyber-alert/1/"
    assert is_newsletter_article(title, original, url, body) is expected
