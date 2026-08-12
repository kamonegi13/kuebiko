"""統合判断分類器 (judgment_classifier) のテスト。

現行 3 分類器 (stance/analysis_axes/subject) を 1 呼び出しに畳んだ置換。防御正規化と
subject の構造ゲート (候補外は空に矯正=誤帰属を構造的に防ぐ) を検証する。
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from src.cti.actor_normalizer import ActorAlias
from src.cti.judgment_classifier import JudgmentOut, classify_judgment
from src.tools.llm_client import LLMClient

_CANDS = [ActorAlias(id="qilin", canonical="Qilin"), ActorAlias(id="apt29", canonical="APT29")]


def _llm(out: JudgmentOut) -> LLMClient:
    llm = AsyncMock()
    llm.generate_structured.return_value = out
    return cast(LLMClient, llm)


async def test_returns_all_judgment_fields() -> None:
    out = JudgmentOut(
        editorial_stance="factual_report",
        article_type="advisory",
        intent="financial",
        confidence="high",
        i_infra=True,
        subject_actor_id="qilin",
        subject_confidence="high",
    )
    res = await classify_judgment(
        _llm(out), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res is not None
    assert res.editorial_stance == "factual_report"
    assert res.article_type == "advisory"
    assert res.intent == "financial"
    assert res.subject_actor_id == "qilin"
    # 下流互換の diamond dict
    sp = res.to_diamond_dict()["socio_political"]
    assert isinstance(sp, dict) and sp["intent"] == "financial"


async def test_out_of_candidate_subject_is_cleared() -> None:
    # 候補外の主題は構造ゲートで空に矯正 (誤帰属防止)
    out = JudgmentOut(subject_actor_id="lazarus", subject_confidence="high")
    res = await classify_judgment(
        _llm(out), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res is not None
    assert res.subject_actor_id == ""


async def test_subject_rationale_preserved_and_trimmed() -> None:
    # 根拠文 (2026-08-13 可視化) は保持し、暴走出力は表示用上限に切り詰める
    out = JudgmentOut(
        subject_actor_id="",
        subject_rationale="  記事の主語はツールの解析であり、\n候補は背景言及のため。  ",
    )
    res = await classify_judgment(
        _llm(out), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res is not None
    assert res.subject_rationale == "記事の主語はツールの解析であり、 候補は背景言及のため。"

    long_out = JudgmentOut(subject_rationale="あ" * 500)
    res2 = await classify_judgment(
        _llm(long_out), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res2 is not None
    assert len(res2.subject_rationale) == 200


async def test_invalid_enums_normalized() -> None:
    out = JudgmentOut(editorial_stance="garbage", article_type="nonsense", confidence="???")
    res = await classify_judgment(
        _llm(out), title="t", category="apt", body="b", published=None, candidates=[]
    )
    assert res is not None
    assert res.editorial_stance == "unknown"
    assert res.article_type == "breaking"
    assert res.confidence == "low"


async def test_empty_body_returns_none() -> None:
    res = await classify_judgment(
        _llm(JudgmentOut()), title="t", category="apt", body="", published=None, candidates=_CANDS
    )
    assert res is None


async def test_empty_body_falls_back_to_summary() -> None:
    """本文が空/極短でも summary があれば判定する (旧 analysis_axes の能力の復元)。

    07-26 の統合で失われた fallback (監査 2026-08-01 ②): 旧分類器は body 200 字未満で
    summary_text を代替入力にしていたが、統合版は空 body で即 None → summarizer 由来の
    枯死 diamond を維持 = intent NULL に落ちていた。
    """
    out = JudgmentOut(intent="espionage", confidence="low")
    llm = _llm(out)
    res = await classify_judgment(
        llm,
        title="t",
        category="apt",
        body="",
        published=None,
        candidates=_CANDS,
        summary_text="国家系アクターによる標的型の情報窃取キャンペーンの要約テキスト。" * 3,
    )
    assert res is not None
    assert res.intent == "espionage"
    # prompt には summary が本文として渡っている
    prompt = cast(AsyncMock, llm.generate_structured).call_args.args[0]
    assert "情報窃取キャンペーン" in prompt


async def test_short_body_prefers_summary_when_longer() -> None:
    """200 字未満の切り株 body より長い summary を優先する (旧挙動と同値)。"""
    out = JudgmentOut(intent="financial")
    llm = _llm(out)
    long_summary = "ランサムウェアによる金銭目的の攻撃キャンペーンに関する詳細な要約。" * 8
    res = await classify_judgment(
        llm,
        title="t",
        category="breach",
        body="短い切り株",
        published=None,
        candidates=_CANDS,
        summary_text=long_summary,
    )
    assert res is not None
    prompt = cast(AsyncMock, llm.generate_structured).call_args.args[0]
    assert "金銭目的の攻撃キャンペーン" in prompt


async def test_llm_failure_returns_none() -> None:
    llm = AsyncMock()
    llm.generate_structured.side_effect = RuntimeError("boom")
    res = await classify_judgment(
        cast(LLMClient, llm), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res is None


# --- C1 (2026-07-27): ungated named_primary_actor (新規アクター発見信号) ---


async def test_named_primary_actor_ungated_keeps_out_of_dictionary_name() -> None:
    """named_primary_actor は候補ゲートを通さない — 辞書外の新顔名をそのまま保持する。"""
    out = JudgmentOut(subject_actor_id="", named_primary_actor="CyberAv3ngers")
    res = await classify_judgment(
        _llm(out), title="t", category="apt", body="b", published=None, candidates=_CANDS
    )
    assert res is not None
    # subject は候補外で空だが、named は発見信号として生き残る
    assert res.subject_actor_id == ""
    assert res.named_primary_actor == "CyberAv3ngers"


async def test_named_primary_actor_drops_defender_orgs() -> None:
    """防御側・報告機関 (NSA 等) は named からも除去 (harvest 汚染防止の二重防御)。"""
    for org in ("US Cyber Command", "NSA", "CISA", "Mandiant"):
        out = JudgmentOut(named_primary_actor=org)
        res = await classify_judgment(
            _llm(out), title="t", category="advisory", body="b", published=None, candidates=[]
        )
        assert res is not None
        assert res.named_primary_actor == "", f"{org} が named に残った"


async def test_named_primary_actor_drops_noise_tokens() -> None:
    """LLM の常套句 (unknown/N/A) は named から除去。"""
    for noise in ("unknown", "N/A", "various", "-"):
        out = JudgmentOut(named_primary_actor=noise)
        res = await classify_judgment(
            _llm(out), title="t", category="apt", body="b", published=None, candidates=[]
        )
        assert res is not None
        assert res.named_primary_actor == ""
