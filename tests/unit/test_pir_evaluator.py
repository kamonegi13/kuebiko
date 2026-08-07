"""src/pir/evaluator.py: match logic 単体テスト (DB 非依存パート)。"""

from __future__ import annotations

from src.pir.evaluator import evaluate_pir_for_article, has_strong_signals
from src.pir.models import Pir, StrongSignals


def test_has_strong_signals_empty() -> None:
    assert has_strong_signals(Pir(id="x", title="x")) is False


def test_has_strong_signals_with_keywords() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(keywords=["a"]))
    assert has_strong_signals(p) is True


def test_evaluate_for_article_no_signals_returns_empty() -> None:
    p = Pir(id="x", title="x")
    article = {
        "title": "anything",
        "summary": "",
        "body": "",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": None,
    }
    assert evaluate_pir_for_article(p, article) == ()


def test_evaluate_for_article_keyword_match_in_title() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(keywords=["ransomware"]))
    article = {
        "title": "Ransomware attack hits company",
        "summary": "",
        "body": "",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": None,
    }
    matched = evaluate_pir_for_article(p, article)
    assert "keywords" in matched


def test_evaluate_for_article_actor_match_in_body() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(actors=["Volt Typhoon"]))
    article = {
        "title": "report",
        "summary": "",
        "body": "Volt Typhoon was observed in ...",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": None,
    }
    matched = evaluate_pir_for_article(p, article)
    assert "actors" in matched


def test_evaluate_for_article_actor_via_entities() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(actors=["Lazarus"]))
    article = {
        "title": "report",
        "summary": "",
        "body": "no actor name in text",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": None,
    }
    matched = evaluate_pir_for_article(p, article, actor_values={"lazarus"})
    assert "actors" in matched


def test_evaluate_for_article_sector_match() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(sectors=["defense"]))
    article = {
        "title": "x",
        "summary": "",
        "body": "",
        "feed_title": "F",
        "victim_sector_canonical": "defense",
        "victim_country_iso": None,
    }
    matched = evaluate_pir_for_article(p, article)
    assert "sectors" in matched


def test_evaluate_for_article_country_match() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(countries=["JP"]))
    article = {
        "title": "x",
        "summary": "",
        "body": "",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": "JP",
    }
    matched = evaluate_pir_for_article(p, article)
    assert "countries" in matched


class TestActorNations:
    """監査 backlog 2026-07-05: APT 系 PIR の countries 意味反転の修正 (actor_nations)。"""

    @staticmethod
    def _article(**overrides: object) -> dict[str, object]:
        base: dict[str, object] = {
            "title": "x",
            "summary": "",
            "body": "",
            "feed_title": "F",
            "victim_sector_canonical": None,
            "victim_country_iso": None,
        }
        return {**base, **overrides}

    def test_actor_nation_matches_via_actor_entity(self) -> None:
        # 記事の actor entity (Volt Typhoon, 辞書 nation=cn) が actor_nations=[CN] に一致
        p = Pir(id="x", title="中国APT", strong_signals=StrongSignals(actor_nations=["CN"]))
        matched = evaluate_pir_for_article(p, self._article(), actor_values={"volt typhoon"})
        assert "actor_nations" in matched

    def test_china_as_victim_does_not_match_actor_nations(self) -> None:
        # 意味反転の回帰固定: 「中国=被害者」の記事は中国 APT 動向 PIR に match しない
        p = Pir(id="x", title="中国APT", strong_signals=StrongSignals(actor_nations=["CN"]))
        matched = evaluate_pir_for_article(
            p, self._article(victim_country_iso="CN"), actor_values=set()
        )
        assert matched == ()

    def test_non_target_nation_actor_does_not_match(self) -> None:
        # ロシア系 actor (Sandworm) は中国 actor_nations に match しない
        p = Pir(id="x", title="中国APT", strong_signals=StrongSignals(actor_nations=["CN"]))
        matched = evaluate_pir_for_article(p, self._article(), actor_values={"sandworm"})
        assert "actor_nations" not in matched

    def test_has_strong_signals_counts_actor_nations(self) -> None:
        p = Pir(id="x", title="x", strong_signals=StrongSignals(actor_nations=["CN"]))
        assert has_strong_signals(p) is True


def test_evaluate_for_article_feed_title_substring() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(feed_titles=["JPCERT"]))
    article = {
        "title": "x",
        "summary": "",
        "body": "",
        "feed_title": "JPCERT/CC 統合 RSS",
        "victim_sector_canonical": None,
        "victim_country_iso": None,
    }
    matched = evaluate_pir_for_article(p, article)
    assert "feed_titles" in matched


def test_multiple_signal_types_can_match() -> None:
    p = Pir(id="x", title="x", strong_signals=StrongSignals(keywords=["ransom"], countries=["JP"]))
    article = {
        "title": "ransom hit",
        "summary": "",
        "body": "",
        "feed_title": "F",
        "victim_sector_canonical": None,
        "victim_country_iso": "JP",
    }
    matched = evaluate_pir_for_article(p, article)
    assert "keywords" in matched
    assert "countries" in matched


