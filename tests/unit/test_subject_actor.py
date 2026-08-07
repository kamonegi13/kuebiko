"""主題アクター判定 (subject_actor) のテスト。

docs/subject_actor_attribution_design.md §5 の受け入れ基準を固定する。核心:
- 第 1 層 = タイトル決定論 (org 抑止 + R-A カテゴリゲート込み)
- 第 2 層 = LLM 補完は語彙固定の二重ゲート (辞書解決 ∧ 言及集合所属)
- 不明は 'none' として正直に評価済み扱い (無理に埋めない)
"""

from __future__ import annotations

import pytest

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.subject_actor import (
    SOURCE_LLM,
    SOURCE_NONE,
    SOURCE_TITLE,
    SubjectActors,
    determine_subject_actors,
    resolve_actor_by_name,
)


def _registry() -> ActorAliasRegistry:
    return ActorAliasRegistry(
        actors=(
            ActorAlias(id="salt_typhoon", canonical="Salt Typhoon", nation="cn"),
            ActorAlias(id="turla", canonical="Turla", nation="ru"),
            ActorAlias(id="lazarus", canonical="Lazarus Group", aliases=("Lazarus",), nation="kp"),
            ActorAlias(
                id="russia_fsb",
                canonical="Russia FSB",
                aliases=("FSB",),
                kind="organization",
                nation="ru",
            ),
            ActorAlias(
                id="russia_gru",
                canonical="Russia GRU",
                aliases=("GRU",),
                kind="organization",
                nation="ru",
            ),
            ActorAlias(id="apt28", canonical="APT28", nation="ru", sponsor_org="russia_gru"),
            ActorAlias(id="anonymous", canonical="Anonymous", ambiguous=True),
        )
    )


def _determine(**kw: object) -> SubjectActors:
    base: dict[str, object] = {
        "titles": ("",),
        "detected_actor_ids": (),
        "llm_primary_actor_id": "",
        "llm_confidence": "low",
        "category": "apt",
        "registry": _registry(),
    }
    base.update(kw)
    return determine_subject_actors(**base)  # type: ignore[arg-type]


class TestTitleTier:
    def test_title_hit_is_subject(self) -> None:
        got = _determine(titles=("Turla、欧州の外務省を標的とする新バックドア",))
        assert got.ids == ("turla",)
        assert got.source == SOURCE_TITLE

    def test_cjk_boundary_and_org_in_cyber_category(self) -> None:
        # 日本語タイトル中の "FSB" (ASCII 境界は非 ASCII 文字で成立)。cyber カテゴリ
        # (advisory) では organization も主題になれる
        got = _determine(
            titles=("EUと英国、ロシアFSB「第16センター」を名指し制裁",),
            category="advisory",
        )
        assert got.ids == ("russia_fsb",)
        assert got.source == SOURCE_TITLE

    def test_org_excluded_in_non_cyber_category(self) -> None:
        # R-A: 地政学カテゴリでは機関の言及を主題にしない (FSB 巡視船の記事等)
        got = _determine(
            titles=("ロシア FSB 巡視船をウクライナが撃沈と報告",),
            category="geopolitical",
        )
        assert got.ids == ()
        assert got.source == SOURCE_NONE

    def test_sponsor_org_suppressed_when_group_in_title(self) -> None:
        # 「APT28 (GRU 配下)」はグループの活動 — 親機関は主題にしない (二重計上規則と同じ)
        got = _determine(titles=("APT28 (GRU 配下) の新作戦を確認",))
        assert got.ids == ("apt28",)

    def test_multiple_titles_union(self) -> None:
        # 表示タイトル (和訳) + 原題の両方を走査する
        got = _determine(titles=("新たな諜報活動を確認", "Turla deploys new backdoor"))
        assert got.ids == ("turla",)

    def test_ambiguous_actor_requires_cue_in_title(self) -> None:
        # 曖昧アクター (Anonymous) はタイトルに文脈 cue が無ければ主題にしない
        got = _determine(titles=("Anonymous officials confirm the incident",))
        assert got.ids == ()


class TestLlmTier:
    def test_llm_primary_resolved_and_in_mentions(self) -> None:
        got = _determine(
            titles=("ロシア APT がネットワーク機器を攻撃",),  # タイトルにアクター名なし
            detected_actor_ids=("turla", "salt_typhoon"),
            llm_primary_actor_id="turla",
            llm_confidence="high",
        )
        assert got.ids == ("turla",)
        assert got.source == SOURCE_LLM
        assert got.confidence == "high"

    def test_slug_with_hyphen_resolves(self) -> None:
        got = _determine(
            detected_actor_ids=("salt_typhoon",),
            llm_primary_actor_id="salt-typhoon",  # _normalize_slug の出力形
            llm_confidence="medium",
        )
        assert got.ids == ("salt_typhoon",)

    def test_not_in_mentions_rejected(self) -> None:
        # 記事に名前が出ていないアクターは主題にできない (帰属の捏造防止)
        got = _determine(
            detected_actor_ids=("turla",),
            llm_primary_actor_id="salt-typhoon",
            llm_confidence="high",
        )
        assert got.ids == ()
        assert got.source == SOURCE_NONE

    def test_low_confidence_not_used(self) -> None:
        # routing の use_llm_primary と同一規約: low は不採用 → 主題なし (正直な不明)
        got = _determine(
            detected_actor_ids=("turla",),
            llm_primary_actor_id="turla",
            llm_confidence="low",
        )
        assert got.source == SOURCE_NONE

    def test_flag_off_disables_llm_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SUBJECT_ACTOR_LLM", "0")
        got = _determine(
            detected_actor_ids=("turla",),
            llm_primary_actor_id="turla",
            llm_confidence="high",
        )
        assert got.source == SOURCE_NONE

    def test_title_tier_wins_over_llm(self) -> None:
        # 決定論優先: タイトルにヒットがあれば LLM 判定は使わない
        got = _determine(
            titles=("Salt Typhoon が通信事業者へ侵入",),
            detected_actor_ids=("salt_typhoon", "turla"),
            llm_primary_actor_id="turla",
            llm_confidence="high",
        )
        assert got.ids == ("salt_typhoon",)
        assert got.source == SOURCE_TITLE


class TestResolveActorByName:
    def test_exact_match_bypasses_ambiguous_gate(self) -> None:
        # slug は「この名前のアクター」という主張 — 文脈 cue を要求しない
        got = resolve_actor_by_name("anonymous", _registry())
        assert got is not None and got.id == "anonymous"

    def test_partial_form_falls_back_to_find(self) -> None:
        got = resolve_actor_by_name("lazarus", _registry())
        assert got is not None and got.id == "lazarus"

    def test_unknown_returns_none(self) -> None:
        assert resolve_actor_by_name("zzyzx bear", _registry()) is None
