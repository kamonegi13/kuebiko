"""シャドー観測の不変条件。

⭐ **本番の挙動を変えない**ことが最重要。観測は群化の後に走り、失敗しても
群化を止めない。⚠ 絞り込みは本番の前段の関門と同じ条件でなければ、観測した比較が
本番の比較にならない (2026-08-24 に評価と本番で取得が分かれて挙動が一致しなかった)。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np

from src.eventnews.models import MemberArticle
from src.eventnews.pair_shadow import _MAX_PAIRS, is_enabled, select_pairs


def _member(
    aid: str, *, entities: set[tuple[str, str]] | None = None, hours: float = 0.0
) -> MemberArticle:
    return MemberArticle(
        article_id=aid,
        title=f"見出し {aid}",
        url=f"https://example.test/{aid}",
        feed_title="媒体",
        feed_url="https://example.test/feed",
        host="example.test",
        importance="high",
        category="breach",
        status="posted",
        anchor_ts=datetime(2026, 8, 31, tzinfo=UTC) + timedelta(hours=hours),
        summary="要約",
        body="本文",
        entities=frozenset(entities or set()),
    )


_VEC = {
    "a": np.asarray([1.0, 0.0], dtype=np.float32),
    "b": np.asarray([1.0, 0.0], dtype=np.float32),
}


def test_disabled_by_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """既定は off — LLM 呼出を伴うので明示的に開ける。"""
    monkeypatch.delenv("EVENTNEWS_PAIR_SHADOW", raising=False)
    assert not is_enabled()
    monkeypatch.setenv("EVENTNEWS_PAIR_SHADOW", "1")
    assert is_enabled()


def test_pairs_need_a_concrete_shared_name() -> None:
    """アクター名だけの共有では観測しない (本番の関門と同じ条件)。"""
    ents = {("actor", "play")}
    pairs = select_pairs([_member("a", entities=ents)], [_member("b", entities=ents)], _VEC)
    assert pairs == []

    ents2 = {("actor", "play"), ("victim_org", "alpha")}
    pairs = select_pairs([_member("a", entities=ents2)], [_member("b", entities=ents2)], _VEC)
    assert len(pairs) == 1


def test_pairs_outside_the_window_are_skipped() -> None:
    ents = {("cve", "CVE-2026-1")}
    far = _member("b", entities=ents, hours=400)
    assert select_pairs([_member("a", entities=ents)], [far], _VEC) == []


def test_articles_without_a_vector_are_skipped() -> None:
    """埋込が無い記事は特徴量が作れない (欠測を 0 で埋めない)。"""
    ents = {("cve", "CVE-2026-1")}
    pairs = select_pairs(
        [_member("a", entities=ents)], [_member("b", entities=ents)], {"a": _VEC["a"]}
    )
    assert pairs == []


def test_pair_count_is_capped() -> None:
    """暴発を抑える。実測 p90 は 41 組なので通常は掛からない。"""
    ents = {("cve", "CVE-2026-1")}
    cands = [_member(f"c{i}", entities=ents) for i in range(30)]
    members = [_member(f"m{i}", entities=ents) for i in range(30)]
    vec = {m.article_id: np.asarray([1.0, 0.0], dtype=np.float32) for m in cands + members}
    assert len(select_pairs(cands, members, vec)) == _MAX_PAIRS
