"""adversary_official 区分 (D1)・IOC の関門・本文の枠 (untrusted framing) のテスト。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from src.cti import adversary_claims as ac
from src.cti.source_basis import (
    _TIER_LABEL,
    SOURCE_TIER_MARKER,
    classify_source_tier,
    invalidate_reliability_cache,
)
from src.grok.link_fetch import _SECTION_HEADER
from src.pipeline.briefing import _frame_untrusted_body
from src.pipeline.persistence import _hold_adversary_ioc
from src.tools.article_model import Article
from src.tools.untrusted_text import UNTRUSTED_TEXT_NOTICE, wrap_adversary_body

# 実在の機関名を公開リポに書かないため、分類はローカル層 (config/local/) の placeholder で試す
_LOCAL_RELIABILITY = """
adversary_official:
  hosts: [ministry.kuebiko.example]
state_media:
  hosts: [state.kuebiko.example]
research:
  hosts: [vendor.kuebiko.example]
"""
MFA = "https://www.ministry.kuebiko.example/eng/rss.xml"
# ccTLD (.ru) で hostile になる host (実在しない example のサブドメイン)
HOSTILE_FEED = "https://news.example.ru/rss"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.delenv(ac.ENV_IOC_GATE, raising=False)
    monkeypatch.delenv(ac.ENV_PROMPT_FRAMING, raising=False)
    monkeypatch.delenv("ADVERSARY_TIER", raising=False)
    monkeypatch.setenv("KUEBIKO_LOCAL_CONFIG_DIR", str(tmp_path))
    (tmp_path / "source_reliability.yaml").write_text(_LOCAL_RELIABILITY, encoding="utf-8")
    invalidate_reliability_cache()
    yield
    invalidate_reliability_cache()


class TestTier:
    def test_default_shadow_keeps_effective_tier_unchanged(self) -> None:
        # ADVERSARY_TIER 未設定 = shadow: 一致は計算するが実効ティアは従来のまま
        assert classify_source_tier("MFA", MFA) == "news"
        assert ac.is_adversary_official("MFA", MFA)

    def test_flag_off_keeps_tier_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADVERSARY_TIER", "0")
        assert classify_source_tier("MFA", MFA) == "news"

    def test_official_government_host_of_adversary_is_labeled_as_claim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADVERSARY_TIER", "1")
        tier = classify_source_tier("MFA", MFA)
        assert tier == "adversary_official"
        assert _TIER_LABEL["adversary_official"] == "主張"
        assert "主張" in SOURCE_TIER_MARKER["adversary_official"]

    def test_state_media_and_vendors_keep_their_existing_tiers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADVERSARY_TIER", "1")
        assert classify_source_tier("State", "https://www.state.kuebiko.example/rss") == (
            "state_media"
        )
        assert classify_source_tier("Vendor", "https://vendor.kuebiko.example/feed/") == "research"

    def test_ordinary_official_is_unchanged(self) -> None:
        assert classify_source_tier("CISA", "https://www.cisa.gov/feed") == "official"


class TestIocGate:
    def test_default_is_shadow_and_does_not_hold(self) -> None:
        assert ac.ioc_gate_mode() == "shadow"
        assert not _hold_adversary_ioc("MFA", MFA, "ioc_ip", "a1")

    def test_on_holds_only_adversary_official_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ac.ENV_IOC_GATE, "1")
        assert _hold_adversary_ioc("MFA", MFA, "ioc_ip", "a1")
        assert not _hold_adversary_ioc("CISA", "https://www.cisa.gov/feed", "ioc_ip", "a2")

    def test_off_never_holds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ac.ENV_IOC_GATE, "0")
        assert not _hold_adversary_ioc("MFA", MFA, "ioc_ip", "a1")


class TestFraming:
    def test_needed_for_adversary_official_and_hostile_hosts(self) -> None:
        assert ac.needs_untrusted_framing("MFA", MFA)
        assert ac.needs_untrusted_framing("News", HOSTILE_FEED, "https://news.example.ru/a/1")
        assert not ac.needs_untrusted_framing("CISA", "https://www.cisa.gov/feed", "")

    def test_wrapper_reuses_the_shared_instruction_notice(self) -> None:
        wrapped = wrap_adversary_body("本文")
        assert UNTRUSTED_TEXT_NOTICE in wrapped
        assert wrapped.endswith("本文")
        assert wrap_adversary_body("") == ""

    def test_grok_link_header_still_carries_the_same_notice(self) -> None:
        # 文言を共通化しても Grok 参照先の節の中身は変わらない
        assert UNTRUSTED_TEXT_NOTICE in _SECTION_HEADER
        assert "投稿の内容が主で、参照先は補足。" in _SECTION_HEADER


class TestBriefingFraming:
    def _article(self, feed_url: str, url: str) -> Article:
        return cast(
            Article,
            SimpleNamespace(feed_title="t", feed_url=feed_url, url=url),
        )

    def test_shadow_leaves_body_unchanged(self) -> None:
        art = self._article(HOSTILE_FEED, "https://news.example.ru/a")
        assert _frame_untrusted_body(art, "BODY") == "BODY"

    def test_on_wraps_hostile_body_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ac.ENV_PROMPT_FRAMING, "1")
        hostile = self._article(HOSTILE_FEED, "https://news.example.ru/a")
        ordinary = self._article("https://www.cisa.gov/feed", "https://www.cisa.gov/a")
        assert UNTRUSTED_TEXT_NOTICE in _frame_untrusted_body(hostile, "BODY")
        assert _frame_untrusted_body(ordinary, "BODY") == "BODY"
