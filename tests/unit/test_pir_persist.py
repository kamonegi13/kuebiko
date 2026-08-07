"""PIR-persist (rebuild_pir_entities) の永続化テスト。

posted 記事 × enabled PIR を再評価し entity_type='pir' を置換することを検証する。
match logic 自体は evaluator のテストが担保するため、ここでは「match → pir entity 永続化」
の配線と full-replace を確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.pir.models import Pir, PirConfig, StrongSignals
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord


def _seed(repo: RunHistoryRepository, run_id: int, aid: str, title: str) -> None:
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=aid,
            title=title,
            url=f"https://x.example/{aid}",
            category="apt",
            status="posted",
            importance="high",
            summary=title,
            published_at=datetime.now(UTC),
        )
    )


def test_rebuild_persists_matching_pir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = RunHistoryRepository(db_path=tmp_path / "pir.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "m1", "Lazarus targets a bank")  # match keyword
    _seed(repo, run_id, "n1", "Unrelated weather report")  # no match

    cfg = PirConfig(
        priorities=[
            Pir(id="pir_test", title="T", strong_signals=StrongSignals(keywords=["lazarus"])),
            Pir(id="pir_empty", title="E"),  # strong_signals 空 → ノーマッチ (skip)
        ]
    )
    from src.pir import persist

    monkeypatch.setattr(persist, "get_pir_config", lambda *a, **k: cfg)

    stats = persist.rebuild_pir_entities(repo=repo)
    assert stats["pirs"] == 1  # strong_signals 持ちのみ
    assert stats["articles_matched"] == 1
    assert stats["pir_links"] == 1

    ents_m = repo.get_entities_by_article("m1")
    ents_n = repo.get_entities_by_article("n1")
    assert ("pir", "pir_test") in ents_m
    assert all(t != "pir" for t, _ in ents_n)


def test_rebuild_full_replace_removes_stale(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    repo = RunHistoryRepository(db_path=tmp_path / "pir2.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "a1", "Lazarus attack")
    # 既存の stale な pir entity を仕込む (前回 rebuild の残骸を模す)
    repo.add_article_entities("a1", [("pir", "pir_old")])

    ss = StrongSignals(keywords=["lazarus"])
    cfg = PirConfig(priorities=[Pir(id="pir_new", title="N", strong_signals=ss)])
    from src.pir import persist

    monkeypatch.setattr(persist, "get_pir_config", lambda *a, **k: cfg)
    persist.rebuild_pir_entities(repo=repo)

    ents = repo.get_entities_by_article("a1")
    assert ("pir", "pir_new") in ents
    assert ("pir", "pir_old") not in ents  # full replace で stale 削除
