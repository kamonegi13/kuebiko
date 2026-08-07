"""本文完全性の書込 seam 不変条件 (2026-07-27)。

docs/body_extraction_and_entity_integrity_redesign.md §2.1 の不変条件を
**慣習でなくテストで強制**する:

  body が非 NULL/非空で永続化されるなら body_source も非 NULL である。

背景 = 全文取得が 403 で無音に feed 抜粋へ fallback し、切り株の上で全下流が
実行されていた (GBHackers 953/953=100%)。fallback の痕跡が DB に一切残らず、
どの監査指標にも現れなかったのが病理。body_source を「body と同じ書込点で必ず
立てる」ことを構造的に保証する。

ルール:
1. articles.body への書込は ``update_article_body`` (唯一の seam) 経由のみ。
   raw SQL で ``body=`` を直接更新する箇所を CI で検知する。
2. production の全 body 書込は ``source=`` を明示する (source=None の暗黙 fallback は
   test 専用)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from src.storage.run_history import RunHistoryRepository

_SRC = Path(__file__).resolve().parents[2] / "src"

# articles.body を raw SQL で書いてよいファイル (repo-relative)。増やす前に docstring を読むこと。
_ALLOWED_BODY_WRITERS = {
    "storage/repo_articles.py",  # update_article_body — 本文書込の唯一の seam
    # purge_article_bodies_older_than — body=NULL 化 (body_source='none' も整合)
    "storage/repo_dedup.py",
}


def _body_writers_in_src() -> set[str]:
    out: set[str] = set()
    for path in _SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # `SET ... body=` の代入形のみ (body_source= 等の別列代入・比較は除外)
        if re.search(r"SET\s+body\s*=", text, re.IGNORECASE):
            out.add(path.relative_to(_SRC).as_posix())
    return out


def test_only_seam_writes_article_body() -> None:
    """articles.body の raw SQL 書込点が seam に集約されている (迂回 writer の検知)。"""
    found = _body_writers_in_src()
    unexpected = found - _ALLOWED_BODY_WRITERS
    assert not unexpected, (
        "articles.body を raw SQL で直接更新する経路が検出されました。"
        "update_article_body (source 必須の seam) を経由させ、確認後 _ALLOWED_BODY_WRITERS を"
        f"更新してください: {sorted(unexpected)}"
    )


def _make_repo(tmp_path: Path) -> RunHistoryRepository:
    repo = RunHistoryRepository(db_path=str(tmp_path / "seam.db"))
    return repo


def _seed_article(repo: RunHistoryRepository, article_id: str) -> None:
    from src.storage.records import ArticleRecord, RunRecord

    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="t",
            url="https://example.com/a",
            status="posted",
        )
    )


def test_update_article_body_sets_source(tmp_path: Path) -> None:
    """source を渡した body 書込は body_source 列を確実に埋める (不変条件の実挙動)。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "seam:1")
    repo.update_article_body("seam:1", "full body text", source="full_extract")

    rec = repo.get_article("seam:1")
    assert rec is not None
    assert rec.body_source == "full_extract"
    assert rec.extraction_failure_reason is None


def test_update_article_body_records_failure_reason(tmp_path: Path) -> None:
    """feed 抜粋 fallback は body_source='feed_summary' + 失敗理由を残す (無音 fallback の排除)。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "seam:2")
    repo.update_article_body(
        "seam:2", "short feed summary", source="feed_summary", failure_reason="http_403"
    )

    rec = repo.get_article("seam:2")
    assert rec is not None
    assert rec.body_source == "feed_summary"
    assert rec.extraction_failure_reason == "http_403"


# ---------- B3a: 抽出失敗 (msg=None) の可視化 ----------


def test_extraction_failure_state_mapping() -> None:
    """top-level failure_reason → body_source 失敗状態の導出 (抽出失敗のみ)。"""
    from src.pipeline.persistence import _extraction_failure_state

    assert _extraction_failure_state("degenerate_body:block_page") == ("blocked", "block_page")
    assert _extraction_failure_state("degenerate_body:body_too_short:0") == (
        "pending_refetch",
        "body_too_short",
    )
    # 抽出失敗でない/無関係な理由は None (body_source を触らない)
    assert _extraction_failure_state("summarize_failed") is None
    assert _extraction_failure_state("") is None


def test_extraction_failure_state_parses_orchestrator_format() -> None:
    """orchestrator が実際に書く書式 (コロン後スペース入り) をそのまま解せる。

    監査 2026-08-01: 書き手 `f"degenerate_body: {reason}"` (スペース入り) と読み手
    `startswith("degenerate_body:block_page")` (スペース無し) の照合不一致で、この
    救済経路が production で 100% デッドコード化していた (初回 extract_failed の
    body_source/reason が必ず NULL)。パーサ側の正規化で書式再変更にも耐える。
    """
    from src.pipeline.persistence import _extraction_failure_state

    assert _extraction_failure_state("degenerate_body: block_page") == ("blocked", "block_page")
    assert _extraction_failure_state("degenerate_body: body_too_short:42") == (
        "pending_refetch",
        "body_too_short",
    )


def test_degenerate_body_error_carries_extraction_reason() -> None:
    """HTTP 層の失敗理由 (http_error_403/js_challenge/timeout) を初回失敗まで運ぶ。

    従来は _resolve_body が返した extraction_failure_reason が raise で破棄され、
    「取得block / パース不能 / 閾値棄却」の 3 切り分けが DB 上で検証不能だった。
    """
    from src.pipeline.briefing import DegenerateBodyError

    e = DegenerateBodyError("block_page", extraction_reason="http_error_403")
    assert e.reason == "block_page"
    assert e.extraction_reason == "http_error_403"
    # 後方互換: 理由なし構築も可
    assert DegenerateBodyError("block_page").extraction_reason is None


def test_mark_extraction_state_visualizes_null_body(tmp_path: Path) -> None:
    """body NULL の行に body_source + reason を記録し、不可視の抽出失敗を検出可能にする。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "seam:3")  # body は NULL のまま

    n = repo.mark_extraction_state("seam:3", body_source="blocked", failure_reason="block_page")
    assert n == 1

    rec = repo.get_article("seam:3")
    assert rec is not None
    assert rec.body_source == "blocked"
    assert rec.extraction_failure_reason == "block_page"
    # body 自体は触らない (NULL のまま) — DB 直引きで確認
    with repo._connect() as conn:
        row = conn.execute("SELECT body FROM articles WHERE article_id=?", ("seam:3",)).fetchone()
    assert row["body"] is None


