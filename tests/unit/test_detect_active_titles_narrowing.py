"""detect へ渡す「既に追跡中の情勢」を候補記事に関係するものだけへ絞る (2026-09-21)。

実測: 一覧は追跡中 147 件すべてで 9,668 tok = detect プロンプトの 63%。台帳が増えると
無制限に伸び、本番は既に 13.6k tok で MLX の壁 (14.1k) に接していた。

一覧の役目は「既に追跡中のものを二重に開かせない」二次的な安全網 — 記事から台帳への
割当は決定論の照合が detect の**前**に済ませており、detect に届くのは割当に漏れた残余。
よって絞りは**割当より緩い重なり**で行う (割当と同じ厳しさにすると全部落ちる)。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from src.assessment.detect_scope import relevant_situation_titles
from src.assessment.situation_store import SituationRow
from src.assessment.stateful import detect_active_titles


def _fs(*values: str) -> frozenset[str]:
    return frozenset(values)


class _Keys:
    """ArticleKeys / SituationKeys の最小代用 (strong / nations / tokens)。"""

    def __init__(
        self,
        strong: frozenset[str] = frozenset(),
        nations: frozenset[str] = frozenset(),
        tokens: frozenset[str] = frozenset(),
    ) -> None:
        self.strong = frozenset(strong)
        self.nations = frozenset(nations)
        self.tokens = frozenset(tokens)


class _Sit(_Keys):
    def __init__(self, title: str, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.row = type("R", (), {"title": title})()


class TestRelevantSituationTitles:
    def test_shared_strong_anchor_keeps_the_situation(self) -> None:
        sits = [_Sit("A の情勢", strong=_fs("actor:lazarus"))]
        arts = [_Keys(strong=_fs("actor:lazarus"))]

        assert relevant_situation_titles(sits, arts) == ["A の情勢"]

    def test_same_nation_plus_one_shared_token_keeps_it(self) -> None:
        sits = [_Sit("B の情勢", nations=_fs("JP"), tokens=_fs("ransomware", "hospital"))]
        arts = [_Keys(nations=_fs("JP"), tokens=_fs("ransomware"))]

        assert relevant_situation_titles(sits, arts) == ["B の情勢"]

    def test_same_nation_alone_is_not_enough(self) -> None:
        """国だけで残すと RU-UA のような高頻度ペアで一覧が縮まない。"""
        sits = [_Sit("C の情勢", nations=_fs("RU"), tokens=_fs("energy"))]
        arts = [_Keys(nations=_fs("RU"), tokens=_fs("phishing"))]

        assert relevant_situation_titles(sits, arts) == []

    def test_unrelated_situation_is_dropped(self) -> None:
        sits = [_Sit("無関係", strong=_fs("actor:apt29"), nations=_fs("RU"), tokens=_fs("embassy"))]
        arts = [_Keys(strong=_fs("cve:cve-2026-1"), nations=_fs("JP"), tokens=_fs("router"))]

        assert relevant_situation_titles(sits, arts) == []

    def test_order_is_preserved_and_titles_are_deduplicated(self) -> None:
        sits = [
            _Sit("同題", strong=_fs("actor:x")),
            _Sit("別題", strong=_fs("actor:x")),
            _Sit("同題", strong=_fs("actor:x")),
        ]
        arts = [_Keys(strong=_fs("actor:x"))]

        assert relevant_situation_titles(sits, arts) == ["同題", "別題"]

    def test_no_candidates_means_no_titles(self) -> None:
        assert relevant_situation_titles([_Sit("A", strong=_fs("actor:x"))], []) == []


class TestProductionWiring:
    """本番の配線 (stateful.detect_active_titles) と rollback 旗。

    判定は埋込へ置き換え済 (2026-09-22)。決定論の純粋関数は rollback 用に残す。
    """

    @staticmethod
    def _row(title: str, status: str = "active") -> SituationRow:
        return SituationRow(
            situation_id=title,
            title=title,
            domain="cyber",
            status=status,  # type: ignore[arg-type]
            anchors=frozenset(),
            pir_ids=(),
            opened_at="2026-09-01T00:00:00+00:00",
            last_evidence_at="2026-09-01T00:00:00+00:00",
        )

    class _Repo:
        def __init__(self, stored: dict[str, list[float]]) -> None:
            self._stored = stored

        def load_summary_embeddings(self, ids: list[str]) -> dict[str, object]:
            import numpy as np

            return {k: np.array(v, dtype=np.float32) for k, v in self._stored.items() if k in ids}

    def _patch_embed(self, monkeypatch: pytest.MonkeyPatch, table: dict[str, list[float]]) -> None:
        import numpy as np

        class _Res:
            def __init__(self, vector: list[float]) -> None:
                self.vector = vector

        class _Client:
            def __init__(self, **_: object) -> None: ...

            async def embed(self, text: str) -> _Res:
                return _Res(table.get(text, [1.0, 0.0]))

        monkeypatch.setattr("src.tools.embedding_client.OllamaEmbeddingClient", _Client)
        monkeypatch.setattr(
            "src.tools.model_tiers.resolve_embedding_model", lambda *a, **k: "fake-embed"
        )
        assert np is not None

    def test_only_situations_close_to_the_candidates_are_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DETECT_SCOPE_NARROW", raising=False)
        self._patch_embed(monkeypatch, {"近い情勢": [1.0, 0.0], "遠い情勢": [0.0, 1.0]})
        repo = self._Repo({"a1": [1.0, 0.0]})
        rows = [self._row("近い情勢"), self._row("遠い情勢"), self._row("休眠", status="dormant")]

        got = asyncio.run(
            detect_active_titles(rows, [{"article_id": "a1", "title": "t"}], cast(Any, repo))
        )

        assert got == ["近い情勢"]

    def test_flag_off_restores_all_active_titles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DETECT_SCOPE_NARROW", "0")
        rows = [self._row("A"), self._row("B")]

        got = asyncio.run(
            detect_active_titles(rows, [{"article_id": "a1", "title": "t"}], cast(Any, None))
        )

        assert got == ["A", "B"]

    def test_embedding_failure_falls_back_to_the_full_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠ 絞りは prompt を縮める最適化。失敗したら **既知の挙動へ倒す** —
        情勢を落として二重開設を招く方が高くつく。"""
        monkeypatch.delenv("DETECT_SCOPE_NARROW", raising=False)

        async def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("embed down")

        monkeypatch.setattr("src.assessment.stateful._detect_scope_vectors", _boom)
        rows = [self._row("A"), self._row("B")]

        got = asyncio.run(
            detect_active_titles(rows, [{"article_id": "a1", "title": "t"}], cast(Any, None))
        )

        assert got == ["A", "B"]
