"""概念 PIR の LLM 主題判定 (llm_judge + verdict 永続化 + rebuild 合成) の unit テスト。

docs/pir_concept_llm_judge_design.md §4-5。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from src.pir.llm_judge import (
    JudgeVerdict,
    build_judge_prompt,
    judge_hourly,
    judge_pending,
    pir_judge_rev,
)
from src.pir.models import LlmJudgeConfig, Pir, PirConfig, StrongSignals
from src.storage.run_history import ArticleRecord, RunHistoryRepository, RunRecord
from src.tools.llm_client import LLMClient

_GATE = {"property": "text", "op": "keyword_any", "value": ["i-Soon", "leak"]}


def _judge_pir(*, question: str = "", description: str = "APT 実態の内部漏洩") -> Pir:
    return Pir(
        id="pir_apt_leak",
        title="APT 内部情報漏洩",
        description=description,
        match=_GATE,
        llm_judge=LlmJudgeConfig(enabled=True, question=question),
    )


def _seed(
    repo: RunHistoryRepository,
    run_id: int,
    aid: str,
    title: str,
    *,
    body: str = "",
) -> None:
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
    # body は record でなく専用 setter で書く (thin feed の後追い抽出と同経路)
    repo.update_article_body(aid, body or title)


class _FakeLLM:
    """title に 'subject' を含む記事だけ適合と返す fake (model property 必須の掟)。"""

    model = "fake-judge"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_structured(self, prompt: str, schema: type, **_kwargs: Any) -> JudgeVerdict:
        self.calls.append(prompt)
        assert schema is JudgeVerdict
        return JudgeVerdict(matched="subject" in prompt, reason="t")


# ---- pir_judge_rev ----


def test_pir_judge_rev_stable_and_sensitive_to_edits() -> None:
    base = _judge_pir()

    assert pir_judge_rev(base) == pir_judge_rev(_judge_pir())
    assert pir_judge_rev(base) != pir_judge_rev(_judge_pir(description="別の意図"))
    assert pir_judge_rev(base) != pir_judge_rev(_judge_pir(question="主題か?"))


# ---- prompt ----


def test_build_judge_prompt_contains_subject_rule_and_question() -> None:
    pir = _judge_pir(question="APT 実態暴露が主題か")
    row = {"title": "i-Soon leak dossier", "summary": "s", "body": "b"}

    prompt = build_judge_prompt(pir, row)

    assert "主題" in prompt
    assert "例示" in prompt  # 言及≠主題の区別を明示
    assert "判定基準: APT 実態暴露が主題か" in prompt
    assert "i-Soon leak dossier" in prompt


# ---- verdict 永続化 (repo mixin) ----


def test_upsert_roundtrip_and_overwrite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo = RunHistoryRepository(db_path=tmp_path / "j.db")

    repo.upsert_pir_llm_judgment("a1", "pir_x", matched=True, reason="r1", pir_rev="rev1")
    repo.upsert_pir_llm_judgment("a1", "pir_x", matched=False, reason="r2", pir_rev="rev2")
    repo.upsert_pir_llm_judgment("a2", "pir_x", matched=True, reason="r3", pir_rev="rev2")

    state = repo.fetch_pir_llm_state("pir_x")
    confirmed = repo.fetch_pir_llm_confirmed()
    assert state == {"a1": "rev2", "a2": "rev2"}
    assert confirmed == {"a2": frozenset({"pir_x"})}  # a1 は負 verdict = confirmed に出ない


# ---- judge_pending ----


def test_judge_pending_disabled_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "0")

    result = asyncio.run(judge_pending())

    assert result["disabled"] == 1
    assert result["judged"] == 0


def test_judge_pending_judges_candidates_and_saves_negatives(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j2.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "m1", "i-Soon subject expose")  # 候補 + LLM 適合
    _seed(repo, run_id, "m2", "sanctions mention i-Soon only")  # 候補 + LLM 不適合
    _seed(repo, run_id, "n1", "unrelated weather")  # 候補外 (LLM を呼ばない)

    cfg = PirConfig(priorities=[_judge_pir()])
    from src.pir import integration

    monkeypatch.setattr(integration, "get_pir_config", lambda *a, **k: cfg)
    llm = cast(LLMClient, _FakeLLM())

    stats = asyncio.run(judge_pending(repo=repo, llm=llm))

    assert stats["candidates"] == 2
    assert stats["judged"] == 2
    assert len(cast(_FakeLLM, llm).calls) == 2  # 候補外に LLM を呼ばない
    state = repo.fetch_pir_llm_state("pir_apt_leak")
    assert set(state) == {"m1", "m2"}  # 負 verdict も保存
    assert repo.fetch_pir_llm_confirmed() == {"m1": frozenset({"pir_apt_leak"})}


def test_judge_pending_skips_fresh_and_rejudges_stale(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j3.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "m1", "i-Soon subject expose")

    from src.pir import integration

    pir = _judge_pir()
    monkeypatch.setattr(integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[pir]))
    llm = cast(LLMClient, _FakeLLM())

    # 1 回目: 判定される
    s1 = asyncio.run(judge_pending(repo=repo, llm=llm))
    # 2 回目: fresh verdict → skip
    s2 = asyncio.run(judge_pending(repo=repo, llm=llm))
    assert (s1["judged"], s2["judged"]) == (1, 0)
    assert len(cast(_FakeLLM, llm).calls) == 1

    # description 編集 → rev が変わり stale → 再判定
    edited = _judge_pir(description="編集後の意図")
    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[edited])
    )
    s3 = asyncio.run(judge_pending(repo=repo, llm=llm))
    assert s3["judged"] == 1
    assert len(cast(_FakeLLM, llm).calls) == 2


def test_judge_pending_include_stale_false_judges_new_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """毎時 incremental の絞り: stale (判定済みだが rev 不一致) は残し、未判定のみ判定する。"""
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j7.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "old1", "i-Soon subject expose")

    from src.pir import integration

    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[_judge_pir()])
    )
    llm = cast(LLMClient, _FakeLLM())

    # old1 を判定 → description 編集で stale 化 → 新着 new1 を追加
    asyncio.run(judge_pending(repo=repo, llm=llm))
    edited = _judge_pir(description="編集後の意図")
    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[edited])
    )
    _seed(repo, run_id, "new1", "i-Soon subject fresh leak")

    stats = asyncio.run(judge_pending(repo=repo, llm=llm, include_stale=False))

    assert stats["judged"] == 1  # new1 のみ (stale old1 は夜間に残す)
    assert stats["pending"] == 1
    # 夜間相当 (include_stale=True) は stale old1 を拾う
    stats_night = asyncio.run(judge_pending(repo=repo, llm=llm))
    assert stats_night["judged"] == 1


def test_judge_pending_sync_entities_tags_matched_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """sync_entities=True: 適合 verdict は pir タグ即時反映、負 verdict はタグを立てない。"""
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j8.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "m1", "i-Soon subject expose")  # LLM 適合
    _seed(repo, run_id, "m2", "sanctions mention i-Soon only")  # LLM 不適合

    from src.pir import integration

    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[_judge_pir()])
    )
    llm = cast(LLMClient, _FakeLLM())

    stats = asyncio.run(judge_pending(repo=repo, llm=llm, sync_entities=True))

    assert stats["judged"] == 2
    assert stats["entities_added"] == 1
    assert ("pir", "pir_apt_leak") in repo.get_entities_by_article("m1")
    assert all(t != "pir" for t, _ in repo.get_entities_by_article("m2"))


def test_judge_hourly_new_only_and_tags(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """毎時 wrapper: 新着のみ + 適合タグ即時反映が既定で有効。"""
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j9.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "h1", "i-Soon subject expose")

    from src.pir import integration

    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[_judge_pir()])
    )
    llm = cast(LLMClient, _FakeLLM())

    stats = asyncio.run(judge_hourly(repo=repo, llm=llm))

    assert stats["judged"] == 1
    assert stats["entities_added"] == 1
    assert ("pir", "pir_apt_leak") in repo.get_entities_by_article("h1")
    # 2 回目は fresh → no-op (冪等)
    stats2 = asyncio.run(judge_hourly(repo=repo, llm=llm))
    assert stats2["judged"] == 0


def test_judge_pending_respects_cap(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j4.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    for i in range(3):
        _seed(repo, run_id, f"m{i}", f"i-Soon subject expose {i}")

    from src.pir import integration

    monkeypatch.setattr(
        integration, "get_pir_config", lambda *a, **k: PirConfig(priorities=[_judge_pir()])
    )
    llm = cast(LLMClient, _FakeLLM())

    stats = asyncio.run(judge_pending(repo=repo, llm=llm, max_judgments=2))
    assert stats["judged"] == 2
    assert stats["capped_out"] == 1


def test_cap_is_distributed_round_robin_across_pirs(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """cap は PIR 横断のラウンドロビンで公平配分する (監査 2026-08-01 ③)。

    旧実装は config 並び順の FIFO で、候補の多い先頭 PIR (apt_leak 型) が cap を
    独占し後段 PIR が恒常的に押し出される構造だった。
    """
    import asyncio

    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j5.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    for i in range(3):
        _seed(repo, run_id, f"m{i}", f"i-Soon subject expose {i}")

    from src.pir import integration

    pir_b = Pir(
        id="pir_b",
        title="PIR-B-ROUND-ROBIN",
        description="d",
        match=_GATE,
        llm_judge=LlmJudgeConfig(enabled=True),
    )
    monkeypatch.setattr(
        integration,
        "get_pir_config",
        lambda *a, **k: PirConfig(priorities=[_judge_pir(), pir_b]),
    )
    llm = _FakeLLM()

    stats = asyncio.run(judge_pending(repo=repo, llm=cast(LLMClient, llm), max_judgments=2))

    assert stats["judged"] == 2
    a_calls = sum("APT 内部情報漏洩" in p for p in llm.calls)
    b_calls = sum("PIR-B-ROUND-ROBIN" in p for p in llm.calls)
    assert (a_calls, b_calls) == (1, 1), "先頭 PIR が cap を独占している (FIFO のまま)"


# ---- rebuild 合成 (verdict が pir entity に反映される) ----


def test_rebuild_requires_llm_verdict_for_judge_pir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    monkeypatch.setenv("PIR_LLM_JUDGE", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j5.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "c1", "i-Soon leak confirmed subject")  # 候補 + verdict 適合
    _seed(repo, run_id, "c2", "i-Soon mentioned in passing")  # 候補 + verdict 未判定

    repo.upsert_pir_llm_judgment("c1", "pir_apt_leak", matched=True, reason="主題", pir_rev="r1")

    from src.pir import persist

    monkeypatch.setattr(
        persist, "get_pir_config", lambda *a, **k: PirConfig(priorities=[_judge_pir()])
    )
    stats = persist.rebuild_pir_entities(repo=repo)

    assert stats["pir_links"] == 1
    assert ("pir", "pir_apt_leak") in repo.get_entities_by_article("c1")
    assert all(t != "pir" for t, _ in repo.get_entities_by_article("c2"))


def test_rebuild_includes_match_tree_only_pir(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """has_match_criteria 修正: strong_signals 空でも match ツリーがあれば rebuild 対象。"""
    monkeypatch.setenv("PIR_SIGNAL_FIRST", "1")
    repo = RunHistoryRepository(db_path=tmp_path / "j6.db")
    run_id = repo.start_run(RunRecord(started_at=datetime.now(UTC), pipeline="t", dry_run=True))
    _seed(repo, run_id, "v1", "a vuln report")

    pir = Pir(
        id="pir_tree_only",
        title="T",
        strong_signals=StrongSignals(),  # 空
        match={"property": "category", "op": "eq", "value": "apt"},
    )
    from src.pir import persist

    monkeypatch.setattr(persist, "get_pir_config", lambda *a, **k: PirConfig(priorities=[pir]))
    stats = persist.rebuild_pir_entities(repo=repo)

    assert stats["pirs"] == 1
    assert ("pir", "pir_tree_only") in repo.get_entities_by_article("v1")
