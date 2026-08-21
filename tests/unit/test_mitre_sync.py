"""mitre_sync (MITRE ATT&CK → Actor 辞書 逐次同期) のテスト。

核心: (1) 追加系差分のみ自動適用に振り分ける、(2) alias 衝突と新規 actor は
レビュー提案に隔離する (誤帰属防止)、(3) 翻訳プロンプトが固有名詞維持を強制する。
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from src.cti.actor_editor import append_new_actor, move_alias
from src.cti.mitre_sync import (
    TRANSLATE_SYSTEM_PROMPT,
    MitreGroup,
    apply_auto_update,
    compute_sync_plan,
    derive_nation,
    parse_mitre_bundle,
    run_mitre_actor_sync,
    translate_summary,
)
from src.tools.llm_client import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    MAX_STRUCTURED_ATTEMPTS,
    LLMClient,
    LLMConnectionError,
    LLMResponse,
)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _group(**overrides: Any) -> MitreGroup:
    base: dict[str, Any] = {
        "mitre_id": "G1017",
        "canonical": "Volt Typhoon",
        "aliases": ("Vanguard Panda", "BRONZE SILHOUETTE"),
        "summary": "Volt Typhoon is a PRC state-sponsored actor.",
        "associated_malware": ("KV Botnet",),
        "references": ("https://example.com/report",),
        "modified": "2026-01-01T00:00:00.000Z",
        "nation": "cn",
        "nation_reason": "検証済 override (G1017)",
        "is_criminal": False,
    }
    base.update(overrides)
    return MitreGroup(**base)


def _data(actors: list[dict[str, Any]]) -> dict[str, Any]:
    return {"families": {}, "actors": actors}


# ---------- parse_mitre_bundle ----------


def test_parse_bundle_extracts_group_with_malware_refs_and_ttps() -> None:
    objects: list[dict[str, Any]] = [
        {"type": "malware", "id": "malware--1", "name": "KV Botnet"},
        {
            "type": "attack-pattern",
            "id": "attack-pattern--1",
            "name": "Phishing",
            "external_references": [{"source_name": "mitre-attack", "external_id": "T1566"}],
        },
        {
            "type": "attack-pattern",
            "id": "attack-pattern--revoked",
            "name": "Old Technique",
            "revoked": True,
            "external_references": [{"source_name": "mitre-attack", "external_id": "T9999"}],
        },
        {
            "type": "intrusion-set",
            "id": "intrusion-set--1",
            "name": "Volt Typhoon",
            "aliases": ["Volt Typhoon", "Vanguard Panda"],
            "modified": "2026-01-01T00:00:00.000Z",
            "description": "PRC state-sponsored. (Citation: x) [link](https://x.example)",
            "external_references": [
                {"source_name": "mitre-attack", "external_id": "G1017"},
                {"source_name": "vendor", "url": "https://example.com/report"},
            ],
        },
        {
            "type": "relationship",
            "relationship_type": "uses",
            "source_ref": "intrusion-set--1",
            "target_ref": "malware--1",
        },
        {
            "type": "relationship",
            "relationship_type": "uses",
            "source_ref": "intrusion-set--1",
            "target_ref": "attack-pattern--1",
        },
        {
            "type": "relationship",
            "relationship_type": "uses",
            "source_ref": "intrusion-set--1",
            "target_ref": "attack-pattern--revoked",
        },
    ]
    groups = parse_mitre_bundle(objects)
    assert len(groups) == 1
    g = groups[0]
    assert g.mitre_id == "G1017"
    assert g.aliases == ("Vanguard Panda",)  # canonical 自身は除外
    assert g.associated_malware == ("KV Botnet",)
    assert g.references == ("https://example.com/report",)
    assert g.ttps == ("T1566 Phishing",)  # revoked technique は除外
    assert "(Citation:" not in g.summary
    assert "link" in g.summary  # markdown link はテキストだけ残す


def test_parse_bundle_skips_revoked_and_deprecated() -> None:
    objects = [
        {
            "type": "intrusion-set",
            "id": "intrusion-set--1",
            "name": "Old Group",
            "revoked": True,
            "external_references": [{"source_name": "mitre-attack", "external_id": "G0001"}],
        },
        {
            "type": "intrusion-set",
            "id": "intrusion-set--2",
            "name": "Deprecated Group",
            "x_mitre_deprecated": True,
            "external_references": [{"source_name": "mitre-attack", "external_id": "G0002"}],
        },
    ]
    assert parse_mitre_bundle(objects) == []


# ---------- derive_nation ----------


def test_derive_nation_prefers_verified_override() -> None:
    nation, reason = derive_nation("G1054", "no marker text")
    assert nation == "cn"
    assert "override" in reason


def test_derive_nation_uses_agency_marker_only() -> None:
    nation, _ = derive_nation("G9999", "linked to the reconnaissance general bureau")
    assert nation == "kp"
    # 国名のみの弱い一致 (bare "chinese") では帰属しない (Gorgon→ru 等の誤帰属防止)
    assert derive_nation("G9999", "a chinese speaking group") == (None, "")


# ---------- compute_sync_plan ----------


def test_plan_adds_new_alias_malware_and_ttps_to_matched_actor() -> None:
    data = _data(
        [
            {
                "id": "volt_typhoon",
                "canonical": "Volt Typhoon",
                "aliases": ["Vanguard Panda"],
                "mitre_group": "G1017",
                "summary": "既訳",
                "mitre_summary_sha1": _sha1("Volt Typhoon is a PRC state-sponsored actor."),
                "associated_malware": [],
                "references": [],
                "mitre_ttps": ["T1566 Phishing"],
            }
        ]
    )
    group = _group(ttps=("T1566 Phishing", "T1059.001 PowerShell"))
    plan = compute_sync_plan(data, [group])
    assert plan.matched == 1
    assert len(plan.auto_updates) == 1
    upd = plan.auto_updates[0]
    assert upd.add_aliases == ("BRONZE SILHOUETTE",)
    assert upd.add_malware == ("KV Botnet",)
    assert upd.add_references == ("https://example.com/report",)
    assert upd.add_ttps == ("T1059.001 PowerShell",)  # 既存 T1566 は追加しない
    assert upd.new_summary_en is None  # sha 一致 → MITRE 側変更なし
    assert plan.proposals == []


def test_plan_routes_conflicting_alias_to_proposal_not_auto() -> None:
    """MITRE の別名が既存の別 actor に帰属済 → 自動適用せず衝突提案に隔離。"""
    data = _data(
        [
            {
                "id": "volt_typhoon",
                "canonical": "Volt Typhoon",
                "aliases": [],
                "mitre_group": "G1017",
                "mitre_summary_sha1": _sha1("Volt Typhoon is a PRC state-sponsored actor."),
            },
            {
                "id": "other",
                "canonical": "Other Group",
                "aliases": ["BRONZE SILHOUETTE"],
            },
        ]
    )
    plan = compute_sync_plan(data, [_group()])
    upd = plan.auto_updates[0]
    assert "BRONZE SILHOUETTE" not in upd.add_aliases
    assert "Vanguard Panda" in upd.add_aliases
    conflicts = [p for p in plan.proposals if p.proposal_type == "mitre_alias_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0].payload["alias"] == "BRONZE SILHOUETTE"
    assert conflicts[0].payload["current_owner_id"] == "other"
    assert conflicts[0].dedup_key == "G1017:bronze silhouette"


def test_plan_detects_mitre_summary_change_via_sha1() -> None:
    data = _data(
        [
            {
                "id": "volt_typhoon",
                "canonical": "Volt Typhoon",
                "aliases": ["Vanguard Panda", "BRONZE SILHOUETTE"],
                "mitre_group": "G1017",
                "summary": "旧訳",
                "mitre_summary_sha1": _sha1("old summary text"),
                "associated_malware": ["KV Botnet"],
                "references": ["https://example.com/report"],
            }
        ]
    )
    plan = compute_sync_plan(data, [_group()])
    upd = plan.auto_updates[0]
    assert upd.new_summary_en == "Volt Typhoon is a PRC state-sponsored actor."
    assert upd.summary_sha1 == _sha1("Volt Typhoon is a PRC state-sponsored actor.")


def test_plan_unchanged_actor_produces_no_update() -> None:
    data = _data(
        [
            {
                "id": "volt_typhoon",
                "canonical": "Volt Typhoon",
                "aliases": ["Vanguard Panda", "BRONZE SILHOUETTE"],
                "mitre_group": "G1017",
                "summary": "既訳",
                "mitre_summary_sha1": _sha1("Volt Typhoon is a PRC state-sponsored actor."),
                "associated_malware": ["KV Botnet"],
                "references": ["https://example.com/report"],
            }
        ]
    )
    plan = compute_sync_plan(data, [_group()])
    assert plan.auto_updates == []
    assert plan.proposals == []


def test_plan_proposes_new_mission_actor_for_review() -> None:
    """未収載のミッション該当 group は自動追加せずレビュー提案 (nation 推定の検証)。"""
    plan = compute_sync_plan(_data([]), [_group()])
    assert plan.auto_updates == []
    assert len(plan.proposals) == 1
    p = plan.proposals[0]
    assert p.proposal_type == "mitre_new_actor"
    assert p.dedup_key == "G1017"
    assert p.payload["nation"] == "cn"
    assert p.payload["id"] == "volt_typhoon"


def test_plan_skips_out_of_mission_group() -> None:
    group = _group(mitre_id="G9999", canonical="Random Group", nation=None, is_criminal=False)
    plan = compute_sync_plan(_data([]), [group])
    assert plan.proposals == []
    assert plan.skipped_out_of_mission == 1


def test_plan_criminal_allowlist_group_gets_financial_motivation() -> None:
    group = _group(mitre_id="G0046", canonical="FIN7", nation=None, is_criminal=True)
    plan = compute_sync_plan(_data([]), [group])
    assert plan.proposals[0].payload["motivation"] == "financial"
    assert "nation" not in plan.proposals[0].payload


def test_plan_rejects_name_fallback_to_actor_bound_to_other_group() -> None:
    """別 MITRE group に紐付く actor へ canonical フォールバックマッチしない。

    silk_typhoon(G0125=HAFNIUM) が ZIRCONIUM を alias 混載する現実の病巣を再現。
    MITRE の ZIRCONIUM(G0128) group はこの actor の summary を上書きしてはならず、
    未収載の別グループとして new_actor 提案に回る。
    """
    actor = {
        "id": "silk_typhoon",
        "canonical": "Silk Typhoon",
        "aliases": ["Hafnium", "ZIRCONIUM"],
        "mitre_group": "G0125",
        "summary": "HAFNIUM 既訳",
        "mitre_summary_sha1": "existing-sha",
    }
    zirconium = _group(
        mitre_id="G0128",
        canonical="ZIRCONIUM",
        aliases=("APT31", "Violet Typhoon"),
        summary="ZIRCONIUM is a China-based group targeting the 2020 US election.",
        nation="cn",
    )
    plan = compute_sync_plan(_data([actor]), [zirconium])
    assert all(u.actor_id != "silk_typhoon" for u in plan.auto_updates)
    assert any(
        p.proposal_type == "mitre_new_actor" and p.mitre_group == "G0128" for p in plan.proposals
    )


def test_plan_rejects_alias_fallback_to_actor_bound_to_other_group() -> None:
    """alias フォールバックでも別 MITRE group への誤マッチを拒否する。

    ember_bear(G1003) が Saint Bear(G1031) の designation を alias 混載する病巣を再現。
    """
    actor = {
        "id": "ember_bear",
        "canonical": "Ember Bear",
        "aliases": ["Storm-0587", "TA471"],
        "mitre_group": "G1003",
        "summary": "Ember Bear 既訳",
        "mitre_summary_sha1": "existing-sha",
    }
    saint_bear = _group(
        mitre_id="G1031",
        canonical="Saint Bear",
        aliases=("Storm-0587", "TA471", "Lorec53"),
        summary="Saint Bear is a distinct cluster from Ember Bear.",
        nation="ru",
        nation_reason="test",
    )
    plan = compute_sync_plan(_data([actor]), [saint_bear])
    assert all(u.actor_id != "ember_bear" for u in plan.auto_updates)
    assert any(
        p.proposal_type == "mitre_new_actor" and p.mitre_group == "G1031" for p in plan.proposals
    )


def test_plan_still_matches_by_name_when_actor_has_no_mitre_group() -> None:
    """mitre_group 未設定の actor への name フォールバックは従来どおり許可 (回帰防止)。"""
    actor = {
        "id": "volt_typhoon",
        "canonical": "Volt Typhoon",
        "aliases": [],
    }
    plan = compute_sync_plan(_data([actor]), [_group()])
    assert plan.matched == 1
    assert any(
        u.actor_id == "volt_typhoon" and u.set_mitre_group == "G1017" for u in plan.auto_updates
    )


# ---------- apply_auto_update ----------


def test_apply_auto_update_is_immutable_and_sets_bookkeeping() -> None:
    original = _data(
        [
            {
                "id": "volt_typhoon",
                "canonical": "Volt Typhoon",
                "aliases": ["Vanguard Panda"],
                "summary": "旧訳",
            }
        ]
    )
    plan = compute_sync_plan(original, [_group()])
    upd = plan.auto_updates[0]
    new_data = apply_auto_update(original, upd, summary_ja="新しい和訳")

    assert original["actors"][0]["aliases"] == ["Vanguard Panda"]  # 元 data は不変
    actor = new_data["actors"][0]
    assert "BRONZE SILHOUETTE" in actor["aliases"]
    assert actor["summary"] == "新しい和訳"
    assert actor["mitre_summary_sha1"] == upd.summary_sha1
    assert actor["mitre_group"] == "G1017"


def test_apply_auto_update_keeps_summary_when_translation_failed() -> None:
    original = _data([{"id": "volt_typhoon", "canonical": "Volt Typhoon", "summary": "旧訳"}])
    plan = compute_sync_plan(original, [_group()])
    new_data = apply_auto_update(original, plan.auto_updates[0], summary_ja=None)
    actor = new_data["actors"][0]
    assert actor["summary"] == "旧訳"  # 据え置き → 次回 run で再試行
    assert "mitre_summary_sha1" not in actor


# ---------- translate_summary ----------


class _FakeLLM(LLMClient):
    @property
    def model(self) -> str:  # LLMClient 抽象契約 (監査 P6 で model を必須化)
        return "fake"

    def __init__(self, text: str = "", raise_error: bool = False) -> None:
        self.text = text
        self.raise_error = raise_error
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
    ) -> LLMResponse:
        self.calls.append({"prompt": prompt, "system": system, "think": think})
        if self.raise_error:
            raise LLMConnectionError("down")
        return LLMResponse(text=self.text, model="fake")

    async def generate_structured(
        self,
        prompt: str,
        schema: Any,
        system: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        think: bool | None = None,
        max_attempts: int = MAX_STRUCTURED_ATTEMPTS,
    ) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_translate_prompt_enforces_proper_noun_preservation() -> None:
    """固有名詞 (アクター/マルウェア/ツール名) 維持と think=False が prompt に乗ること。"""
    llm = _FakeLLM(text="Volt Typhoon は中国の国家支援型アクター。")
    out = await translate_summary(llm, "Volt Typhoon is a PRC actor.")
    assert out == "Volt Typhoon は中国の国家支援型アクター。"
    call = llm.calls[0]
    assert call["system"] == TRANSLATE_SYSTEM_PROMPT
    assert "固有名詞は翻訳せず" in str(call["system"])
    assert "MITRE ATT&CK ID" in str(call["system"])
    assert call["think"] is False


@pytest.mark.asyncio
async def test_translate_falls_back_to_original_on_llm_error() -> None:
    llm = _FakeLLM(raise_error=True)
    out = await translate_summary(llm, "original text")
    assert out == "original text"


# ---------- 提案適用 (actor_editor 拡張) ----------


def test_move_alias_transfers_between_actors() -> None:
    data = _data(
        [
            {"id": "a", "canonical": "Actor A", "aliases": ["Shared Name", "Keep"]},
            {"id": "b", "canonical": "Actor B", "aliases": []},
        ]
    )
    new_data = move_alias(data, "Shared Name", from_id="a", to_id="b")
    assert new_data["actors"][0]["aliases"] == ["Keep"]
    assert new_data["actors"][1]["aliases"] == ["Shared Name"]
    assert data["actors"][0]["aliases"] == ["Shared Name", "Keep"]  # 元は不変


def test_move_alias_refuses_canonical() -> None:
    data = _data(
        [
            {"id": "a", "canonical": "Shared Name", "aliases": []},
            {"id": "b", "canonical": "Actor B", "aliases": []},
        ]
    )
    with pytest.raises(ValueError, match="canonical"):
        move_alias(data, "Shared Name", from_id="a", to_id="b")


def test_append_new_actor_rejects_duplicate_id() -> None:
    data = _data([{"id": "volt_typhoon", "canonical": "Volt Typhoon"}])
    with pytest.raises(ValueError, match="重複"):
        append_new_actor(data, {"id": "volt_typhoon", "canonical": "Copy"})
    appended = append_new_actor(data, {"id": "new_one", "canonical": "New One"})
    assert [a["id"] for a in appended["actors"]] == ["volt_typhoon", "new_one"]


# ---------- runner (fetch / yaml / repo をモック) ----------


class _FakeRepo:
    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []
        self.existing_keys: set[tuple[str, str]] = set()

    def find_actor_update_proposal(self, *, proposal_type: str, dedup_key: str) -> Any:
        return object() if (proposal_type, dedup_key) in self.existing_keys else None

    def insert_actor_update_proposal(self, **kwargs: Any) -> int:
        self.inserted.append(kwargs)
        return len(self.inserted)


class _FakeRepoWithLabels(_FakeRepo):
    """較正格子 P1: 収穫② (alias 確定ラベル) の記録を捕捉する fake。"""

    def __init__(self) -> None:
        super().__init__()
        self.labels: list[dict[str, Any]] = []

    def record_tuning_label(self, **kwargs: Any) -> int | None:
        self.labels.append(kwargs)
        return len(self.labels)


@pytest.mark.asyncio
async def test_runner_applies_updates_and_persists_proposals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _data(
        [{"id": "volt_typhoon", "canonical": "Volt Typhoon", "aliases": [], "summary": ""}]
    )
    new_group = _group(
        mitre_id="G0008",
        canonical="Carbanak",
        aliases=("Anunak",),
        nation=None,
        is_criminal=True,
        summary="Carbanak is a financially motivated group.",
    )

    async def fake_fetch(url: str = "") -> list[MitreGroup]:
        return [_group(), new_group]

    monkeypatch.setattr("src.cti.mitre_sync.fetch_mitre_groups", fake_fetch)
    monkeypatch.setattr("src.cti.mitre_sync.load_actors_raw", lambda: existing)

    written: list[tuple[str, str]] = []
    repo = _FakeRepo()
    result = await run_mitre_actor_sync(
        llm=_FakeLLM(text="和訳済みテキスト"),
        repo=repo,
        run_id=42,
        dry_run=False,
        write_yaml=lambda content, msg: written.append((content, msg)),
    )

    assert result.auto_applied == 1
    assert result.summaries_translated == 1
    assert result.proposals_new == 1
    assert len(written) == 1
    assert "和訳済みテキスト" in written[0][0]
    assert "BRONZE SILHOUETTE" in written[0][0]
    assert repo.inserted[0]["proposal_type"] == "mitre_new_actor"
    assert "和訳済みテキスト" in repo.inserted[0]["payload"]  # 新規 actor の summary も和訳済


@pytest.mark.asyncio
async def test_runner_records_alias_labels_on_auto_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """較正格子 P1 収穫②: 自動適用された alias 追加が E1 ラベルとして記録される。"""
    existing = _data(
        [{"id": "volt_typhoon", "canonical": "Volt Typhoon", "aliases": [], "summary": ""}]
    )

    async def fake_fetch(url: str = "") -> list[MitreGroup]:
        return [_group()]

    monkeypatch.setattr("src.cti.mitre_sync.fetch_mitre_groups", fake_fetch)
    monkeypatch.setattr("src.cti.mitre_sync.load_actors_raw", lambda: existing)

    repo = _FakeRepoWithLabels()
    result = await run_mitre_actor_sync(
        llm=_FakeLLM(text="和訳済みテキスト"),
        repo=repo,
        run_id=None,
        dry_run=False,
        write_yaml=lambda content, msg: None,
    )

    assert result.auto_applied == 1
    assert repo.labels, "自動適用の alias がラベル化されていない"
    label = repo.labels[0]
    assert label["field"] == "actor_alias"
    assert label["label_value"] == "volt_typhoon"
    assert label["source"] == "E1"
    assert label["dedup_key"].startswith("mitre_alias:volt_typhoon:")


@pytest.mark.asyncio
async def test_runner_dry_run_records_no_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _data(
        [{"id": "volt_typhoon", "canonical": "Volt Typhoon", "aliases": [], "summary": ""}]
    )

    async def fake_fetch(url: str = "") -> list[MitreGroup]:
        return [_group()]

    monkeypatch.setattr("src.cti.mitre_sync.fetch_mitre_groups", fake_fetch)
    monkeypatch.setattr("src.cti.mitre_sync.load_actors_raw", lambda: existing)

    repo = _FakeRepoWithLabels()
    await run_mitre_actor_sync(
        llm=_FakeLLM(text="x"), repo=repo, run_id=None, dry_run=True, write_yaml=None
    )
    assert repo.labels == []


@pytest.mark.asyncio
async def test_runner_skips_duplicate_proposal_and_dry_run_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_group = _group(mitre_id="G0008", canonical="Carbanak", nation=None, is_criminal=True)

    async def fake_fetch(url: str = "") -> list[MitreGroup]:
        return [new_group]

    monkeypatch.setattr("src.cti.mitre_sync.fetch_mitre_groups", fake_fetch)
    monkeypatch.setattr("src.cti.mitre_sync.load_actors_raw", lambda: _data([]))

    # 既存 (rejected 含む) の同一提案 → 再挿入しない
    repo = _FakeRepo()
    repo.existing_keys.add(("mitre_new_actor", "G0008"))
    result = await run_mitre_actor_sync(
        llm=_FakeLLM(text="x"), repo=repo, run_id=None, dry_run=False, write_yaml=None
    )
    assert result.proposals_new == 0
    assert result.proposals_skipped_duplicate == 1
    assert repo.inserted == []

    # dry_run は repo があっても永続化しない (提案は「される予定の件数」のみ報告)
    written: list[tuple[str, str]] = []
    dry_repo = _FakeRepo()
    result = await run_mitre_actor_sync(
        llm=_FakeLLM(text="x"),
        repo=dry_repo,
        run_id=None,
        dry_run=True,
        write_yaml=lambda content, msg: written.append((content, msg)),
    )
    assert result.proposals_new == 1  # 件数報告のみ
    assert dry_repo.inserted == []  # 永続化はしない
    assert written == []


# ---------- 一般語 alias の取込 filter (2026-07-21 再発防止) ----------


class TestGenericAliasFilter:
    """MITRE の一般語 alias を辞書に取り込まない (再発ループの遮断)。

    経緯: 2026-07-17 に旧 Microsoft 元素名 alias (POTASSIUM 等) を誤帰属源として
    辞書から除去したが、MITRE 側は保持し続けるため週次同期が毎回書き戻していた
    (2026-07-21 に 11 件の復活を検出)。除去は辞書側だけでは完結しない。
    """

    def test_generic_aliases_are_not_auto_applied(self) -> None:
        group = _group(aliases=("BARIUM", "Winnti Group", "POTASSIUM"))
        data = _data([{"id": "apt41", "canonical": "APT41", "mitre_group": "G1017"}])

        plan = compute_sync_plan(data, [group])

        assert len(plan.auto_updates) == 1
        update = plan.auto_updates[0]
        assert update.add_aliases == ("Winnti Group",), "一般語以外は従来どおり取り込む"
        assert set(update.skipped_generic_aliases) == {"BARIUM", "POTASSIUM"}

        applied = apply_auto_update(data, update, summary_ja=None)
        assert applied["actors"][0]["aliases"] == ["Winnti Group"]

    def test_ambiguous_actor_may_receive_generic_alias(self) -> None:
        """ambiguous=true は文脈 cue ゲートがあるため一般語名を持ってよい (例: Tick)。"""
        group = _group(mitre_id="G0060", canonical="BRONZE BUTLER", aliases=("Tick",))
        data = _data(
            [
                {
                    "id": "tick",
                    "canonical": "BRONZE BUTLER",
                    "mitre_group": "G0060",
                    "ambiguous": True,
                }
            ]
        )

        plan = compute_sync_plan(data, [group])

        assert plan.auto_updates[0].add_aliases == ("Tick",)
        assert plan.auto_updates[0].skipped_generic_aliases == ()

    def test_new_actor_proposal_excludes_generic_aliases(self) -> None:
        """新規 actor には ambiguous ゲートが付かないため提案段階で除外する。"""
        group = _group(
            mitre_id="G0099",
            canonical="Example Typhoon",
            aliases=("ZINC", "Diamond Sleet"),
        )

        plan = compute_sync_plan(_data([]), [group])

        proposals = [p for p in plan.proposals if p.proposal_type == "mitre_new_actor"]
        assert len(proposals) == 1
        assert proposals[0].payload["aliases"] == ["Diamond Sleet"]
