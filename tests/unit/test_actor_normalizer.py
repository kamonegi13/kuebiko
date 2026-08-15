"""src.cti.actor_normalizer のテスト (Phase 4)。"""

from __future__ import annotations

from pathlib import Path

from src.cti.actor_normalizer import (
    CYBERCRIME_CONTEXT_CUES,
    DEFAULT_AMBIGUOUS_CUES,
    ActorAlias,
    ActorAliasRegistry,
    load_actor_aliases,
    normalize_actor,
    resolve_ambiguous_cues,
)
from src.cti.generic_alias_words import GENERIC_ALIAS_WORDS, is_generic_alias


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="volt_typhoon",
                canonical="Volt Typhoon",
                aliases=("Vanguard Panda", "BRONZE SILHOUETTE"),
                mitre_group="G1017",
                nation="cn",
            ),
            ActorAlias(
                id="apt29",
                canonical="APT29",
                aliases=("Cozy Bear", "Midnight Blizzard", "Nobelium"),
                mitre_group="G0016",
                nation="ru",
            ),
            ActorAlias(
                id="lazarus",
                canonical="Lazarus Group",
                aliases=("HIDDEN COBRA", "Zinc"),
                mitre_group="G0032",
                nation="kp",
            ),
        ),
    )


# ---------- find ----------


def test_find_by_canonical_name() -> None:
    r = _registry()
    actor = r.find("中国系 Volt Typhoon が事前配置を継続")
    assert actor is not None
    assert actor.id == "volt_typhoon"


def test_find_by_alias() -> None:
    r = _registry()
    actor = r.find("Cozy Bear を APT29 として観測")
    assert actor is not None
    assert actor.id == "apt29"


def test_find_case_insensitive() -> None:
    r = _registry()
    actor = r.find("VOLT TYPHOON")
    assert actor is not None and actor.id == "volt_typhoon"
    actor = r.find("hidden cobra")
    assert actor is not None and actor.id == "lazarus"


def test_find_prefers_longer_match() -> None:
    """``BRONZE SILHOUETTE`` のようなより長い名前を優先する。"""
    r = ActorAliasRegistry(
        actors=(
            ActorAlias(id="bronze", canonical="BRONZE", aliases=()),
            ActorAlias(
                id="volt_typhoon",
                canonical="Volt Typhoon",
                aliases=("BRONZE SILHOUETTE",),
            ),
        ),
    )
    actor = r.find("Confirmed BRONZE SILHOUETTE activity")
    assert actor is not None and actor.id == "volt_typhoon"


def test_find_no_match() -> None:
    r = _registry()
    assert r.find("関連性のないテキスト") is None


def test_find_empty() -> None:
    r = _registry()
    assert r.find("") is None
    assert r.find("   ") is None


# ---------- find_all ----------


def test_find_all_multiple_actors() -> None:
    r = _registry()
    found = r.find_all("Lazarus Group と APT29 が同時に観測された")
    assert {a.id for a in found} == {"lazarus", "apt29"}


def test_find_all_dedupes() -> None:
    """canonical と alias 両方に同一アクター名がある場合も 1 度だけ返す。"""
    r = _registry()
    found = r.find_all("Lazarus Group も Lazarus も HIDDEN COBRA も同じ")
    assert len(found) == 1
    assert found[0].id == "lazarus"


# ---------- Part B: 曖昧アクター文脈判別 ----------


def _ambiguous_registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(id="anonymous", canonical="Anonymous", ambiguous=True, family="hacktivist"),
            ActorAlias(id="lazarus", canonical="Lazarus Group", aliases=("HIDDEN COBRA",)),
        ),
    )


def test_ambiguous_actor_not_matched_without_cue() -> None:
    # "anonymous source" = 匿名情報源。ハクティビズム文脈が無いので Anonymous 集団と誤検出しない
    r = _ambiguous_registry()
    assert r.find("The breach was disclosed by an anonymous source close to the company") is None
    assert r.find_all("匿名の情報筋によると、Anonymous な投稿が増えている") == []


