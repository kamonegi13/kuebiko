"""subject-gate SQL フラグメントの単体テスト (2026-07-27, 案A / §5)。

評価済み行は主題メンバーシップに絞り、legacy 行 (subject_actor_source NULL) は mention
fallback することを、実 DB (SQLite、INSTR) で検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.cti.subject_gate import (
    passes_subject_gate,
    subject_gate_clause,
    subject_membership_clause,
)
from src.storage.records import ArticleRecord, RunRecord
from src.storage.run_history import RunHistoryRepository


def _repo(tmp_path: Path) -> RunHistoryRepository:
    return RunHistoryRepository(db_path=str(tmp_path / "gate.db"))


def _add(
    repo: RunHistoryRepository,
    aid: str,
    *,
    subject_ids: str | None,
    subject_source: str | None,
    mentions: list[str],
) -> None:
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id=aid,
            title="t",
            url=f"https://ex.com/{aid}",
            status="posted",
            subject_actor_ids=subject_ids,
            subject_actor_source=subject_source,
        )
    )
    repo.add_article_entities(aid, [("actor", m) for m in mentions])


def _gated_actors(repo: RunHistoryRepository) -> set[str]:
    gate = subject_gate_clause(
        mention_col="ae.value",
        subject_ids_col="a.subject_actor_ids",
        subject_source_col="a.subject_actor_source",
    )
    with repo._connect() as con:  # noqa: SLF001
        rows = con.execute(
            "SELECT ae.value AS v FROM article_entities ae "
            "JOIN articles a ON a.article_id = ae.article_id "
            f"WHERE ae.entity_type='actor' AND {gate}"
        ).fetchall()
    return {str(r["v"]) for r in rows}


def test_evaluated_keeps_only_subject(tmp_path: Path) -> None:
    """評価済み行: 主題 (qilin) は残り、言及のみ (play) は落ちる。"""
    repo = _repo(tmp_path)
    _add(
        repo,
        "digest",
        subject_ids="qilin",
        subject_source="llm",
        mentions=["qilin", "play", "everest"],
    )
    got = _gated_actors(repo)
    assert "qilin" in got
    assert "play" not in got
    assert "everest" not in got


def test_legacy_falls_back_to_mention(tmp_path: Path) -> None:
    """legacy 行 (subject_actor_source NULL): 主題層以前なので全 mention を残す (fallback)。"""
    repo = _repo(tmp_path)
    _add(repo, "old", subject_ids=None, subject_source=None, mentions=["apt28", "lazarus"])
    got = _gated_actors(repo)
    assert got == {"apt28", "lazarus"}


def test_evaluated_no_subject_drops_all(tmp_path: Path) -> None:
    """評価済み・主題なし (source='none', ids=''): 全 mention を落とす (NSA advisory 型)。"""
    repo = _repo(tmp_path)
    _add(repo, "adv", subject_ids="", subject_source="none", mentions=["us_nsa"])
    got = _gated_actors(repo)
    assert got == set()


def test_multi_subject_membership_is_exact(tmp_path: Path) -> None:
    """複数主題 + 部分文字列の誤マッチ防止 (akira は akira_ransom にマッチしない)。"""
    repo = _repo(tmp_path)
    _add(
        repo,
        "multi",
        subject_ids="the_gentlemen,inc_ransom",
        subject_source="feed",
        mentions=["the_gentlemen", "inc_ransom", "akira"],
    )
    got = _gated_actors(repo)
    assert got == {"the_gentlemen", "inc_ransom"}  # akira は部分一致せず落ちる


# ---------- passes_subject_gate (Python 版、text-regex 抽出経路用) ----------
# SQL 版 subject_gate_clause と同一意味論であることを同一シナリオで検証する。


def test_passes_gate_evaluated_keeps_only_subject() -> None:
    assert passes_subject_gate(mention_id="qilin", subject_ids_csv="qilin", subject_source="llm")
    assert not passes_subject_gate(mention_id="play", subject_ids_csv="qilin", subject_source="llm")


def test_passes_gate_legacy_allows_any_mention() -> None:
    # subject_source が None/'' → legacy fallback、全 mention を通す
    assert passes_subject_gate(mention_id="apt28", subject_ids_csv=None, subject_source=None)
    assert passes_subject_gate(mention_id="lazarus", subject_ids_csv=None, subject_source="")


def test_passes_gate_evaluated_no_subject_drops_all() -> None:
    # 評価済み・主題なし (source='none', ids='') → 全 mention を落とす (NSA advisory 型)
    assert not passes_subject_gate(mention_id="us_nsa", subject_ids_csv="", subject_source="none")


def test_passes_gate_membership_is_exact() -> None:
    # 部分文字列の誤マッチ防止: akira は akira_ransom のメンバーではない
    assert not passes_subject_gate(
        mention_id="akira", subject_ids_csv="akira_ransom,the_gentlemen", subject_source="feed"
    )
    assert passes_subject_gate(
        mention_id="the_gentlemen",
        subject_ids_csv="akira_ransom,the_gentlemen",
        subject_source="feed",
    )


# ---------- subject_membership_clause (2026-08-21、地図 flow / 概況で共用) ----------


def test_subject_membership_clause_matches_situation_hand_rolled_form() -> None:
    """situation.py._attacker_face_rows の置換前の手書き OR 連結と文字列一致する (挙動不変の
    リファクタであることの回帰防止)。"""
    ids = ["volt_typhoon", "lazarus", "salt_typhoon"]
    expected_membership = " OR ".join(
        "INSTR(',' || COALESCE(subject_actor_ids,'') || ',', ',' || ? || ',') > 0" for _ in ids
    )
    assert subject_membership_clause("subject_actor_ids", len(ids)) == f"({expected_membership})"


def test_subject_membership_clause_single_param_respects_column_prefix() -> None:
    assert subject_membership_clause("a.subject_actor_ids", 1) == (
        "(INSTR(',' || COALESCE(a.subject_actor_ids,'') || ',', ',' || ? || ',') > 0)"
    )


def test_subject_membership_clause_rejects_zero_param_count() -> None:
    # 空候補での呼び出しは呼出側の guard 漏れなので早期に落とす。
    with pytest.raises(ValueError, match="param_count"):
        subject_membership_clause("subject_actor_ids", 0)


def test_subject_membership_clause_exact_match_via_sqlite(tmp_path: Path) -> None:
    """実 SQLite 上で position ベースの完全一致 (部分文字列誤マッチ防止) を検証する。"""
    repo = _repo(tmp_path)
    rid = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=False))
    repo.add_article(
        ArticleRecord(
            run_id=rid,
            article_id="a1",
            title="t",
            url="https://ex.com/a1",
            status="posted",
            subject_actor_ids="the_gentlemen,inc_ransom",
        )
    )
    clause = subject_membership_clause("subject_actor_ids", 2)
    with repo._connect() as con:  # noqa: SLF001
        rows = con.execute(
            f"SELECT article_id FROM articles WHERE {clause}",  # noqa: S608 — clause は内部固定
            ("akira", "inc_ransom"),
        ).fetchall()
    assert [r["article_id"] for r in rows] == ["a1"]  # akira は部分一致せず落ちる
