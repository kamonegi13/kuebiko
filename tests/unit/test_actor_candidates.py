"""src.cti.actor_candidates のテスト (Actor Recall Layer Part C1)。

辞書未収録の新興アクター候補採取 — 既知は除外、ベンダ designation / LLM primary を拾う。
"""

from __future__ import annotations

from src.cti.actor_candidates import (
    ActorCandidate,
    harvest_candidates,
    normalize_actor_key,
)
from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(id="salt_typhoon", canonical="Salt Typhoon", aliases=("GhostEmperor",)),
            ActorAlias(id="anonymous", canonical="Anonymous", ambiguous=True),
        ),
    )


class TestNormalizeKey:
    def test_lowercase_and_collapse(self) -> None:
        assert normalize_actor_key("  Storm-2372  ") == "storm-2372"
        assert normalize_actor_key("Scattered   Spider") == "scattered spider"

    def test_strips_surrounding_punctuation(self) -> None:
        assert normalize_actor_key("(UNC5221)") == "unc5221"
        assert normalize_actor_key("'GhostShell',") == "ghostshell"


class TestHarvest:
    def test_vendor_designation_harvested(self) -> None:
        body = "Microsoft tracks the cluster as Storm-2372; Mandiant calls it UNC5221."
        cands = harvest_candidates(body=body, primary_actor_id="", registry=_registry())
        keys = {c.key for c in cands}
        assert "storm-2372" in keys
        assert "unc5221" in keys
        assert all(c.signal == "vendor_designation" for c in cands)

    def test_llm_primary_unknown_harvested(self) -> None:
        # 辞書未収録の LLM primary actor → 候補 (自称 crew を含む新顔)
        cands = harvest_candidates(
            body="A group calling itself Scattered Lapsus Hunters leaked the data.",
            primary_actor_id="scattered lapsus hunters",
            registry=_registry(),
        )
        assert any(c.key == "scattered lapsus hunters" and c.signal == "llm_primary" for c in cands)

    def test_known_actor_not_harvested(self) -> None:
        # 既知 (Salt Typhoon) は候補化しない — 確定帰属は辞書ゲートのまま
        cands = harvest_candidates(
            body="Salt Typhoon breached another telecom.",
            primary_actor_id="salt typhoon",
            registry=_registry(),
        )
        assert cands == []

    def test_known_alias_not_harvested(self) -> None:
        cands = harvest_candidates(
            body="The actor GhostEmperor returned.",
            primary_actor_id="ghostemperor",
            registry=_registry(),
        )
        assert cands == []

    def test_ambiguous_known_actor_not_treated_as_new(self) -> None:
        # "anonymous" は既知 (ambiguous)。LLM primary に来ても新興候補にしない (knows_name=gate抜き)
        cands = harvest_candidates(
            body="Anonymous claimed the DDoS.", primary_actor_id="anonymous", registry=_registry()
        )
        assert all(c.key != "anonymous" for c in cands)

    def test_non_actor_token_skipped(self) -> None:
        cands = harvest_candidates(
            body="Attribution remains unclear.", primary_actor_id="unknown", registry=_registry()
        )
        assert cands == []

    def test_dedup_by_key(self) -> None:
        body = "Storm-2372 again. Storm-2372 struck twice."
        cands = harvest_candidates(body=body, primary_actor_id="", registry=_registry())
        assert len([c for c in cands if c.key == "storm-2372"]) == 1

    def test_excerpt_captured(self) -> None:
        body = "In March, the group UNC1234 deployed a backdoor against the ministry."
        cands = harvest_candidates(body=body, primary_actor_id="", registry=_registry())
        unc = next(c for c in cands if c.key == "unc1234")
        assert "UNC1234" in unc.excerpt
        assert isinstance(unc, ActorCandidate)


class TestUatAptDesignations:
    """2026-08-01: taxonomy pattern_5 退役に伴う UAT/APT designation の引き継ぎ。"""

    def test_uat_designation_harvested(self) -> None:
        body = "Cisco Talos attributes the LapDogs ORB network to UAT-7811 with confidence."
        cands = harvest_candidates(body=body, primary_actor_id="", registry=_registry())
        assert any(c.key == "uat-7811" and c.signal == "vendor_designation" for c in cands)

    def test_unknown_apt_number_harvested(self) -> None:
        body = "Researchers documented APT77 targeting maritime logistics."
        cands = harvest_candidates(body=body, primary_actor_id="", registry=_registry())
        assert any(c.key == "apt77" and c.signal == "vendor_designation" for c in cands)

    def test_known_apt_not_harvested(self) -> None:
        registry = ActorAliasRegistry(
            actors=(ActorAlias(id="apt28", canonical="APT28", aliases=("Fancy Bear",)),),
        )
        cands = harvest_candidates(
            body="APT28 continues phishing campaigns.", primary_actor_id="", registry=registry
        )
        assert cands == []


class TestGeopoliticalNoiseFilter:
    """2026-08-01: llm_primary の地政学ノイズ (国家・政府・軍) を候補化しない。"""

    def _harvest_pid(self, pid: str) -> list[ActorCandidate]:
        return harvest_candidates(
            body=f"{pid} did something.", primary_actor_id=pid, registry=_registry()
        )

    def test_country_names_filtered(self) -> None:
        for pid in ("China", "Russia", "Iran", "North Korea", "United States", "PRC"):
            assert self._harvest_pid(pid) == [], pid

    def test_government_military_suffixes_filtered(self) -> None:
        for pid in (
            "Trump Administration",
            "Iranian Regime",
            "US Air Force",
            "China Coast Guard",
            "U.S. Military",
        ):
            assert self._harvest_pid(pid) == [], pid

    def test_militant_org_denylist_filtered(self) -> None:
        assert self._harvest_pid("Hezbollah") == []

    def test_real_hacktivist_with_army_in_middle_kept(self) -> None:
        # 末尾一致のみ — Cyber Army of Russia Reborn は実在 hacktivist、巻き込まない
        cands = self._harvest_pid("Cyber Army of Russia Reborn")
        assert any(c.key == "cyber army of russia reborn" for c in cands)

    def test_normal_group_name_kept(self) -> None:
        cands = self._harvest_pid("SilverFox")
        assert any(c.key == "silverfox" for c in cands)

    def test_vendor_designation_not_affected(self) -> None:
        cands = harvest_candidates(
            body="Attribution: Storm-2372 operated from China.",
            primary_actor_id="China",
            registry=_registry(),
        )
        keys = {c.key for c in cands}
        assert "storm-2372" in keys and "china" not in keys