def test_ambiguous_actor_matched_with_cue() -> None:
    # ハクティビズム文脈 (DDoS / 犯行声明 / hacktivist) が共起すれば本物の集団として検出
    r = _ambiguous_registry()
    assert r.find("Anonymous claimed responsibility for the DDoS attack") is not None
    found = r.find_all("ハクティビスト集団 Anonymous が犯行声明を出した")
    assert {a.id for a in found} == {"anonymous"}


def test_ambiguous_actor_custom_cues_override_default() -> None:
    r = ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="anonymous", canonical="Anonymous", ambiguous=True, context_cues=("#opjapan",)
            ),
        ),
    )
    # 既定 cue (ddos) ではマッチせず、独自 cue のみで確定
    assert r.find("Anonymous launched a DDoS") is None
    assert r.find("Anonymous announced #OpJapan") is not None


def test_non_ambiguous_actor_unaffected_by_cue_gate() -> None:
    # 非曖昧アクターは従来通り cue 無しでマッチ (回帰防止)
    r = _ambiguous_registry()
    assert r.find("Lazarus Group の新たな金融標的") is not None


# ---------- 一般語衝突アクターの cyber cue ゲート (entity 棚卸し 2026-07-29) ----------


def test_resolve_ambiguous_cues_selects_by_family() -> None:
    # 明示 cues → そのまま
    custom = ActorAlias(id="x", canonical="X", ambiguous=True, context_cues=("#opjp",))
    assert resolve_ambiguous_cues(custom) == ("#opjp",)
    # hacktivist で cues 未指定 → ハクティビズム default (既存挙動を維持)
    hacktivist = ActorAlias(id="y", canonical="Y", ambiguous=True, family="hacktivist")
    assert resolve_ambiguous_cues(hacktivist) == DEFAULT_AMBIGUOUS_CUES
    # 非 hacktivist で cues 未指定 → cybercrime default (新規)
    ransom = ActorAlias(id="z", canonical="Z", ambiguous=True, family="")
    assert resolve_ambiguous_cues(ransom) == CYBERCRIME_CONTEXT_CUES


def test_cybercrime_generic_actor_requires_cyber_cue() -> None:
    # "Play" は一般語。ゲーム/遊びの文脈では Play ランサムグループと誤検出しない
    r = ActorAliasRegistry(
        actors=(ActorAlias(id="play", canonical="Play", kind="group", ambiguous=True),),
    )
    assert r.find("The children play in the park every afternoon") is None
    assert r.find_all("A great play was performed at the theater") == []
    # ランサム文脈 cue 共起で確定
    assert r.find("Play ransomware leaked victim data on their dark web leak site") is not None


def test_cybercrime_espionage_actor_requires_cyber_cue() -> None:
    # "Axiom"/"GALLIUM" は会社名/元素と衝突する中国系サイバー諜報グループ
    r = ActorAliasRegistry(
        actors=(
            ActorAlias(id="axiom", canonical="Axiom", kind="group", ambiguous=True),
            ActorAlias(id="gallium", canonical="GALLIUM", kind="group", ambiguous=True),
        ),
    )
    # 非 cyber 文脈 (宇宙企業 / 半導体材料) では誤検出しない
    assert r.find("Axiom Space launched a new module to the ISS") is None
    assert r.find_all("GALLIUM nitride semiconductors improve power efficiency") == []
    # 諜報文脈 cue 共起で確定
    assert r.find("Axiom is a Chinese cyberespionage group deploying PlugX backdoor") is not None


def test_cybercrime_generic_group_words_are_gated_in_real_yaml() -> None:
    """一般語衝突グループは denylist SSoT + 実 yaml で ambiguous 必須。

    2026-07-31: 静的 curated 集合を SSoT (GENERIC_ALIAS_WORDS) からの動的導出に強化。
    SSoT に語を足すだけで実 yaml の ambiguous 必須が自動で強制される (07-26 バッチ承認で
    一般語 11 体が ambiguous なしで辞書入りした事故の再発防止)。"""

    curated = set(GENERIC_ALIAS_WORDS)
    # denylist SSoT に載っていること (guard test / mitre filter / editor 警告を効かせる)
    assert curated <= GENERIC_ALIAS_WORDS, "curated 一般語が generic_alias_words の SSoT に無い"
    reg = load_actor_aliases(Path("config/cti/actor_aliases.yaml"))
    by_key = {a.id: a for a in reg.actors} | {a.canonical.lower(): a for a in reg.actors}
    for word in curated:
        actor = by_key.get(word)
        if actor is None:
            continue  # SSoT は辞書未収録の語 (元素等) も先回りで持つ — 存在は要求しない
        assert actor.ambiguous, f"{word} ({actor.id}) は ambiguous=true (cue ゲート) 必須"


