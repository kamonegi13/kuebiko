from __future__ import annotations

from scripts.backfill_publication_event_basis import render_preview, select_targets


def _row(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 1,
        "title": "t",
        "feed_title": "News",
        "feed_url": "",
        "category": "breach",
        "article_type": "breaking",
        "account_class": None,
        "event_date": "2026-10-01",
    }
    return {**base, **kw}


def test_select_targets_picks_publication_rows_only() -> None:
    rows = [_row(id=1), _row(id=2, article_type="advisory"), _row(id=3, category="apt_leak")]
    got = select_targets(rows)
    assert [(r["id"], r["kind"]) for r in got] == [(2, "advisory"), (3, "leak_site")]
    assert "kind" not in rows[1]  # 入力は変更しない


def test_render_preview_has_headline_and_no_body() -> None:
    out = render_preview(select_targets([_row(article_type="advisory", title="A | B")]), 5)
    assert "A / B" in out and "reported → disclosed" in out
