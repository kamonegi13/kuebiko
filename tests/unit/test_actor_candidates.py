"""src.cti.actor_candidates のテスト (Actor Recall Layer Part C1)。

辞書未収録の新興アクター候補採取 — 既知は除外、ベンダ designation / LLM primary を拾う。
"""

from __future__ import annotations

from src.cti.actor_candidates import (
    ActorCandidate,
    harvest_candidates,
    is_geopolitical_noise,
    is_known_non_actor,
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


class TestNonActorGuards:
    """カテゴリ混同遮断 (2026-08-13): ツール/人物/企業/AI モデル名をアクター候補にしない。"""

    def test_same_article_tool_name_not_harvested(self) -> None:
        # ConsentFix v3 事案: LLM が主体名にツールキット名を返しても、同記事の
        # malware/tool 抽出結果との突合で遮断する
        cands = harvest_candidates(
            body="A new toolkit ConsentFix v3 was released on a criminal forum.",
            primary_actor_id="ConsentFix v3",
            registry=_registry(),
            known_non_actor_names=["ConsentFix v3", "SpecterPortal"],
        )
        assert all(c.key != "consentfix v3" for c in cands)

    def test_global_malware_vocab_not_harvested(self) -> None:
        # config/cti/malware_aliases.yaml の語彙 (PlayCrypt = Play ransomware) は候補化しない
        cands = harvest_candidates(
            body="PlayCrypt continues to spread.",
            primary_actor_id="PlayCrypt",
            registry=_registry(),
        )
        assert cands == []

    def test_version_suffix_of_known_actor_absorbed(self) -> None:
        # 既知アクターの版数亜種 ("Salt Typhoon 2") は新顔として拾わない
        cands = harvest_candidates(
            body="Researchers describe Salt Typhoon 2 activity.",
            primary_actor_id="Salt Typhoon 2",
            registry=_registry(),
        )
        assert cands == []

    def test_known_non_actor_names_blocked(self) -> None:
        # 頻出の非アクター (AI 企業・人物・形容詞的総称) は決定論遮断
        for name in ("OpenAI", "Vladimir Putin", "Russian Hackers", "Pentagon"):
            cands = harvest_candidates(
                body=f"{name} was mentioned in the report.",
                primary_actor_id=name,
                registry=_registry(),
            )
            assert cands == [], name

    def test_legit_new_actor_still_harvested(self) -> None:
        # 遮断の副作用で正当な新顔まで殺していないこと
        cands = harvest_candidates(
            body="A group calling itself Crimson Mantis claimed the intrusion.",
            primary_actor_id="Crimson Mantis",
            registry=_registry(),
            known_non_actor_names=["SpecterPortal"],
        )
        assert any(c.key == "crimson mantis" and c.signal == "llm_primary" for c in cands)

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


class TestGeopoliticalNoiseBeyondEnglish:
    """承認キューに滞留していた非アクターを決定論で弾く (2026-08-19)。

    ⚠ judgment のプロンプトは「国家/政党/軍組織そのもの」「個人名 (政治家)」
    「防御側・報告機関」を **既に禁じている** のに、承認キューには 中国共産党 /
    中国人民解放軍 / Tucker Carlson / US Space Command / Team82 が滞留していた。
    ⭐ **指示だけでは止まらない** (同日 summarizer でも実証) ため収穫側で遮断する。
    """

    def test_japanese_government_and_military_orgs(self) -> None:
        assert is_geopolitical_noise("中国共産党")
        assert is_geopolitical_noise("中国人民解放軍")
        # ⚠ 実在のアクター名を巻き込まない
        assert not is_geopolitical_noise("armored likho")
        assert not is_geopolitical_noise("head mare")

    def test_multi_country_enumeration(self) -> None:
        """列挙形は素通りしていた — 個々は国名判定で弾けるのに。"""
        assert is_geopolitical_noise("china, russia, iran, and north korea")
        assert is_geopolitical_noise("china, russia, iran & north korea")
        # 1 か国だけの言及は列挙ではない (国名単体は別の分岐が弾く)
        assert not is_geopolitical_noise("direwolf")

    def test_defenders_and_generic_terms(self) -> None:
        assert is_known_non_actor("team82")
        assert is_known_non_actor("us space command")
        assert is_known_non_actor("ai agents")
        assert is_known_non_actor("north korean it workers")
        assert is_known_non_actor("tucker carlson")
        assert is_known_non_actor("maria zakharova")

    def test_ai_platform_names_including_japanese_compounds(self) -> None:
        """正規 AI 製品名は日本語複合語形でも弾く (2026-08-19)。

        _KNOWN_NON_ACTOR_NAMES の完全一致は「claudeエージェント」を素通りさせた (実例)。
        ai_platform_drop の collapse key (非英数字を落とす) を語彙側でも共有して塞ぐ。
        """
        assert is_known_non_actor("claudeエージェント")
        assert is_known_non_actor("claude code")
        assert is_known_non_actor("deepseek-v4-pro")
        # ⚠ 悪性 LLM サービスは攻撃側の実体なので候補に残す
        assert not is_known_non_actor("wormgpt")

    def test_party_and_vulnerability_nicknames(self) -> None:
        """承認キューの人手判断で却下した 4 件 (2026-08-19)。

        DPP は政党 — ⚠ プロパガンダ記事が「攪乱役」と framing するため LLM が
        攻撃主体として抽出する。Nightmare/Chaotic Eclipse は Windows/Defender の
        脆弱性報道が根拠で、記事の固有名は LegacyHive / ShieldBreak という
        **脆弱性のニックネーム**。候補名自体はタイトルに一度も出ない。
        """
        assert is_known_non_actor("dpp")
        assert is_known_non_actor("nightmare eclipse")
        assert is_known_non_actor("nightmare-eclipse")
        assert is_known_non_actor("chaotic eclipse")

    def test_vendor_designated_actors_are_not_blocked_by_title_absence(self) -> None:
        """⚠ 「候補名がタイトルに出ない」は機械判定に使えない。

        UNC5537 / CL-STA-0049 / Famous Chollima は実在のアクターだが、ベンダー命名は
        本文で言及されタイトルに出ないため、Nightmare Eclipse と同じ形になる。
        構造では分けられないので列挙で対処した — この差を固定する。
        """
        for key in ("unc5537", "cl-sta-0049", "famous chollima"):
            assert not is_known_non_actor(key), key

    def test_real_actor_candidates_survive(self) -> None:
        """⭐ 収穫を殺さないことの確認 — 実データの正当な候補は全て通す。"""
        for key in (
            "direwolf",
            "xpl0itrs",
            "jewelbug",
            "head mare",
            "armored likho",
            "unc5537",
            "cl-sta-0049",
            "famous chollima",
        ):
            assert not is_known_non_actor(key), key
            assert not is_geopolitical_noise(key), key