def test_seeded_hacktivists_present_in_real_yaml() -> None:
    # Part A: 実 yaml にハクティビストが seed され、辞書照合で拾えること
    reg = load_actor_aliases(Path("config/cti/actor_aliases.yaml"))
    ids = {a.id for a in reg.actors}
    assert {"noname05716", "killnet", "anonymous_sudan", "it_army_ukraine", "anonymous"} <= ids
    # 数字付き/複合名は文脈 cue 無しでも安全に検出
    assert reg.find("NoName057(16) が日本の港湾に DDoS") is not None
    assert reg.find("Anonymous Sudan claimed the outage") is not None
    # bare "Anonymous" (ambiguous) は匿名情報源では検出しない
    assert reg.find("According to an anonymous official, talks continue") is None


# ---------- display_with_aliases ----------


def test_display_with_aliases() -> None:
    r = _registry()
    actor = r.find("Volt Typhoon")
    assert actor is not None
    s = actor.display_with_aliases()
    assert "Volt Typhoon" in s
    assert "Vanguard Panda" in s
    assert "G1017" in s


def test_display_truncates_aliases() -> None:
    """エイリアスは表示で 3 件まで (4 件以上ある場合)。"""
    actor = ActorAlias(
        canonical="Many",
        aliases=("a1", "a2", "a3", "a4", "a5"),
        id="many",
    )
    s = actor.display_with_aliases()
    assert "a1" in s
    assert "a3" in s
    # 4 件目以降は表示されない (3 件まで)
    assert "a4" not in s


# ---------- normalize_actor ----------


def test_normalize_actor_known() -> None:
    r = _registry()
    out = normalize_actor("Volt Typhoon", r)
    assert "Volt Typhoon" in out
    assert "G1017" in out


def test_normalize_actor_unknown_returns_raw() -> None:
    r = _registry()
    assert normalize_actor("UnknownAPT99", r) == "UnknownAPT99"


def test_normalize_actor_handles_none() -> None:
    r = _registry()
    assert normalize_actor(None, r) == ""
    assert normalize_actor("", r) == ""
    assert normalize_actor("N/A", r) == ""
    assert normalize_actor("none", r) == ""
    assert normalize_actor("なし", r) == ""


def test_normalize_actor_strips() -> None:
    r = _registry()
    out = normalize_actor("  Volt Typhoon  ", r)
    assert "Volt Typhoon" in out


# ---------- load_actor_aliases ----------


def test_load_yaml_valid(tmp_path: Path) -> None:
    yaml_text = """
actors:
  - id: test_actor
    canonical: Test Actor
    aliases: [Alias One]
    mitre_group: G9999
    nation: cn
"""
    p = tmp_path / "x.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    r = load_actor_aliases(p)
    assert len(r.actors) == 1
    assert r.actors[0].canonical == "Test Actor"


def test_load_missing_file(tmp_path: Path) -> None:
    r = load_actor_aliases(tmp_path / "nonexistent.yaml")
    assert r.actors == ()


def test_load_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("not: valid: yaml::", encoding="utf-8")
    r = load_actor_aliases(p)
    assert r.actors == ()


def test_load_skips_invalid_entries(tmp_path: Path) -> None:
    """schema 不正なエントリはスキップして他は読み込む。"""
    yaml_text = """
actors:
  - id: ok
    canonical: OK Actor
  - canonical: missing_id_field  # id 欠落 → schema OK (id がないだけ)
"""
    p = tmp_path / "y.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    r = load_actor_aliases(p)
    # id が無いと validation でエラーになる (id は必須フィールド)
    assert all(a.id for a in r.actors)


def test_real_actor_aliases_yaml_loads() -> None:
    """実プロジェクトの ``config/cti/actor_aliases.yaml`` が壊れていないこと。"""
    r = load_actor_aliases()
    if r.actors:
        # 最低限、Volt Typhoon と APT29 と Lazarus は登録されているはず
        ids = {a.id for a in r.actors}
        for must_have in ("volt_typhoon", "apt29", "lazarus"):
            assert must_have in ids, f"missing actor: {must_have}"