# (⑤ exclude_signals は 2026-07-23 撤去 — 利用 0 件、除外は match ツリーの not 節で表現。
# 除外挙動のテストは test_pir_signal_match.py の not combinator が担う。)


# ===================== B2: strong_signals.signals (派生 signal 条件) =====================


def test_has_strong_signals_with_only_signals() -> None:
    from src.pir.models import StrongSignals

    p = Pir(id="x", title="x", strong_signals=StrongSignals(signals=["kev"]))
    assert has_strong_signals(p) is True


def test_signal_condition_matches_when_derived_true() -> None:
    from src.pir.models import StrongSignals

    p = Pir(id="x", title="x", strong_signals=StrongSignals(signals=["kev", "zero_day"]))
    article = {"title": "patch now", "summary": "", "body": ""}
    matched = evaluate_pir_for_article(p, article, derived_signals={"kev": True})
    assert "signals" in matched


def test_signal_condition_no_match_when_derived_false() -> None:
    from src.pir.models import StrongSignals

    p = Pir(id="x", title="x", strong_signals=StrongSignals(signals=["kev"]))
    article = {"title": "patch", "summary": "", "body": ""}
    assert evaluate_pir_for_article(p, article, derived_signals={"kev": False}) == ()


def test_signal_condition_skipped_when_no_derived_supplied() -> None:
    """DB 経路 (derived_signals=None) では signals 条件は評価されない (graceful skip)。"""
    from src.pir.models import StrongSignals

    p = Pir(id="x", title="x", strong_signals=StrongSignals(signals=["kev"]))
    article = {"title": "patch", "summary": "", "body": ""}
    assert evaluate_pir_for_article(p, article) == ()


def test_signal_condition_or_with_keywords() -> None:
    from src.pir.models import StrongSignals

    p = Pir(
        id="x",
        title="x",
        strong_signals=StrongSignals(keywords=["ransomware"], signals=["kev"]),
    )
    # keyword は外れるが signal が当たる
    article = {"title": "advisory", "summary": "", "body": ""}
    assert "signals" in evaluate_pir_for_article(p, article, derived_signals={"kev": True})


class TestKeywordWordBoundary:
    """監査 2026-07-05 P3: ASCII keyword の語境界必須化 (OT/ICS/IO の内包偽陽性根治)。"""

    def test_ot_does_not_match_inside_words(self) -> None:
        from src.pir.evaluator import _keyword_in_text

        assert not _keyword_in_text("OT", "this is not both protocols")
        assert _keyword_in_text("OT", "attack on ot networks")  # text は小文字化済み前提

    def test_ics_does_not_match_statistics(self) -> None:
        from src.pir.evaluator import _keyword_in_text

        assert not _keyword_in_text("ICS", "statistics and politics of graphics")
        assert _keyword_in_text("ICS", "ics/scada environment compromised")

    def test_io_does_not_match_million(self) -> None:
        from src.pir.evaluator import _keyword_in_text

        assert not _keyword_in_text("IO", "$3 million in版本 versions")
        assert _keyword_in_text("IO", "state-run io campaign detected")

    def test_japanese_keyword_stays_substring(self) -> None:
        from src.pir.evaluator import _keyword_in_text

        assert _keyword_in_text("脆弱性", "重大な脆弱性が悪用されている")

    def test_symbol_suffix_abbreviation(self) -> None:
        from src.pir.evaluator import _keyword_in_text

        assert _keyword_in_text("ED-", "cisa issued ed-26-01 emergency directive")
        assert not _keyword_in_text("ED-", "concluded-2026 report")


