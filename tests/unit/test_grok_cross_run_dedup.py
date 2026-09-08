"""Grok sub-article の run 横断 URL dedup (2026-09-08)。

背景: 親レポート URL は毎回新規のため fetch 段の未見選別 (seen_hash_filter) を
素通りし、別レポートに含まれる同一 tweet が run を跨いで別 sub-article 化していた
(利用者発見。30 日実測: X 投稿 191 URL 重複 / 余剰 209 記事。dedup_seen_urls は
tweet URL を seen_count=3 で記録しながら、展開経路に照会する関門が無かった)。

展開直後に ledger を照会して既知 tweet を落とす純粋関数を固定する。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.pipeline.grok_convert import filter_expanded_by_seen
from src.tools.discord_publisher import BriefingMessage
from src.tools.url_normalizer import url_hash

_URL_A = "https://x.com/someactor/status/1111"
_URL_B = "https://x.com/someactor/status/2222"


def _msg(dedup_key: str) -> BriefingMessage:
    return BriefingMessage(
        title="t",
        summary="s",
        importance="medium",
        category="apt",
        metadata={"dedup_key": dedup_key},
    )


class _FakeSeenFilter:
    """dedup_repo.filter_seen_and_touch の代役 (seen 集合を返す)。"""

    def __init__(self, seen_urls: list[str]) -> None:
        self._seen = {url_hash(u) for u in seen_urls}
        self.calls: list[Sequence[str]] = []

    def __call__(self, hashes: Sequence[str]) -> set[str]:
        self.calls.append(hashes)
        return {h for h in hashes if h in self._seen}


def test_known_tweet_is_dropped_unknown_kept() -> None:
    seen = _FakeSeenFilter([_URL_A])
    kept, dropped = filter_expanded_by_seen([_msg(_URL_A), _msg(_URL_B)], seen)
    assert dropped == 1
    assert [m.metadata["dedup_key"] for m in kept] == [_URL_B]


def test_no_filter_passes_through() -> None:
    msgs = [_msg(_URL_A)]
    kept, dropped = filter_expanded_by_seen(msgs, None)
    assert kept == msgs
    assert dropped == 0


def test_message_without_key_is_kept() -> None:
    # dedup_key 無しの msg は落とさない (fail-open — 落とし過ぎは収集喪失)
    msg = BriefingMessage(title="t", summary="s", importance="medium", category="apt")
    kept, dropped = filter_expanded_by_seen([msg], _FakeSeenFilter([_URL_A]))
    assert kept == [msg]
    assert dropped == 0