def test_by_id() -> None:
    r = _registry()
    assert r.by_id("volt_typhoon") is not None
    assert r.by_id("nonexistent") is None


# ---------- load cache ((mtime_ns, size) キー) ----------


def test_load_cache_returns_same_registry_when_unchanged(tmp_path: Path) -> None:
    """同一ファイル (mtime/size 不変) の再ロードは parse せず cache を返す。"""
    p = tmp_path / "cache.yaml"
    p.write_text("actors:\n- id: x\n  canonical: X\n", encoding="utf-8")
    r1 = load_actor_aliases(p)
    r2 = load_actor_aliases(p)
    assert r1 is r2


def test_load_cache_invalidated_on_rewrite(tmp_path: Path) -> None:
    """書き換え (UI 編集 / mitre-sync の atomic write 相当) で cache が自動失効する。"""
    p = tmp_path / "cache.yaml"
    p.write_text("actors:\n- id: x\n  canonical: X\n", encoding="utf-8")
    r1 = load_actor_aliases(p)
    assert len(r1.actors) == 1
    p.write_text(
        "actors:\n- id: x\n  canonical: X\n- id: y\n  canonical: Y\n",
        encoding="utf-8",
    )
    r2 = load_actor_aliases(p)
    assert len(r2.actors) == 2


class TestGenericWordAliasGuard:
    """一般語と衝突する単独名の再発防止 (2026-07-18)。

    実害: APT10 の旧 Microsoft 名 "POTASSIUM" が化学元素カリウム (肥料記事) に誤帰属し、
    PIR 分類まで汚染した。旧 Microsoft 元素名 (Barium/Zinc/Mercury/CHROMIUM 等) は
    単独使用が稀で併記が通例のため 9 件を除去した。
    規則: **一般語 (元素名等) を単独名に持つ actor は ambiguous=true (文脈 cue ゲート)
    でなければならない** — Tick はこの形で許容されている。

    危険名の集合は ``src/cti/generic_alias_words.py`` が SSoT (2026-07-21)。同じ集合を
    MITRE 週次同期の取込 filter も参照する — テストが独自 list を持つと、辞書からの除去は
    通るのに同期が書き戻す状態 (実際に 2026-07-21 に発生) を検知できない。
    """

    def test_generic_word_names_require_ambiguous_gate(self) -> None:
        reg = load_actor_aliases()
        assert reg.actors, "辞書 seed がロードできない"
        violations: list[str] = []
        for actor in reg.actors:
            for name in actor.all_names:
                if is_generic_alias(name) and not actor.ambiguous:
                    violations.append(f"{actor.id}: '{name}'")
        assert not violations, (
            "一般語の単独名は ambiguous=true (文脈 cue) が必須、"
            f"または alias から除去すること: {violations}"
        )


# ---------- identity ライフサイクル (アクター辞書 Phase1) ----------


def _registry_with_merge() -> ActorAliasRegistry:
    """merged 墓標を含む合成 registry (B → A の redirect)。"""
    return ActorAliasRegistry(
        actors=(
            ActorAlias(
                id="actor_a",
                canonical="Alpha Group",
                aliases=("Old Beta", "Beta Crew"),  # merge で B から物理移動済み
            ),
            ActorAlias(
                id="actor_b",
                canonical="Old Beta",  # 歴史表示用に canonical は残す (継承先 alias と同名で正)
                aliases=(),  # 不変条件: merged 墓標の aliases は 0 件
                status="merged",
                merged_into="actor_a",
                merged_at="2026-07-26",
                moved_aliases=("Old Beta", "Beta Crew"),
            ),
        ),
    )


