"""定期の発表の別の号を hard の重複判定から外す判定 (AT-4、DEDUP_PERIODIC_EXEMPT) の unit test。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.pipeline.dedup_periodic import periodic_exempt_reason, periodic_mode
from src.pipeline.filters import _filter_semantic_duplicates
from src.tools.article_model import Article
from src.tools.embedding_client import EmbeddingResponse


@pytest.mark.parametrize(
    ("title_a", "title_b"),
    [
        ("Iran Update, October 9, 2026", "Iran Update, October 8, 2026"),
        (
            "Multiples vulnérabilités dans les produits IBM (16 octobre 2026)",
            "Multiples vulnérabilités dans les produits IBM (09 octobre 2026)",
        ),
        (
            "JVN: CISA ICS Advisory（2026年10月06日）",
            "JVN: CISA ICS Advisory（2026年10月01日）",
        ),
        ("Weekly Digest Issue #12", "Weekly Digest Issue #11"),
        ("Threat Trends Report 2026年9月", "Threat Trends Report 2026年10月"),
    ],
)
def test_different_issue_is_detected(title_a: str, title_b: str) -> None:
    assert periodic_exempt_reason(title_a, title_b) is not None


@pytest.mark.parametrize(
    ("title_a", "title_b"),
    [
        # 同じ号の再投稿
        ("Iran Update, October 9, 2026", "Iran Update, October 9, 2026"),
        # 片方に鍵が無い
        ("Russia hits port infrastructure", "Russia hits port infrastructure, October 9"),
        ("Russian Forces Continue Strikes on Ukrainian Port", "Russia Continues Strikes on Port"),
        # 骨格が違う (日付だけ違っても別の話題)
        ("Acme breached on October 9", "Globex ransomware attack on October 8"),
        ("", ""),
    ],
)
def test_same_issue_or_non_periodic_is_not_detected(title_a: str, title_b: str) -> None:
    assert periodic_exempt_reason(title_a, title_b) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, "0"), ("0", "0"), ("shadow", "shadow"), ("1", "1"), ("junk", "0")],
)
def test_mode_resolves_three_values(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: str
) -> None:
    if raw is None:
        monkeypatch.delenv("DEDUP_PERIODIC_EXEMPT", raising=False)
    else:
        monkeypatch.setenv("DEDUP_PERIODIC_EXEMPT", raw)
    assert periodic_mode() == expected


class _Embedder:
    async def embed(self, text: str) -> EmbeddingResponse:
        return EmbeddingResponse(vector=(1.0, 0.0), model="m", dim=2)


class _Repo:
    """hard / cluster とも常に一致 (cos 0.99)、一致先の見出しを返す。"""

    def __init__(self, matched_title: str) -> None:
        self._title = matched_title

    def find_similar_embedding(self, *args: Any, **kwargs: Any) -> tuple[str, float]:
        return ("hash1", 0.99)

    def get_seen_url(self, url_hash: str) -> tuple[str, datetime]:
        return (self._title, datetime(2026, 10, 8, tzinfo=UTC))


def _article(title: str) -> Article:
    return Article(
        id="a1",
        title=title,
        url="https://x.example/1",
        summary_html="<p>body</p>",
        author="A",
        published=datetime(2026, 10, 9, tzinfo=UTC),
        feed_title="Feed",
        feed_url="https://x.example/feed",
    )


async def _run(title: str, matched: str) -> tuple[list[Article], list[Any]]:
    survivors, _n, _emb, _ids, records = await _filter_semantic_duplicates(
        [_article(title)],
        _Repo(matched),  # type: ignore[arg-type]
        _Embedder(),  # type: ignore[arg-type]
        threshold_hard=0.92,
        threshold_cluster=0.78,
        window_hours_hard=168,
        window_hours_cluster=48,
    )
    return survivors, records


async def test_flag_off_still_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_PERIODIC_EXEMPT", "0")
    survivors, records = await _run("Iran Update, October 9, 2026", "Iran Update, October 8, 2026")
    assert survivors == []
    assert records[0].tier == "hard"


async def test_shadow_logs_but_still_drops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_PERIODIC_EXEMPT", "shadow")
    survivors, records = await _run("Iran Update, October 9, 2026", "Iran Update, October 8, 2026")
    assert survivors == []
    assert records[0].tier == "hard"


async def test_on_keeps_different_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_PERIODIC_EXEMPT", "1")
    survivors, records = await _run("Iran Update, October 9, 2026", "Iran Update, October 8, 2026")
    assert [a.id for a in survivors] == ["a1"]
    assert records == []


async def test_on_still_drops_same_issue_reprint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUP_PERIODIC_EXEMPT", "1")
    survivors, records = await _run("Iran Update, October 9, 2026", "Iran Update, October 9, 2026")
    assert survivors == []
    assert records[0].tier == "hard"
