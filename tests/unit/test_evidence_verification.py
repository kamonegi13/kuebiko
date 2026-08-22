"""証拠の実在検査 (引用実在チェック)。

背景 (2026-08-22 実測): synthesis の証拠は「どの記事か」(article_id) と
「その記事の何が根拠か」(excerpt) の 2 要素だが、どちらも実在検査が無かった。
- 記事参照: situation_evidence 3,911 件中 123 件 (3.14%) が実在しない記事を指す。
  全件が record_assessment 経路 = detect_new にある valid_ids 検査が無い唯一の口。
- 引用抜粋: 3,308 件中 1,983 件 (59.9%) が本文 (原文 + 日本語訳) に存在しない。
  プロンプトは既に「要約でなく該当箇所」と指示しており、指示では止まっていない。

CTI では「証拠が無い」より「たどれない証拠を示す」ほうが悪い (件数と出典 tier が
裏取り済みに見える)。指示でなく書込 seam の関門で止める。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.assessment.evidence_verify import excerpt_is_supported, normalize_for_match
from src.assessment.situation_store import SituationStore
from src.storage.records import ArticleRecord, RunRecord


class TestNormalize:
    def test_collapses_all_whitespace(self) -> None:
        assert normalize_for_match("a b\n c\t d") == "abcd"

    def test_none_and_empty_are_empty(self) -> None:
        assert normalize_for_match("") == ""


class TestExcerptSupport:
    def test_verbatim_quote_is_supported(self) -> None:
        body = "研究者は APT29 が新たなローダーを用いたと報告した。"
        assert excerpt_is_supported("APT29 が新たなローダーを用いた", body) is True

    def test_whitespace_differences_are_tolerated(self) -> None:
        body = "The actor   deployed\na new loader in June."
        assert excerpt_is_supported("The actor deployed a new loader", body) is True

    def test_paraphrase_is_rejected(self) -> None:
        # 実データの典型: 本文の内容を日本語で言い換えた「証拠」
        body = "Houthi forces announced a blockade of shipping near Saudi Arabia."
        assert excerpt_is_supported("フーシ派が全面海上封鎖を宣言し米の攻撃に連動", body) is False

    def test_ellipsis_fragments_all_must_appear(self) -> None:
        body = (
            "developers left human traces in the code and likely use "
            "AI coding assistants for routine work"
        )
        ok = "developers left human traces...use AI coding assistants"
        ng = "developers left human traces...deployed a wiper"
        assert excerpt_is_supported(ok, body) is True
        assert excerpt_is_supported(ng, body) is False

    def test_too_short_excerpt_is_not_verifiable(self) -> None:
        # 短すぎる文字列は偶然一致するので証拠として扱わない
        body = "APT29 は新たなローダーを用いた"
        assert excerpt_is_supported("APT29", body) is False

    def test_empty_body_cannot_verify(self) -> None:
        assert excerpt_is_supported("何らかの引用文がここに入る", "") is False

    def test_matches_against_any_of_multiple_bodies(self) -> None:
        """原文が英語で引用が日本語訳本文から採られている場合を救う。"""
        en = "The group used a new loader."
        ja = "同グループは新しいローダーを使用した。"
        assert excerpt_is_supported("同グループは新しいローダーを使用した", en, ja) is True


class TestWriteSeamGate:
    """書込 seam (record_assessment) の関門。指示でなくここで止める。"""

    @pytest.fixture
    def store(self, tmp_path: Path) -> SituationStore:
        return SituationStore(db_path=tmp_path / "sit.db")

    @staticmethod
    def _seed_article(store: SituationStore, article_id: str, body: str) -> None:
        repo = store._repo  # noqa: SLF001 — テストからの意図的な内部参照
        rid = repo.start_run(
            RunRecord(started_at=datetime.now(UTC), pipeline="x", dry_run=False)
        )
        repo.add_article(
            ArticleRecord(
                run_id=rid,
                article_id=article_id,
                title="t",
                url=f"https://kuebiko.example/{article_id}",
                status="posted",
            )
        )
        if body:
            repo.update_article_body(article_id, body)

    @staticmethod
    def _rows(store: SituationStore) -> list[dict[str, Any]]:
        with store._repo._connect() as conn:  # noqa: SLF001
            return [dict(r) for r in conn.execute("SELECT * FROM situation_evidence")]

    @staticmethod
    def _record(store: SituationStore, article_id: str, excerpt: str) -> bool:
        return store.record_assessment(
            situation_id="s1",
            article_id=article_id,
            assessed_at="2026-08-22T00:00:00+00:00",
            polarity="supports",
            attribution_basis="vendor_attribution",
            excerpt=excerpt,
            source_tier="tier1",
        )

    def test_nonexistent_article_is_not_written(self, store: SituationStore) -> None:
        # Arrange / Act — LLM が実在しない id を出したケース
        written = self._record(store, "rss:https://kuebiko.example/ghost", "何らかの引用文です")

        # Assert — 台帳に入れない
        assert written is False
        assert self._rows(store) == []

    def test_verbatim_excerpt_is_kept(self, store: SituationStore) -> None:
        # Arrange
        self._seed_article(store, "a1", "研究者は APT29 が新たなローダーを用いたと報告した。")

        # Act
        self._record(store, "a1", "APT29 が新たなローダーを用いた")

        # Assert
        rows = self._rows(store)
        assert len(rows) == 1
        assert rows[0]["excerpt"] == "APT29 が新たなローダーを用いた"

    def test_paraphrase_excerpt_is_dropped_but_row_remains(self, store: SituationStore) -> None:
        # Arrange — 実データの典型 (本文の言い換え)
        self._seed_article(store, "a1", "Houthi forces announced a blockade near Saudi Arabia.")

        # Act
        self._record(store, "a1", "フーシ派が全面海上封鎖を宣言し米の攻撃に連動")

        # Assert — 観測の事実は残し、たどれない引用だけ落とす
        rows = self._rows(store)
        assert len(rows) == 1
        assert rows[0]["excerpt"] == ""

    def test_excerpt_kept_when_body_unavailable(self, store: SituationStore) -> None:
        """本文 purge 済み等で照合不能なら引用を保持する (検証不能 ≠ 反証)。"""
        # Arrange
        self._seed_article(store, "a1", "")

        # Act
        self._record(store, "a1", "照合できないが捏造とは限らない引用文")

        # Assert
        rows = self._rows(store)
        assert len(rows) == 1
        assert rows[0]["excerpt"] == "照合できないが捏造とは限らない引用文"


class TestNormalizationTolerance:
    """字体差は吸収するが、言い換えは通さない (偽陰性・偽陽性の両方を抑える)。"""

    def test_smart_quotes_and_dashes_are_tolerated(self) -> None:
        body = "The team “became aware of active exploitation” in June — per PSIRT."
        excerpt = "the team 'became aware of active exploitation' in June - per PSIRT"
        assert excerpt_is_supported(excerpt, body) is True

    def test_fullwidth_and_case_are_tolerated(self) -> None:
        body = "APT29 が新たなローダーを配布した。"
        assert excerpt_is_supported("ＡＰＴ29が新たなローダーを配布した", body) is True

    def test_paraphrase_still_rejected_after_loosening(self) -> None:
        # 記号を落としても言い換えは一致しない (緩めすぎていないことの固定)
        body = "Houthi forces announced a blockade of shipping near Saudi Arabia."
        assert excerpt_is_supported("フーシ派が全面海上封鎖を宣言し米の攻撃に連動", body) is False
        assert excerpt_is_supported("The group deployed a wiper against banks", body) is False


class TestSourceIdResolution:
    """LLM が返す article_id の転記破損を、prompt で渡した id へ寄せて解決する。

    実測された破損 (実 LLM 試験): feed 名の連結 / rss:https:// prefix 欠落 / 切り詰め。
    候補外を引用することは原理的に無いため、これらは捏造でなく転記の壊れ。
    """

    KNOWN = {"article_id": "rss:https://www.theregister.com/a/5290706"}

    @staticmethod
    def _resolve(raw: str, *ids: str) -> str | None:
        from src.synthesis.grounded.passes import _norm_id, _resolve_source_id

        known = {_norm_id(i): i for i in ids}
        return _resolve_source_id(raw, known)

    def test_exact_id_resolves(self) -> None:
        real = self.KNOWN["article_id"]
        assert self._resolve(real, real) == real

    def test_feed_title_suffix_is_stripped(self) -> None:
        real = self.KNOWN["article_id"]
        assert self._resolve(f"{real} | The Register", real) == real

    def test_missing_rss_prefix_is_recovered(self) -> None:
        real = "rss:https://gbhackers.com/?p=196463"
        assert self._resolve("gbhackers.com/?p=196463", real) == real

    def test_truncated_id_resolves_when_unique(self) -> None:
        real = "grok:e576425909d9a9e5:387#2090136982378700811"
        assert self._resolve("grok:e576425909d9a9e5", real) == real

    def test_ambiguous_truncation_is_not_resolved(self) -> None:
        # 誤った記事へ証拠を付けるより落とす (台帳は帰属の台帳)
        a = "rss:https://example.com/articles/2026/aaaa"
        b = "rss:https://example.com/articles/2026/bbbb"
        assert self._resolve("rss:https://example.com/articles/2026/", a, b) is None

    def test_unknown_id_is_not_resolved(self) -> None:
        real = self.KNOWN["article_id"]
        assert self._resolve("rss:https://never-seen.example/x/1", real) is None

    def test_short_fragment_does_not_match_by_prefix(self) -> None:
        real = self.KNOWN["article_id"]
        assert self._resolve("rss:", real) is None


class TestIndexReference:
    """ACH も Spotlight と同じ番号参照へ揃える (転記破損を原理的に起こさせない)。

    実測: ACH プロンプトが `--- id: <長い id> | <feed名> ---` と出していたため、LLM が
    行ごと写して `id | feed名` の破損が生じていた (実 LLM 試験で 2 claim が 8/8 件不在)。
    Spotlight は候補一覧の [N] 番号で参照させてこの класс を消している。
    """

    @staticmethod
    def _resolve(index: int | None, raw: str, ids: list[str]) -> str | None:
        from src.synthesis.grounded.passes import _resolve_evidence_source

        sources = [{"article_id": i, "feed_title": "f", "text": "t"} for i in ids]
        return _resolve_evidence_source(index, raw, sources)

    IDS = [
        "rss:https://www.theregister.com/a/5290706",
        "rss:https://gbhackers.com/?p=196463",
        "grok:e576425909d9a9e5:387#2090136982378700811",
    ]

    def test_index_is_preferred(self) -> None:
        assert self._resolve(2, "", self.IDS) == self.IDS[1]

    def test_index_wins_over_corrupted_id(self) -> None:
        # 番号が正しければ id が壊れていても正しい記事に解決する
        assert self._resolve(1, "theregister.com/a/5290706 | The Register", self.IDS) == self.IDS[0]

    def test_out_of_range_index_falls_back_to_id(self) -> None:
        assert self._resolve(99, self.IDS[2], self.IDS) == self.IDS[2]

    def test_missing_index_falls_back_to_id_repair(self) -> None:
        assert self._resolve(None, "gbhackers.com/?p=196463", self.IDS) == self.IDS[1]

    def test_unresolvable_returns_none(self) -> None:
        assert self._resolve(None, "rss:https://never-seen.example/x", self.IDS) is None

    def test_zero_index_is_not_treated_as_first(self) -> None:
        # 1-based。0 は「未指定」であって 1 件目ではない
        assert self._resolve(0, "rss:https://never-seen.example/x", self.IDS) is None

    def test_bare_number_in_article_id_is_treated_as_index(self) -> None:
        """実測: LLM は番号を article_id 側に文字列で入れる ("1")。意図に寄せて解する。"""
        assert self._resolve(None, "2", self.IDS) == self.IDS[1]

    def test_bare_number_out_of_range_is_not_an_index(self) -> None:
        assert self._resolve(None, "99", self.IDS) is None


class TestBothAchPathsUseTheSameDiscipline:
    """ACH は 2 経路ある (初回 ground_and_score / 増分 incremental_ground_and_score)。

    2026-08-22: 初回だけ番号参照へ直し、増分を取り残して本番で
    evidence_article_not_found が出た。**兄弟経路に同じ規律が入っているかを固定する。**
    """

    def test_both_wire_schemas_carry_index(self) -> None:
        from src.synthesis.grounded.incremental import _WireIncEvidence
        from src.synthesis.grounded.passes import _WireEvidence

        for model in (_WireEvidence, _WireIncEvidence):
            assert "index" in model.model_fields, model.__name__
            # 0 = 未指定 (int|None は structured 生成で不安定)
            assert model.model_fields["index"].default == 0

    def test_both_prompts_reference_sources_by_number(self) -> None:
        from pathlib import Path

        for name in ("ground_ach", "ground_incremental"):
            body = Path(f"prompts/synthesis/{name}.j2").read_text(encoding="utf-8")
            assert "[{{ loop.index }}]" in body, name
            # 長い id を列挙すると LLM が行ごと写して破損する
            assert "--- id: {{ s.article_id }}" not in body, name