class TestIdentityLifecycle:
    """resolve_actor_id / merged 墓標の seam (redirect 0 件なら恒等関数)。"""

    def test_resolve_active_id_is_identity(self) -> None:
        r = _registry_with_merge()
        assert r.resolve_actor_id("actor_a") == "actor_a"

    def test_resolve_unknown_id_is_identity(self) -> None:
        r = _registry_with_merge()
        assert r.resolve_actor_id("no_such_actor") == "no_such_actor"

    def test_resolve_follows_redirect(self) -> None:
        r = _registry_with_merge()
        assert r.resolve_actor_id("actor_b") == "actor_a"

    def test_resolve_follows_chain(self) -> None:
        r = ActorAliasRegistry(
            actors=(
                ActorAlias(id="c", canonical="C"),
                ActorAlias(id="b", canonical="B", status="merged", merged_into="c"),
                ActorAlias(id="a", canonical="A", status="merged", merged_into="b"),
            ),
        )
        assert r.resolve_actor_id("a") == "c"

    def test_resolve_cycle_guard_returns_input(self) -> None:
        r = ActorAliasRegistry(
            actors=(
                ActorAlias(id="a", canonical="A", status="merged", merged_into="b"),
                ActorAlias(id="b", canonical="B", status="merged", merged_into="a"),
            ),
        )
        # 循環 (設定不正) でも無限ループせず入力を返す
        assert r.resolve_actor_id("a") == "a"

    def test_merged_stub_excluded_from_find(self) -> None:
        r = _registry_with_merge()
        actor = r.find("Old Beta の新キャンペーンを観測")
        assert actor is not None
        assert actor.id == "actor_a"  # 墓標ではなく継承先にマッチ

    def test_merged_stub_excluded_from_find_all(self) -> None:
        r = _registry_with_merge()
        ids = [a.id for a in r.find_all("Beta Crew と Old Beta の関与")]
        assert ids == ["actor_a"]

    def test_by_id_still_returns_tombstone(self) -> None:
        r = _registry_with_merge()
        stub = r.by_id("actor_b")
        assert stub is not None
        assert stub.is_merged
        assert stub.merged_into == "actor_a"

    def test_merged_sources_lists_old_ids(self) -> None:
        r = _registry_with_merge()
        assert r.merged_sources("actor_a") == ("actor_b",)
        assert r.merged_sources("actor_b") == ()


class TestIdentityGuardRealYaml:
    """実 yaml の identity 不変条件 (merge 導入後の恒久 guard)。

    - merged 墓標は aliases 0 件 (照合は継承先へ物理移動済みの alias が担う)
    - merged_into の参照先が実在し、チェーンが active な entry に到達する (循環なし)
    """

    def test_merged_stubs_hold_invariants(self) -> None:
        reg = load_actor_aliases()
        assert reg.actors, "辞書 seed がロードできない"
        violations: list[str] = []
        for actor in reg.actors:
            if not actor.is_merged:
                continue
            if actor.aliases:
                violations.append(f"{actor.id}: merged 墓標に aliases が残存")
            if not actor.merged_into:
                violations.append(f"{actor.id}: merged_into 未指定")
                continue
            resolved = reg.resolve_actor_id(actor.id)
            target = reg.by_id(resolved)
            if resolved == actor.id or target is None or target.is_merged:
                violations.append(f"{actor.id}: redirect が active entry に到達しない")
        assert not violations, f"identity 不変条件違反: {violations}"

    def test_normalized_id_canonical_keys_unique_among_active(self) -> None:
        """active actor の id/canonical 正規化キーは辞書全体で一意 (2026-08-01 事故の恒久 guard)。

        `the_gentlemen` と `thegentlemen` のような綴り違い二重登録を CI で遮断する。
        alias の共有 (APT38 = lazarus/bluenoroff 両属、Carbanak = fin7 alias) は
        意図的に存在するため、検査は id/canonical 由来のキーに限定する。
        """
        from src.cti.actor_normalizer import _norm_slug

        reg = load_actor_aliases()
        owners: dict[str, str] = {}
        violations: list[str] = []
        for actor in reg.actors:
            if actor.is_merged:
                continue
            for key in {_norm_slug(actor.id), _norm_slug(actor.canonical)} - {""}:
                prev = owners.get(key)
                if prev is not None and prev != actor.id:
                    violations.append(f"キー『{key}』: {prev} と {actor.id} が衝突")
                owners[key] = actor.id
        assert not violations, (
            f"id/canonical 正規化キー衝突 (同一実体の綴り違い二重登録): {violations} — "
            "merge (redirect 墓標) で統合してください"
        )