class TestSubjectGate:
    """主題ゲート (docs/subject_actor_attribution_design.md §5)。

    契機 = FSB 第16センター記事が Salt Typhoon への比較言及で中国系 APT PIR に
    誤分類されたインシデント (2026-07-17)。実辞書 (config/actor_aliases.yaml) を使う。
    """

    def _fsb_article(
        self,
        subject_ids: str | None,
        subject_source: str | None,
    ) -> dict[str, object]:
        return {
            "title": "EUと英国、ロシアFSB「第16センター」を名指し制裁",
            "summary": "第16センターの手法は中国系アクター「Salt Typhoon」の手法とも類似している。",
            "body": "",
            "feed_title": "F",
            "victim_sector_canonical": None,
            "victim_country_iso": None,
            "subject_actor_ids": subject_ids,
            "subject_actor_source": subject_source,
        }

    def _china_pir(self) -> Pir:
        return Pir(
            id="pir_china_apt",
            title="c",
            strong_signals=StrongSignals(actors=["Salt Typhoon"], actor_nations=["CN"]),
        )

    def test_comparative_mention_excluded_when_subject_known(self) -> None:
        """主題=turla の行は、比較言及 (Salt Typhoon) でも中国 PIR に一致しない。"""
        matched = evaluate_pir_for_article(
            self._china_pir(),
            self._fsb_article("turla", "llm"),
            {"salt_typhoon", "turla"},  # 言及 entity (比較言及込み) があっても主題が勝つ
        )
        assert matched == ()

    def test_subject_matches_relevant_pir_with_provenance(self) -> None:
        """同じ行がロシア PIR には主題 id → nation で一致し、provenance が併記される。"""
        russia = Pir(
            id="pir_russia_apt",
            title="r",
            strong_signals=StrongSignals(actor_nations=["RU"]),
        )
        matched = evaluate_pir_for_article(
            russia, self._fsb_article("turla", "llm"), {"salt_typhoon", "turla"}
        )
        assert "actor_nations" in matched
        assert "subject:llm" in matched

    def test_subject_actors_match_by_dictionary_id(self) -> None:
        """PIR actors 名 (表示名) が辞書 id に解決され、主題 id (複数語 id) と一致する。

        旧実装は id vs 名前の齟齬で entity 照合が不発だった — 主題ゲート側で修復。
        """
        article = {
            "title": "Salt Typhoon が通信事業者へ侵入",
            "summary": "",
            "body": "",
            "feed_title": "F",
            "victim_sector_canonical": None,
            "victim_country_iso": None,
            "subject_actor_ids": "salt_typhoon",
            "subject_actor_source": "title",
        }
        matched = evaluate_pir_for_article(self._china_pir(), article, {"salt_typhoon"})
        assert "actors" in matched
        assert "actor_nations" in matched  # salt_typhoon (id) → cn の解決も主題側で機能
        assert "subject:title" in matched

    def test_legacy_null_row_keeps_mention_matching(self) -> None:
        """未評価 (source=NULL) 行は従来のテキスト照合を維持する (回帰ピン)。

        legacy 行に id 修復を適用しないのも仕様 (設計 §1 ⚠) — backfill と窓経過で収束。
        """
        matched = evaluate_pir_for_article(
            self._china_pir(), self._fsb_article(None, None), {"salt_typhoon", "turla"}
        )
        assert "actors" in matched  # 比較言及テキストで一致 (旧挙動そのまま)

    def test_evaluated_none_blocks_actor_signals(self) -> None:
        """評価済み・主題なし ('none') は actors/actor_nations を発火させない。"""
        matched = evaluate_pir_for_article(
            self._china_pir(), self._fsb_article("", "none"), {"salt_typhoon", "turla"}
        )
        assert matched == ()

    def test_unresolved_pir_actor_falls_back_to_title_only(self) -> None:
        """辞書外の PIR アクター名はタイトル語境界照合のみに fallback する。"""
        pir = Pir(id="x", title="x", strong_signals=StrongSignals(actors=["Zzyzx Bear"]))
        in_title = {
            "title": "Zzyzx Bear targets energy sector",
            "summary": "",
            "body": "",
            "feed_title": "F",
            "victim_sector_canonical": None,
            "victim_country_iso": None,
            "subject_actor_ids": "",
            "subject_actor_source": "none",
        }
        only_in_summary = dict(in_title) | {
            "title": "energy sector attacks",
            "summary": "similar to Zzyzx Bear tradecraft",
        }
        assert "actors" in evaluate_pir_for_article(pir, in_title)
        assert evaluate_pir_for_article(pir, only_in_summary) == ()

    def test_gate_flag_off_restores_legacy(self, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        """SUBJECT_ACTOR_GATE=0 で評価済み行も legacy テキスト照合に戻る (rollback 経路)。"""
        monkeypatch.setenv("SUBJECT_ACTOR_GATE", "0")
        matched = evaluate_pir_for_article(
            self._china_pir(), self._fsb_article("turla", "llm"), {"salt_typhoon", "turla"}
        )
        assert "actors" in matched


class TestActorNationsStateOnly:
    """actor_nations = 国家系アクターの国籍 (2026-07-18 ユーザー判断: Qilin は APT ではない)。

    nation 帰属つき犯罪ランサム/ハクティビストを国別 APT PIR に載せない。実辞書使用
    (qilin: family=ransom_group, nation=ru / sandworm: family=sandworm, nation=ru)。
    """

    def _russia_pir(self) -> Pir:
        return Pir(id="r", title="r", strong_signals=StrongSignals(actor_nations=["RU"]))

    def _article(self, subject_ids: str | None, subject_source: str | None) -> dict[str, object]:
        return {
            "title": "被害組織の公表",
            "summary": "",
            "body": "",
            "feed_title": "F",
            "victim_sector_canonical": None,
            "victim_country_iso": None,
            "subject_actor_ids": subject_ids,
            "subject_actor_source": subject_source,
        }

    def test_ransomware_subject_does_not_match_nation_pir(self) -> None:
        matched = evaluate_pir_for_article(
            self._russia_pir(), self._article("qilin", "title"), {"qilin"}
        )
        assert matched == ()

    def test_state_actor_subject_still_matches(self) -> None:
        matched = evaluate_pir_for_article(
            self._russia_pir(), self._article("sandworm", "title"), {"sandworm"}
        )
        assert "actor_nations" in matched

    def test_legacy_row_ransomware_entity_does_not_match(self) -> None:
        # legacy 行 (未評価) の entity 経由でも犯罪系 nation は国別 PIR に載せない
        matched = evaluate_pir_for_article(self._russia_pir(), self._article(None, None), {"qilin"})
        assert matched == ()
