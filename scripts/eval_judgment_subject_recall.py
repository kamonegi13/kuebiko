#!/usr/bin/env python3
"""judgment 主体判定の保守バイアス評価 (2026-08-21、rubric チューニングの統制付き比較)。

**正解ラベルの流用**: subject_actor_source='feed' (ransomware.live 犯行声明 = 決定論帰属)
の記事を positive set とし、その title/body を LLM 判定に掛けて主体を当てられるかで
recall を測る (人手ラベル不要)。**統制**: 同一 rubric を 2 回実行し temperature 揺らぎの
基準線を得る (eval_goldset 2026-08-17 の教訓 — 対照群なしでは何も言えない)。
**回帰面**: 地政学/政策記事 (主体なしが正) に誤って主体を付けないかを同時測定 —
主題定義の緩和は言及ノイズ再流入 (ゼレンスキー記事の qilin) と背中合わせのため。

rubric 変種は DB を触らず in-memory blocks で合成する (層分け 04c4655 の成果)。

実行はコンテナ内 (DATABASE_URL + Ollama 到達):
    docker exec kuebiko python scripts/eval_judgment_subject_recall.py --positives 30 --controls 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.cti.judgment_classifier import (  # noqa: E402
    _BODY_MAX_CHARS,
    JudgmentOut,
    compose_from_blocks,
)
from src.prompts.prompt_store import load_prompt_rubric  # noqa: E402
from src.prompts.registry import get_spec  # noqa: E402

# 保守バイアス緩和の候補文 (subject_actor block 末尾に追記)。
# 「被害開示が主の記事で犯行声明グループを主題にしない」境界例 (Baylor 型) を狙う。
VARIANT_APPEND = (
    "\n被害組織の開示・侵害公表が主の記事でも、攻撃の実行主体として名指し・犯行声明・"
    "帰属されているアクターは主題とする (出自紹介・比較・過去事例としての言及とは区別する)。"
)


def _load_blocks() -> dict[str, str]:
    spec = get_spec("judgment_classifier")
    assert spec is not None
    rubric = load_prompt_rubric(spec)
    assert rubric is not None, "judgment_rubric が store/seed から読めません"
    return {s.field_id: s.body for s in rubric.sections}


def _fetch_sets(n_pos: int, n_ctrl: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from src.storage.db_backend import connect
    from src.storage.run_history import RunHistoryRepository

    con = connect(RunHistoryRepository().db_path)
    try:
        # 正例 = 「犯行声明 (feed 帰属) と同一 victim_org を ±5 日に報じたニュース記事」。
        # feed 記事自体は構造化レコードで散文 body を持たないため、直接は使えない (初回実験
        # で実証)。声明のギャング = 正解ラベル、ニュース側の散文 = 判定入力。
        # ⚠ INTERVAL 構文は PG 前提 (本スクリプトはコンテナ内実行専用)。
        pos_sql = (
            "WITH claims AS ("
            "  SELECT a.subject_actor_ids AS gt, LOWER(e.value) AS org, a.created_at"
            "  FROM articles a JOIN article_entities e"
            "    ON e.article_id=a.article_id AND e.entity_type='victim_org'"
            "  WHERE a.subject_actor_source='feed' AND COALESCE(a.subject_actor_ids,'') <> ''"
            "), news AS ("
            "  SELECT a.article_id, a.title, a.body, a.category, LOWER(e.value) AS org,"
            "         a.created_at"
            "  FROM articles a JOIN article_entities e"
            "    ON e.article_id=a.article_id AND e.entity_type='victim_org'"
            "  WHERE a.subject_actor_source <> 'feed'"
            "    AND LENGTH(COALESCE(a.body,'')) >= 500"
            "    AND COALESCE(a.article_type,'') <> 'recap'"
            "    AND a.title NOT LIKE ?"  # % は psycopg の placeholder 罠 → パラメータで渡す
            ") "
            "SELECT DISTINCT ON (n.article_id) n.article_id, n.title, n.body, n.category, c.gt "
            "FROM news n JOIN claims c ON n.org = c.org "
            " AND n.created_at BETWEEN c.created_at - INTERVAL '5 days'"
            "                      AND c.created_at + INTERVAL '5 days' "
            "ORDER BY n.article_id, c.created_at DESC LIMIT ?"
        )
        pos = [
            {
                "article_id": str(r[0]),
                "title": str(r[1] or ""),
                "body": str(r[2] or ""),
                "category": str(r[3] or ""),
                "gt": str(r[4] or "").split(",")[0],
            }
            for r in con.execute(pos_sql, ("%ダイジェスト%", n_pos * 3)).fetchall()
        ]
        ctrl_sql = (
            "SELECT a.article_id, a.title, a.body, a.category FROM articles a "
            "WHERE a.category IN ('geopolitical','policy') AND a.subject_actor_source='none' "
            "AND LENGTH(COALESCE(a.body,'')) >= 500 "
            "AND EXISTS (SELECT 1 FROM article_entities e "
            "            WHERE e.article_id=a.article_id AND e.entity_type='actor') "
            "ORDER BY a.created_at DESC LIMIT ?"
        )
        ctrl = [
            {
                "article_id": str(r[0]),
                "title": str(r[1] or ""),
                "body": str(r[2] or ""),
                "category": str(r[3] or ""),
            }
            for r in con.execute(ctrl_sql, (n_ctrl,)).fetchall()
        ]
    finally:
        con.close()
    return pos, ctrl


def _candidates_for(title: str, body: str, category: str) -> list[Any]:
    from src.cti.actor_normalizer import load_actor_aliases
    from src.cti.subject_actor import _relevant_actors

    found = load_actor_aliases().find_all(f"{title}\n{body[:_BODY_MAX_CHARS]}")
    return _relevant_actors(found, category)


def _build_prompt_with(blocks: dict[str, str], art: dict[str, Any], cands: list[Any]) -> str:
    cand = ", ".join(c.id for c in cands) or "(候補なし — subject_actor_id は空)"
    context = {
        "candidates": cand,
        "category": (art["category"] or "").strip() or "?",
        "published": "?",
        "title": (art["title"] or "").strip(),
        "body": (art["body"] or "").strip()[:_BODY_MAX_CHARS],
    }
    return compose_from_blocks(blocks, context)


async def _judge(llm: Any, prompt: str) -> str:
    try:
        out = await llm.generate_structured(prompt, schema=JudgmentOut, think=False)
        return str(out.subject_actor_id or "")
    except Exception:  # noqa: BLE001 — 個別失敗は空判定として集計 (実験の続行優先)
        return "(error)"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positives", type=int, default=30)
    ap.add_argument("--controls", type=int, default=20)
    args = ap.parse_args()

    from src.config_loader import load_app_config
    from src.tools.model_tiers import Step, build_llm_for

    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())
    base = _load_blocks()
    variant = {**base, "subject_actor": base["subject_actor"] + VARIANT_APPEND}

    pos_raw, ctrl = _fetch_sets(args.positives, args.controls)
    # 候補到達率: GT が候補に入らない記事は LLM に選びようがない — 除外して別掲する
    pos: list[dict[str, Any]] = []
    unreachable = 0
    for a in pos_raw:
        cands = _candidates_for(a["title"], a["body"], a["category"])
        if any(c.id == a["gt"] for c in cands):
            pos.append({**a, "cands": cands})
            if len(pos) >= args.positives:
                break
        else:
            unreachable += 1
    for a in ctrl:
        a["cands"] = _candidates_for(a["title"], a["body"], a["category"])

    results: dict[str, Any] = {
        "positives": len(pos),
        "controls": len(ctrl),
        "gt_unreachable_in_candidates": unreachable,
    }
    for label, blocks in (("current", base), ("variant", variant)):
        for run in (1, 2):
            hits = 0
            details: list[str] = []
            for a in pos:
                pred = await _judge(llm, _build_prompt_with(blocks, a, a["cands"]))
                ok = pred == a["gt"]
                hits += int(ok)
                details.append(f"{a['article_id']}:{a['gt']}->{pred or '(空)'}")
            fp = 0
            for a in ctrl:
                pred = await _judge(llm, _build_prompt_with(blocks, a, a["cands"]))
                fp += int(bool(pred) and pred != "(error)")
            key = f"{label}_run{run}"
            results[key] = {
                "positive_recall": round(hits / len(pos), 3) if pos else None,
                "control_false_subject": round(fp / len(ctrl), 3) if ctrl else None,
            }
            results[f"{key}_detail"] = details
            print(f"[{key}] recall={results[key]['positive_recall']}"
                  f" ctrl_fp={results[key]['control_false_subject']}", flush=True)

    print(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