class TestMatchedNamesFor:
    """F5 alias 使用統計: 発火した全名前の走査 (find_all は最初のヒットで打ち切るため別実装)。"""

    def test_returns_all_hit_names(self) -> None:
        r = _registry()
        apt29 = r.by_id("apt29")
        assert apt29 is not None
        names = r.matched_names_for(apt29, "Cozy Bear (APT29, aka Midnight Blizzard) の活動")
        assert set(names) == {"APT29", "Cozy Bear", "Midnight Blizzard"}

    def test_ambiguous_gate_applies(self) -> None:
        r = ActorAliasRegistry(
            actors=(
                ActorAlias(id="anon", canonical="Anonymous", ambiguous=True, family="hacktivist"),
            ),
        )
        anon = r.by_id("anon")
        assert anon is not None
        assert r.matched_names_for(anon, "Anonymous sources said") == ()
        assert r.matched_names_for(anon, "Anonymous claimed responsibility for DDoS") == (
            "Anonymous",
        )

    def test_empty_text(self) -> None:
        r = _registry()
        actor = r.actors[0]
        assert r.matched_names_for(actor, "") == ()


class TestResolveSourceSlug:
    """R2 source slug 名前空間写像 (2026-07-26): 構造化ソースの group slug 解決。"""

    def _reg(self) -> ActorAliasRegistry:
        return ActorAliasRegistry(
            actors=(
                ActorAlias(id="the_gentlemen", canonical="The Gentlemen"),
                ActorAlias(id="inc_ransom", canonical="INC Ransom", aliases=("INC",)),
                ActorAlias(
                    id="akira_ransom",
                    canonical="Akira ransomware",
                    source_slugs=("akira",),
                ),
                ActorAlias(id="volt_typhoon", canonical="Volt Typhoon"),
            ),
        )

    def test_normalized_slug_matches_id(self) -> None:
        # "thegentlemen" (feed slug) → the_gentlemen (記号除去 casefold で一致)
        hit = self._reg().resolve_source_slug("thegentlemen")
        assert hit is not None and hit.id == "the_gentlemen"

    def test_normalized_slug_matches_alias(self) -> None:
        hit = self._reg().resolve_source_slug("incransom")
        assert hit is not None and hit.id == "inc_ransom"

    def test_source_slug_field_resolves_generic_word(self) -> None:
        # "akira" は prose alias に持てない一般語 — source_slugs で解決する
        hit = self._reg().resolve_source_slug("akira")
        assert hit is not None and hit.id == "akira_ransom"

    def test_unknown_slug_returns_none(self) -> None:
        assert self._reg().resolve_source_slug("deadlock") is None
        assert self._reg().resolve_source_slug("") is None

    def test_source_slug_not_used_in_prose_matching(self) -> None:
        # source_slugs は find/find_all を汚さない (prose 一般語誤爆の防止)
        assert self._reg().find("the akira project released a new tool") is None


class TestAssertedSubjectLayer:
    """R2 第 0 層: source 構造的断言が最強証拠として採用される。"""

    def test_asserted_actor_becomes_subject(self) -> None:
        from src.cti.subject_actor import SOURCE_FEED, determine_subject_actors

        reg = ActorAliasRegistry(actors=(ActorAlias(id="qilin", canonical="Qilin"),))
        s = determine_subject_actors(
            titles=("Some victim disclosed",),  # title に名前なし
            detected_actor_ids=(),
            llm_primary_actor_id="",
            llm_confidence="",
            category="breach",
            registry=reg,
            asserted_actor_ids=("qilin",),
        )
        assert s.ids == ("qilin",)
        assert s.source == SOURCE_FEED

    def test_unresolvable_assertion_ignored(self) -> None:
        from src.cti.subject_actor import SOURCE_NONE, determine_subject_actors

        reg = ActorAliasRegistry(actors=(ActorAlias(id="qilin", canonical="Qilin"),))
        s = determine_subject_actors(
            titles=("x",),
            detected_actor_ids=(),
            llm_primary_actor_id="",
            llm_confidence="",
            category="breach",
            registry=reg,
            asserted_actor_ids=("deadlock",),  # 辞書に無い断言は採用しない
        )
        assert s.ids == ()
        assert s.source == SOURCE_NONE
