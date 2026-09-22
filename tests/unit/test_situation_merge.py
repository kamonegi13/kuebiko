"""情勢どうしの統合 (2026-09-22)。

規則 (`match_claim`) は誤って繋ぐ一方で、**本当の重複は取りこぼしていた**。情勢どうしを
埋込で総当たりし Opus に判定させると、余弦 0.75 以上は 3/3、0.65-0.75 は 6/12 が
「統合すべき」で、実際に台帳が二重に持つ事案が 11 組見つかった (Claude/OpenAI の
リポジトリアクセスが余弦 0.997 で 2 件、MikroTik RouterOS が 0.882 で 2 件 等)。

⭐ 統合は **redirect + 墓標** (アクター辞書で確立した規約): id は不変、吸収された側は
``merged_into`` を持ち全経路から外れる。closed にするだけでは不十分 — ``open_situation``
は同一 title ハッシュの行を status を問わず返すので、同じ題名の claim が来ると
**吸収された側が復活する** (ゾンビ経路、2026-07-05 に同型の障害)。
"""

from __future__ import annotations

from pathlib import Path

from src.assessment.situation_store import SituationStore


def _store(tmp_path: Path) -> SituationStore:
    from src.storage.run_history import RunHistoryRepository

    db = tmp_path / "t.db"
    RunHistoryRepository(db_path=db)
    return SituationStore(db_path=db)


_NOW = "2026-09-22T00:00:00+00:00"


def _open(store: SituationStore, title: str) -> str:
    row = store.open_situation(
        title=title,
        domain="cyber",
        anchors=frozenset({"actor:x"}),
        pir_ids=(),
        now_iso="2026-09-01T00:00:00+00:00",
    )
    return row.situation_id


class TestMergeSituations:
    def test_absorbed_situation_disappears_from_every_reader(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        keep, dup = _open(store, "残る情勢"), _open(store, "消える情勢")

        store.merge_situation(dup_id=dup, into_id=keep, basis="埋込 0.99", now_iso=_NOW)

        ids = {r.situation_id for r in store.load_situations(("active", "dormant", "closed"))}
        assert keep in ids and dup not in ids

    def test_evidence_moves_to_the_surviving_situation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        keep, dup = _open(store, "残る情勢"), _open(store, "消える情勢")
        for sid, aid in ((keep, "a1"), (dup, "a2"), (dup, "a3")):
            store.record_assignment(
                situation_id=sid,
                article_id=aid,
                added_at="2026-09-01T00:00:00+00:00",
                assigned_by="anchor",
            )

        store.merge_situation(dup_id=dup, into_id=keep, basis="b", now_iso=_NOW)

        assert store.evidence_ids_by_situation([keep])[keep] == {"a1", "a2", "a3"}

    def test_merge_is_recorded_as_a_relation(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        keep, dup = _open(store, "残る情勢"), _open(store, "消える情勢")

        store.merge_situation(dup_id=dup, into_id=keep, basis="埋込 0.99", now_iso=_NOW)

        rels = store.relations_for([keep, dup])
        assert any(r["rel_type"] == "merged_into" for r in rels)

    def test_reopening_the_absorbed_title_redirects_to_the_survivor(self, tmp_path: Path) -> None:
        """⭐ ゾンビ経路の封じ: 同じ題名で開き直しても吸収先が返る。"""
        store = _store(tmp_path)
        keep, dup = _open(store, "残る情勢"), _open(store, "消える情勢")
        store.merge_situation(dup_id=dup, into_id=keep, basis="b", now_iso=_NOW)

        again = store.open_situation(
            title="消える情勢",
            domain="cyber",
            anchors=frozenset(),
            pir_ids=(),
            now_iso="2026-09-23T00:00:00+00:00",
        )

        assert again.situation_id == keep

    def test_merging_into_itself_is_rejected(self, tmp_path: Path) -> None:
        import pytest

        store = _store(tmp_path)
        sid = _open(store, "情勢")

        with pytest.raises(ValueError):
            store.merge_situation(dup_id=sid, into_id=sid, basis="b", now_iso=_NOW)
