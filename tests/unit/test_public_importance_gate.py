"""公開面に出す資格 (重要度ごとの条件) を固定する。

2026-08-31 に medium を開放した。**high は単独報でも出す / medium は統合記事が
生成済みかつ独立 2 媒体以上のときだけ出す** という非対称は意図したもので、
- medium を素通しすると 1 日 24 件 → 138 件になり、増えた分の 9 割が単独報
- 生成済み medium は 84% が複数媒体で、生成済み high (24%) より条件を満たす

⚠ 一覧 (SQL) と詳細 (Python) で条件が分かれると、**一覧に出ないものが直リンク
では読める**。両方が同じ ``ImportanceRule`` から導かれていることを検査する。
"""

from __future__ import annotations

from pathlib import Path

from src.storage.repo_eventnews import ImportanceRule
from src.ui.api.public_news import (
    _PUBLIC_IMPORTANCE_RULES,
    _PUBLIC_IMPORTANCES,
    _is_public,
)


def _rule(importance: str) -> ImportanceRule:
    return _PUBLIC_IMPORTANCE_RULES.get(importance, ImportanceRule())


def test_high_is_published_even_when_single_source() -> None:
    """案 A: high は「重要だから 1 媒体でも知らせる」判断が既に働いている。"""
    assert "high" in _PUBLIC_IMPORTANCES
    assert _rule("high").is_open
    assert _rule("high").allows(independent_sources=1, has_news=False)


def test_medium_requires_generated_news_and_two_sources() -> None:
    assert "medium" in _PUBLIC_IMPORTANCES
    rule = _rule("medium")
    assert rule.allows(independent_sources=2, has_news=True)
    assert not rule.allows(independent_sources=1, has_news=True), "単独報の medium は出さない"
    assert not rule.allows(independent_sources=2, has_news=False), "未生成の medium は出さない"


def test_low_is_never_published() -> None:
    """low は 500 件/週あって複数媒体は 0 件。開けると要約の流し込みになる。"""
    assert "low" not in _PUBLIC_IMPORTANCES


def test_sql_and_python_gates_return_the_same_set(tmp_path: Path) -> None:
    """**一覧 (SQL) と詳細 (Python) が同じ集合を選ぶ。**

    片方だけ緩いと「一覧に出ないものが直リンクでは読める」状態になる。
    実 DB に 6 通りを入れて、クエリの結果と述語の結果を突き合わせる
    (述語どうしを比べても同義反復にしかならない)。
    """
    from datetime import UTC, datetime, timedelta

    from src.storage.run_history import RunHistoryRepository

    repo = RunHistoryRepository(db_path=tmp_path / "gate.db")
    base = datetime(2026, 8, 1, tzinfo=UTC)
    cases = [
        ("ev-h-single", "high", 1, False),
        ("ev-h-multi", "high", 3, True),
        ("ev-m-single", "medium", 1, True),
        ("ev-m-nonews", "medium", 2, False),
        ("ev-m-ok", "medium", 2, True),
        ("ev-l-multi", "low", 5, True),
    ]
    for n, (item_id, importance, sources, has_news) in enumerate(cases):
        repo.create_event_item(
            item_id=item_id,
            origin="live",
            first_reported_at=base + timedelta(hours=n),
            last_reported_at=base + timedelta(hours=n),
            importance=importance,
            independent_sources=sources,
        )
        if has_news:
            repo.record_event_version(
                item_id=item_id,
                version=1,
                generated_at=base + timedelta(hours=n),
                model="test",
                prompt_version="eventnews-v6",
                headline=f"{item_id} の見出し",
                body_json='{"facts": []}',
                new_facts_json="[]",
                verified_at=None,
                dropped_lines=0,
                repaired_ids=0,
            )
            # 生成の有無の SSoT は current_version (版の記録とは別責務)
            repo.update_event_item(item_id, {"current_version": 1})

    from_sql = {
        r.state.item_id
        for r in repo.list_event_items(
            origin="live",
            importances=list(_PUBLIC_IMPORTANCES),
            importance_rules=_PUBLIC_IMPORTANCE_RULES,
            limit=50,
        )
    }
    from_python = {
        item_id
        for item_id, _imp, _src, _has_news in cases
        if _is_public(repo.get_event_item(item_id))  # type: ignore[arg-type]
    }

    assert from_sql == from_python
    assert from_sql == {"ev-h-single", "ev-h-multi", "ev-m-ok"}
