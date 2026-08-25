"""注目の採点 (行動要度)。

2026-08-25 に利用者から「報道量の多さと重要性は合致しない」と指摘され、
媒体数順から作り直した。**媒体数は一切使わない**。
"""

from __future__ import annotations

import inspect

from src.eventnews import urgency


class TestExploitedDetection:
    def test_detects_actual_exploitation(self) -> None:
        for text in (
            "本脆弱性は実際に悪用されている。",
            "CISA は悪用を確認し KEV カタログへ追加した。",
            "The flaw is actively exploited in the wild.",
            "ゼロデイ攻撃に使われた。",
        ):
            assert urgency.looks_exploited(text), text

    def test_rejects_hypothetical_exploitation(self) -> None:
        """⚠ 「悪用される**可能性**」を拾うと過検出になる。"""
        for text in (
            "悪用される可能性がある。",
            "悪用されると情報が漏えいする恐れがある。",
            "This could be exploited by an attacker.",
            "There is a potential for exploitation in the wild.",
        ):
            assert not urgency.looks_exploited(text), text

    def test_empty_text_is_not_exploited(self) -> None:
        assert not urgency.looks_exploited("")


class TestScoring:
    def base(
        self,
        *,
        text: str = "",
        max_cvss: float = 0.0,
        japan_related: bool = False,
        ransomware: bool = False,
        pir_count: int = 0,
    ) -> int:
        return urgency.urgency_score(
            text=text,
            max_cvss=max_cvss,
            japan_related=japan_related,
            ransomware=ransomware,
            pir_count=pir_count,
        )

    def test_exploitation_outranks_everything_else_alone(self) -> None:
        """「今すぐ対処が要る」が最強の信号。"""
        assert self.base(text="実際に悪用されている") > self.base(max_cvss=10.0)
        assert self.base(text="実際に悪用されている") > self.base(japan_related=True)

    def test_japan_outranks_a_critical_cvss(self) -> None:
        """読み手の所在。実測で日本関連は公開対象の 4.5% しかなく埋もれていた。"""
        assert self.base(japan_related=True) > self.base(max_cvss=10.0)

    def test_cvss_tiers_are_ordered(self) -> None:
        assert self.base(max_cvss=9.5) > self.base(max_cvss=7.5) > self.base(max_cvss=5.0)

    def test_pir_matches_are_capped(self) -> None:
        """PIR は多く当たるほど良いわけではない (何十件も当たる語がある)。"""
        assert self.base(pir_count=99) == self.base(pir_count=urgency.MAX_PIR_COUNTED)

    def test_negative_pir_count_is_ignored(self) -> None:
        assert self.base(pir_count=-5) == self.base(pir_count=0)

    def test_nothing_scores_zero(self) -> None:
        assert self.base() == 0


class TestBoundaries:
    def test_media_count_is_not_a_parameter(self) -> None:
        """⚠ 媒体数を採点に入れない。入れれば結局は収集量が順位を決める。"""
        params = set(inspect.signature(urgency.urgency_score).parameters)
        assert params == {"text", "max_cvss", "japan_related", "ransomware", "pir_count"}
        src = inspect.getsource(urgency)
        assert "independent_sources" not in src

    def test_roundups_are_excluded(self) -> None:
        """まとめ記事は複数の話題を含み、信号が同時に立って点が跳ね上がる。"""
        from types import SimpleNamespace

        assert urgency.is_excluded_category([SimpleNamespace(category="recap")])
        assert not urgency.is_excluded_category([SimpleNamespace(category="vulnerability")])
        assert not urgency.is_excluded_category([])
