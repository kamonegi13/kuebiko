"""signal-first PIR 照合 (ArticleFacts + evaluate_match + dispatcher) の unit テスト。

docs/pir_signal_first_matching_design.md の PoC 実装。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.pir.article_facts import ArticleFacts
from src.pir.evaluator import has_match_criteria, pir_match_signals
from src.pir.models import LlmJudgeConfig, Pir, StrongSignals
from src.pir.signal_match import evaluate_match


def _row(**overrides: Any) -> dict[str, Any]:
    """テスト用の articles 行 (アクセスされる全キーを default で埋める)。"""
    base: dict[str, Any] = {
        "article_id": "a1",
        "title": "",
        "summary": "",
        "body": "",
        "feed_title": "",
        "category": "",
        "socio_political_intent": "",
        "victim_sector_canonical": "",
        "victim_country_iso": "",
        "is_ransomware": 0,
        "article_type": "",
        "subject_actor_source": "",
        "subject_actor_ids": "",
    }
    base.update(overrides)
    return base


# ---- ArticleFacts.from_db_row ----


def test_from_db_row_coerces_smallint_ransomware_to_bool() -> None:
    # Arrange / Act
    facts_true = ArticleFacts.from_db_row(_row(is_ransomware=1))
    facts_false = ArticleFacts.from_db_row(_row(is_ransomware=0))

    # Assert
    assert facts_true.is_ransomware is True
    assert facts_false.is_ransomware is False


def test_from_db_row_lowercases_and_defaults_missing() -> None:
    facts = ArticleFacts.from_db_row({"category": "Vulnerability", "victim_country_iso": "JP"})

    assert facts.category == "vulnerability"
    assert facts.victim_country == "jp"
    assert facts.intent == ""  # 欠損キーは空文字


# ---- evaluate_match: leaves ----


def test_leaf_str_eq_matches_and_reports_property() -> None:
    facts = ArticleFacts(category="vulnerability")
    ok, fired = evaluate_match(
        {"property": "category", "op": "eq", "value": "vulnerability"}, facts
    )

    assert ok is True
    assert fired == frozenset({"category"})


def test_leaf_str_in_is_case_insensitive() -> None:
    facts = ArticleFacts(intent="influence")
    ok, _ = evaluate_match(
        {"property": "intent", "op": "in", "value": ["Influence", "subversion"]}, facts
    )

    assert ok is True


def test_leaf_boolean_is_true() -> None:
    ok, _ = evaluate_match(
        {"property": "is_ransomware", "op": "is_true"}, ArticleFacts(is_ransomware=True)
    )
    no, _ = evaluate_match(
        {"property": "is_ransomware", "op": "is_true"}, ArticleFacts(is_ransomware=False)
    )

    assert ok is True
    assert no is False


def test_leaf_feed_title_contains_any_substring() -> None:
    # feed_title は完全名。contains_any は短い識別子で substring 照合する。
    facts = ArticleFacts(feed_title="cisa cybersecurity advisories (incl. kev)")
    hit, fired = evaluate_match(
        {"property": "feed_title", "op": "contains_any", "value": ["CISA", "JPCERT"]}, facts
    )
    miss, _ = evaluate_match(
        {"property": "feed_title", "op": "contains_any", "value": ["ENISA", "NCSC"]}, facts
    )

    assert hit is True
    assert fired == frozenset({"feed_title"})
    assert miss is False


def test_leaf_keyword_any_word_boundary() -> None:
    # text は小文字済み前提。keyword_any は語境界照合 (ASCII) / substring (日本語)。
    facts = ArticleFacts(text="the i-soon leak exposed 内部漏洩 operations")
    hit, fired = evaluate_match(
        {"property": "text", "op": "keyword_any", "value": ["i-Soon", "Vulkan"]}, facts
    )
    hit_jp, _ = evaluate_match(
        {"property": "text", "op": "keyword_any", "value": ["内部漏洩"]}, facts
    )
    miss, _ = evaluate_match(
        {"property": "text", "op": "keyword_any", "value": ["Conti chat", "log4j"]}, facts
    )

    assert hit is True
    assert fired == frozenset({"keyword"})  # keyword は property 非依存の provenance
    assert hit_jp is True
    assert miss is False


def test_keyword_any_title_scope_ignores_body_mentions() -> None:
    # title スコープ: body に語があってもタイトルに無ければ不成立 (recall 監査 2026-07-23)。
    # 「制裁」「日本」の body 言及氾濫 (地政学記事) を避け、タイトル=主題の近似で照合する。
    facts = ArticleFacts(
        title_text="eu、ロシア諜報機関の職員らに制裁を科す",
        text="eu、ロシア諜報機関の職員らに制裁を科す 本文には 日本 への言及もある",
    )
    title_leaf = {"property": "title", "op": "keyword_any", "value": ["制裁", "sanction"]}
    body_only = {"property": "title", "op": "keyword_any", "value": ["日本"]}
    fulltext = {"property": "text", "op": "keyword_any", "value": ["日本"]}

    assert evaluate_match(title_leaf, facts)[0] is True
    assert evaluate_match(body_only, facts)[0] is False  # 日本 は body のみ → title 不成立
    assert evaluate_match(fulltext, facts)[0] is True  # 全文スコープは従来どおり


def test_from_db_row_builds_title_text() -> None:
    facts = ArticleFacts.from_db_row({"title": "米郡政府へのランサムウェア", "body": "b"})

    assert facts.title_text == "米郡政府へのランサムウェア"


def test_keyword_any_combined_with_semantic_clause() -> None:
    # supply_chain 型: not geopolitical AND keyword (弱補強を意味 clause で scope)。
    tree = {
        "all": [
            {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
            {"property": "text", "op": "keyword_any", "value": ["log4j", "xz"]},
        ]
    }
    cyber = ArticleFacts(category="vulnerability", text="a new log4j exploit chain")
    geo = ArticleFacts(category="geopolitical", text="log4j mentioned in a policy debate")

    assert evaluate_match(tree, cyber)[0] is True
    assert evaluate_match(tree, geo)[0] is False  # geopolitical は keyword があっても除外


def test_unknown_property_is_failsafe_false() -> None:
    ok, fired = evaluate_match({"property": "not_a_prop", "op": "eq", "value": "x"}, ArticleFacts())

    assert ok is False
    assert fired == frozenset()


# ---- evaluate_match: combinators ----


def test_all_requires_every_child() -> None:
    tree = {
        "all": [
            {"property": "is_ransomware", "op": "is_true"},
            {
                "property": "victim_sector",
                "op": "in",
                "value": ["government", "healthcare", "energy"],
            },
        ]
    }
    hit = ArticleFacts(is_ransomware=True, victim_sector="healthcare")
    miss_sector = ArticleFacts(is_ransomware=True, victim_sector="finance")
    miss_ransom = ArticleFacts(is_ransomware=False, victim_sector="healthcare")

    ok_hit, fired = evaluate_match(tree, hit)
    ok_sector, _ = evaluate_match(tree, miss_sector)
    ok_ransom, _ = evaluate_match(tree, miss_ransom)

    assert ok_hit is True
    assert fired == frozenset({"is_ransomware", "victim_sector"})
    assert ok_sector is False
    assert ok_ransom is False


def test_any_needs_one_child() -> None:
    tree = {
        "any": [
            {"property": "category", "op": "eq", "value": "vulnerability"},
            {"property": "is_ransomware", "op": "is_true"},
        ]
    }

    assert evaluate_match(tree, ArticleFacts(category="vulnerability"))[0] is True
    assert evaluate_match(tree, ArticleFacts(is_ransomware=True))[0] is True
    assert evaluate_match(tree, ArticleFacts(category="breach"))[0] is False


def test_not_negates() -> None:
    tree = {"not": {"property": "category", "op": "eq", "value": "geopolitical"}}

    assert evaluate_match(tree, ArticleFacts(category="vulnerability"))[0] is True
    assert evaluate_match(tree, ArticleFacts(category="geopolitical"))[0] is False


# ---- dispatcher: flag gating (PIR_SIGNAL_FIRST) ----


def _pir_with_both() -> Pir:
    """keyword(ransomware) と signal(category=vulnerability) が別々の記事に当たる PIR。"""
    return Pir(
        id="pir_test",
        title="t",
        strong_signals=StrongSignals(keywords=["ransomware"]),
        match={"property": "category", "op": "eq", "value": "vulnerability"},
    )


def test_dispatch_flag_off_uses_keyword_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "0")
    pir = _pir_with_both()
    # category=vulnerability だが body に ransomware 無し → keyword 経路では no-match
    row = _row(category="vulnerability", body="a security advisory")

    assert pir_match_signals(pir, row, set()) == ()  # type: ignore[arg-type]


def test_dispatch_flag_on_uses_signal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    pir = _pir_with_both()
    row = _row(category="vulnerability", body="a security advisory")

    assert pir_match_signals(pir, row, set()) == ("signal:category",)  # type: ignore[arg-type]


def test_dispatch_flag_on_without_match_falls_back_to_keyword(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    pir = Pir(id="pir_kw", title="t", strong_signals=StrongSignals(keywords=["ransomware"]))
    hit = _row(body="a ransomware incident")
    miss = _row(body="a patch advisory")

    assert pir_match_signals(pir, hit, set()) == ("keywords",)  # type: ignore[arg-type]
    assert pir_match_signals(pir, miss, set()) == ()  # type: ignore[arg-type]


def test_signal_first_default_on() -> None:
    """Phase E (2026-07-23): env 未設定でも signal-first が既定 ON。"""
    pir = _pir_with_both()
    row = _row(category="vulnerability", body="a security advisory")

    assert pir_match_signals(pir, row, set()) == ("signal:category",)  # type: ignore[arg-type]


# ---- LLM 主題判定の合成 (docs/pir_concept_llm_judge_design.md §4.5) ----


def _judge_pir() -> Pir:
    return Pir(
        id="pir_judge",
        title="t",
        match={"property": "category", "op": "eq", "value": "apt"},
        llm_judge=LlmJudgeConfig(enabled=True),
    )


def test_llm_judge_requires_confirmed_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    pir = _judge_pir()
    row = _row(category="apt")

    # 候補ゲート通過でも verdict 無し → 不適合 (夜間バッチ後の rebuild で昇格)
    assert pir_match_signals(pir, row, set()) == ()  # type: ignore[arg-type]
    # 適合 verdict あり → match + provenance に llm:subject
    via = pir_match_signals(pir, row, set(), llm_confirmed=frozenset({"pir_judge"}))  # type: ignore[arg-type]
    assert via == ("signal:category", "llm:subject")


def test_llm_judge_flag_off_degrades_to_candidate_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    monkeypatch.setenv("PIR_LLM_JUDGE", "0")
    pir = _judge_pir()
    row = _row(category="apt")

    # rollback: 候補ゲートのみで match (verdict 不要)
    assert pir_match_signals(pir, row, set()) == ("signal:category",)  # type: ignore[arg-type]


def test_llm_judge_require_llm_false_returns_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    pir = _judge_pir()
    row = _row(category="apt")

    # preview 用: 候補ゲートのみの評価 (LLM 未確定候補の算出)
    via = pir_match_signals(pir, row, set(), require_llm=False)  # type: ignore[arg-type]
    assert via == ("signal:category",)


def test_has_match_criteria_accepts_tree_only_pir() -> None:
    tree_only = Pir(id="p1", title="t", match={"property": "category", "op": "eq", "value": "x"})
    empty = Pir(id="p2", title="t")

    assert has_match_criteria(tree_only) is True
    assert has_match_criteria(empty) is False


# ---- actor / actor_nation leaf (Tier 1 の移植、authoring 統一 §3.1) ----
# 実辞書 (config/actor_aliases.yaml) を使う — test_pir_evaluator.TestSubjectGate と同じ流儀。

_APT_TREE = {
    "any": [
        {"property": "actor", "op": "any_of", "value": ["Salt Typhoon"]},
        {"property": "actor_nation", "op": "in", "value": ["cn"]},
    ]
}


def _fsb_row(subject_ids: str | None, subject_source: str | None) -> dict[str, Any]:
    return _row(
        title="EUと英国、ロシアFSB「第16センター」を名指し制裁",
        summary="第16センターの手法は中国系アクター「Salt Typhoon」の手法とも類似している。",
        subject_actor_ids=subject_ids or "",
        subject_actor_source=subject_source or "",
    )


class TestActorLeafParity:
    """actor/actor_nation leaf が legacy strong_signals 照合と同一結果を返すこと。"""

    def _legacy_pir(self) -> Pir:
        return Pir(
            id="pir_china_apt",
            title="c",
            strong_signals=StrongSignals(actors=["Salt Typhoon"], actor_nations=["CN"]),
        )

    def _tree_pir(self) -> Pir:
        return Pir(id="pir_china_apt", title="c", match=_APT_TREE)

    def test_comparative_mention_excluded_when_subject_known(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """主題=turla の行は比較言及 (Salt Typhoon) でも一致しない — legacy と同じ。"""
        monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
        row = _fsb_row("turla", "llm")
        actor_values = {"salt_typhoon", "turla"}

        legacy = pir_match_signals(self._legacy_pir(), row, actor_values)  # type: ignore[arg-type]
        tree = pir_match_signals(self._tree_pir(), row, actor_values)  # type: ignore[arg-type]
        assert legacy == ()
        assert tree == ()

    def test_subject_nation_match_with_provenance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """主題 id → nation 解決の一致と provenance (subject:llm) — legacy と同型。"""
        monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
        russia_tree = Pir(
            id="pir_russia_apt",
            title="r",
            match={"property": "actor_nation", "op": "in", "value": ["ru"]},
        )
        row = _fsb_row("turla", "llm")

        via = pir_match_signals(russia_tree, row, {"salt_typhoon", "turla"})  # type: ignore[arg-type]
        assert "signal:actor_nation" in via
        assert "signal:subject:llm" in via

    def test_legacy_null_row_matches_by_text_and_entity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未評価 (subject NULL) 行は従来のテキスト/entity 照合を維持 (bug-for-bug 温存)。"""
        monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
        row = _fsb_row(None, None)

        # 本文言及 (salt typhoon が summary に含まれる) → legacy 行では一致する
        via_text = pir_match_signals(self._tree_pir(), row, set())  # type: ignore[arg-type]
        assert "signal:actor" in via_text
        # entity 経由 (text に無い名前でも actor_values で一致)
        plain = _row(title="通信事業者への侵入", subject_actor_ids="", subject_actor_source="")
        via_entity = pir_match_signals(self._tree_pir(), plain, {"salt typhoon"})  # type: ignore[arg-type]
        assert "signal:actor" in via_entity

    def test_subject_actor_id_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PIR actors 名が辞書 id に解決され主題 id と一致する (legacy と同じ修復挙動)。"""
        monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
        row = _row(
            title="Salt Typhoon が通信事業者へ侵入",
            subject_actor_ids="salt_typhoon",
            subject_actor_source="title",
        )

        via = pir_match_signals(self._tree_pir(), row, {"salt_typhoon"})  # type: ignore[arg-type]
        assert "signal:actor" in via
        assert "signal:subject:title" in via


# ---- validate_match_tree (authoring 統一 §3.2) ----


def test_validate_accepts_deployed_style_tree() -> None:
    from src.pir.signal_match import validate_match_tree

    errors, warnings = validate_match_tree(
        {
            "any": [
                {
                    "all": [
                        {"property": "victim_country", "op": "eq", "value": "jp"},
                        {"not": {"property": "category", "op": "in", "value": ["geopolitical"]}},
                    ]
                },
                {"property": "title", "op": "keyword_any", "value": ["日本", "日系"]},
                {"property": "is_ransomware", "op": "is_true"},
            ]
        }
    )
    assert errors == []
    assert warnings == []


def test_validate_rejects_unknown_property_and_bad_op() -> None:
    from src.pir.signal_match import validate_match_tree

    errors, _ = validate_match_tree({"property": "bogus", "op": "eq", "value": "x"})
    assert any("bogus" in e for e in errors)

    errors2, _ = validate_match_tree({"property": "category", "op": "keyword_any", "value": ["x"]})
    assert errors2

    errors3, _ = validate_match_tree({"property": "category", "op": "in"})  # value 欠落
    assert errors3

    errors4, _ = validate_match_tree({"all": []})  # 空 combinator
    assert errors4


def test_validate_warns_on_unknown_category_value() -> None:
    from src.pir.signal_match import validate_match_tree

    errors, warnings = validate_match_tree(
        {"property": "category", "op": "in", "value": ["vulnerability", "not_a_category"]}
    )
    assert errors == []
    assert any("not_a_category" in w for w in warnings)


def test_pir_match_property_vocab_covers_all_leaf_properties() -> None:
    """UI ラベル vocab (pir_match_property) と LEAF_OPS の SSoT 一致 (同一言語 pytest 強制)。"""
    from src.pir.signal_match import KNOWN_PROPERTIES
    from src.vocab.registry import get_vocabulary

    vocab = get_vocabulary("pir_match_property")
    assert vocab is not None
    assert frozenset(vocab.values()) == KNOWN_PROPERTIES
