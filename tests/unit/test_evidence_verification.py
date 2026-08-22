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
