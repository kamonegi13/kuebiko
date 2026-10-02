"""日次ブリーフ構造化 payload (src/digest/brief_payload.py) の unit test。

Web はテキストでなく構造から描画する (2026-07-12)。builder が canonical な構造
(StatusSynthesisRecord / PirFocusSection) を JSON-safe な payload に正しく射影するかを検証。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from src.digest.brief_payload import build_brief_payload
from src.digest.pir_daily_focus import PirFocusSection
from src.pir.evaluator import PirMatch
from src.pir.models import Pir, SpotlightConfig, StrongSignals
from src.storage.run_history import StatusSynthesisRecord


def _synthesis(*, tradecraft: str = "") -> StatusSynthesisRecord:
    ts = datetime(2026, 7, 12, tzinfo=UTC)
    return StatusSynthesisRecord(
        period_type="daily",
        period_start=ts,
        period_end=ts,
        headline="決定的な変化は観測されていない",
        weight_section="【強化】ランサム集団の規模拡大 (中)。【新規】KVM 脆弱性 (低)。",
        chain_section="",
        cog_section="重心はエッジ機器",
        spillover_section="周辺国にも波及",
        pir_section="【防衛】AI ツールの RCE リスク (高確度)",
        axes_evidence="{}",
        tradecraft=tradecraft,
        article_count=144,
    )


def _pir_section() -> PirFocusSection:
    pir = Pir(
        id="pir_china_apt",
        title="中国 APT 動向",
        description="d",
        enabled=True,
        strong_signals=StrongSignals(keywords=["apt"]),
        spotlight=SpotlightConfig(enabled=False),
    )
    match = PirMatch(
        article_id="a1",
        title="Volt Typhoon が通信へ",
        url="https://example.com/a1",
        feed_title="CISA",
        importance="high",
        posted_channel="brief",
        created_at="2026-07-12T00:00:00+00:00",
        matched_via=("keywords",),
    )
    return PirFocusSection(pir=pir, matches=[match], total_match_count=4, llm_summary="要点。")


class TestSynthesisPayload:
    def test_sections_keep_only_nonempty_with_stripped_labels(self) -> None:
        payload = build_brief_payload(syn_record=_synthesis(), sections=[])
        syn = payload["synthesis"]
        assert syn is not None
        assert syn["headline"] == "決定的な変化は観測されていない"
        keys = [s["key"] for s in syn["sections"]]
        assert "chain_section" not in keys  # 空節は除外
        weight = next(s for s in syn["sections"] if s["key"] == "weight_section")
        assert weight["label"].startswith("比重")  # 「■ 」prefix は剥がす
        assert not weight["label"].startswith("■")

    def test_none_record_gives_null_synthesis(self) -> None:
        payload = build_brief_payload(syn_record=None, sections=[])
        assert payload["synthesis"] is None
        assert payload["pir"] == []

    def test_tradecraft_parsed_with_known_keys_only(self) -> None:
        tc = json.dumps(
            {
                "leading_assessment": "主見立て文",
                "alternatives": ["別解1", " "],
                "indicators": ["指標1"],
                "unknown_key": ["漏れてはいけない"],
            }
        )
        payload = build_brief_payload(syn_record=_synthesis(tradecraft=tc), sections=[])
        parsed = payload["synthesis"]["tradecraft"]
        assert parsed == {
            "leading_assessment": "主見立て文",
            "alternatives": ["別解1"],
            "indicators": ["指標1"],
        }

    def test_broken_tradecraft_is_none(self) -> None:
        payload = build_brief_payload(syn_record=_synthesis(tradecraft="{oops"), sections=[])
        assert payload["synthesis"]["tradecraft"] is None


class TestPirPayload:
    def test_matches_carry_link_importance_and_tier(self) -> None:
        payload = build_brief_payload(syn_record=None, sections=[_pir_section()])
        assert len(payload["pir"]) == 1
        sec = payload["pir"][0]
        assert sec["title"] == "中国 APT 動向"
        assert sec["total"] == 4
        assert sec["summary"] == "要点。"
        m = sec["matches"][0]
        assert m["title"] == "Volt Typhoon が通信へ"
        assert m["url"] == "https://example.com/a1"
        assert m["importance"] == "high"
        assert m["feed"] == "CISA"
        assert isinstance(m["tier"], str) and m["tier"]

    def test_payload_is_json_serializable(self) -> None:
        payload = build_brief_payload(syn_record=_synthesis(), sections=[_pir_section()])
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


class TestPirBriefPayload:
    def test_absent_pir_brief_is_null(self) -> None:
        assert build_brief_payload(syn_record=None, sections=[])["pir_brief"] is None

    def test_pir_brief_is_embedded(self) -> None:
        from datetime import UTC, datetime

        from src.digest.pir_brief import build_pir_brief

        brief = build_pir_brief(
            [{"situation_id": "a", "assessed": True, "question": "問い", "trajectory": []}],
            now=datetime(2026, 9, 29, tzinfo=UTC),
        )

        payload = build_brief_payload(syn_record=None, sections=[], pir_brief=brief)

        assert payload["pir_brief"]["headline"] == "本日、1 問中 0 問の答えが動いた"
        json.dumps(payload, ensure_ascii=False)


class TestHighThreatsPayload:
    """高脅威の安全網を Web のブリーフにも載せる (2026-10-02)。

    従来は Discord の要点にだけ出て、保存したブリーフ (Web) には入らなかった。
    alert に流れなかった high の脅威 (JadePuffer/Storm-3168 の 7 本など) が Web のどこにも
    出ていなかった。
    """

    def test_items_and_total_are_carried(self) -> None:
        from src.digest.high_threat_digest import HighThreatItem, high_threats_payload

        items = [
            HighThreatItem("jp", "日本の電力会社が標的", "incident", "https://x/jp", is_japan=True),
            HighThreatItem("a1", "APT41 が製造業に侵入", "apt", "https://x/1", is_japan=False),
        ]

        payload = build_brief_payload(
            syn_record=None, sections=[], high_threats=high_threats_payload(items, total=5)
        )

        ht = payload["high_threats"]
        assert ht["total"] == 5
        assert [i["article_id"] for i in ht["items"]] == ["jp", "a1"]
        assert ht["items"][0]["category_label"] == "インシデント"
        assert ht["items"][0]["is_japan"] is True
        json.dumps(payload)  # JSON-safe

    def test_absent_high_threats_is_null(self) -> None:
        payload = build_brief_payload(syn_record=None, sections=[])

        assert payload["high_threats"] is None