def test_mark_extraction_state_does_not_overwrite_existing_body(tmp_path: Path) -> None:
    """既存本文がある行の body_source は上書きしない (WHERE body IS NULL ガード)。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "seam:4")
    repo.update_article_body("seam:4", "real full body", source="full_extract")

    n = repo.mark_extraction_state("seam:4", body_source="blocked", failure_reason="block_page")
    assert n == 0  # body 非 NULL のため更新されない

    rec = repo.get_article("seam:4")
    assert rec is not None
    assert rec.body_source == "full_extract"  # 元のまま
    assert rec.extraction_failure_reason is None


# ---------- B3b: リトライ cap + キュー修正 ----------


def _seed_article_ft(repo: RunHistoryRepository, article_id: str, feed_title: str) -> None:
    from src.storage.records import ArticleRecord, RunRecord

    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=run_id,
            article_id=article_id,
            title="t",
            url=f"https://example.com/{article_id}",
            feed_title=feed_title,
            status="posted",
        )
    )


def test_record_refetch_failure_increments(tmp_path: Path) -> None:
    """再取得失敗で refetch_attempts が +1 され reason が記録される。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "rf:1")

    assert repo.record_refetch_failure("rf:1", reason="http_error_403") == 1
    rec = repo.get_article("rf:1")
    assert rec is not None
    assert rec.refetch_attempts == 1
    assert rec.extraction_failure_reason == "http_error_403"

    repo.record_refetch_failure("rf:1", reason="timeout")
    rec2 = repo.get_article("rf:1")
    assert rec2 is not None
    assert rec2.refetch_attempts == 2


def test_refetch_queue_caps_by_attempts(tmp_path: Path) -> None:
    """refetch_attempts が max_attempts に達した記事はキューから除外される (無限リトライ停止)。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "cap:1")

    ids = [a for a, _ in repo.list_articles_needing_refetch(max_attempts=3)]
    assert "cap:1" in ids  # attempts=0 → 対象

    for _ in range(3):
        repo.record_refetch_failure("cap:1", reason="timeout")
    ids2 = [a for a, _ in repo.list_articles_needing_refetch(max_attempts=3)]
    assert "cap:1" not in ids2  # attempts=3 → 除外


def test_refetch_queue_excludes_ransomware_case_insensitive(tmp_path: Path) -> None:
    """'ransomware.live' (小文字) が case-insensitive で除外される (旧 case 不一致バグの修正)。"""
    repo = _make_repo(tmp_path)
    _seed_article_ft(repo, "rw:1", "ransomware.live")
    _seed_article_ft(repo, "bc:1", "BleepingComputer")

    ids = [a for a, _ in repo.list_articles_needing_refetch()]
    assert "rw:1" not in ids
    assert "bc:1" in ids


def test_refetch_queue_excludes_blocked(tmp_path: Path) -> None:
    """body_source='blocked' (WAF/bot 壁) は body NULL でも再取得キューから除外される。"""
    repo = _make_repo(tmp_path)
    _seed_article(repo, "blk:1")
    repo.mark_extraction_state("blk:1", body_source="blocked", failure_reason="block_page")

    ids = [a for a, _ in repo.list_articles_needing_refetch()]
    assert "blk:1" not in ids
