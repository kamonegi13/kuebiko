#!/usr/bin/env python3
"""常設情報要求 (PIR の問い) への「答えの物語」を narrative モデルで書かせる試作 (2026-09-17)。

利用者の問題提起: 問いへの答えは ACH 判定の結合ではなく、判定された材料から書く**物語**であるべき。
本試作は台帳の射影 (最新判定・前回判定との差分・評価済み証拠・欠落・指標) を 1 つの estimate に
組み、narrative ティアのモデルに CoT 先頭の schema で答えを書かせる。**確度を変えず主張を足さない**
(状況総括 render と同じ「射影であって再分析ではない」原則)。

使い方 (DB + Ollama。ホストなら DATABASE_URL と OLLAMA_BASE_URL=http://127.0.0.1:11434 を付ける):
    python scripts/render_pir_answer_prototype.py --model kuebiko-sft:n17m30 \
        --out data/mlx/pir_answer_proto_n17m30.json
    python scripts/render_pir_answer_prototype.py --dry-run   # プロンプトだけ印字 (LLM 不要)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.situation_store import RevisionRow, SituationStore  # noqa: E402
from src.assessment.standing import STANDING_KIND  # noqa: E402
from src.config_loader import load_app_config  # noqa: E402
from src.tools.llm_schema import require_all_properties  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

_CONF_JA = {"high": "高確度", "moderate": "中確度", "low": "低確度"}
_EVIDENCE_LIMIT = 12
_TEMPERATURE = 0.2
_MAX_TOKENS = 4_000


class _WireAnswer(BaseModel):
    """答えの物語 (CoT 先頭)。全欄 required (途中閉じ防止)。"""

    model_config = ConfigDict(extra="ignore")
    analysis_notes: str = ""
    answer: str = ""
    story: str = ""
    change: str = ""
    gaps_and_indicators: str = ""

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: CoreSchema, handler: Any) -> JsonSchemaValue:
        return require_all_properties(dict(handler(core_schema)))


def _persona() -> str:
    """ペルソナ partial の本文 (Jinja コメント {# … #} は落とす)。"""
    for name in ("_persona_local.j2", "_persona.j2"):
        p = Path("prompts") / name
        if p.exists():
            text = re.sub(r"\{#.*?#\}", "", p.read_text(encoding="utf-8"), flags=re.S)
            return text.strip()
    return ""


def _rev_block(r: RevisionRow, label: str) -> str:
    hyps = json.loads(r.hypotheses_json or "[]")
    lines = [
        f"[{label}] (rev {r.rev}, {r.created_at[:10]})",
        f"  答え (claim): {r.claim}",
        f"  見立て: {r.leading_hypothesis} / 確度: {_CONF_JA.get(r.confidence, r.confidence)}"
        f" / 確度の根拠: {r.confidence_basis}",
    ]
    for h in hyps[:6]:
        if isinstance(h, dict):
            lines.append(
                f"  仮説「{h.get('hypothesis', '')}」整合 {h.get('consistent', '')}"
                f" / 反整合 {h.get('inconsistent', '')}"
            )
    if r.implication:
        lines.append(f"  含意: {r.implication}")
    for key, title in (
        ("assumptions_json", "前提"),
        ("missing_json", "欠落証拠"),
        ("indicators_json", "指標"),
    ):
        vals = json.loads(getattr(r, key) or "[]")
        if vals:
            lines.append(f"  {title}: " + " / ".join(str(v) for v in vals[:6]))
    if r.delta_type and r.delta_type != "no_change":
        lines.append(f"  前回からの変化: {r.delta_type} — {r.delta_note}")
    return "\n".join(lines)


def build_prompt(
    *, question: str, latest: RevisionRow, prior: RevisionRow | None, evidence: list[dict[str, str]]
) -> str:
    ev_lines = [
        f"- ({e['polarity']} / {e['attribution_basis']} / {e['source_tier']}) "
        f"「{e['excerpt'][:200]}」"
        for e in evidence
        if e.get("excerpt")
    ]
    intro = (
        f"下記は常設情報要求 (PIR) の問い「{question}」に対する、"
        "台帳の**確定した現在の判定**です。この材料だけを使って、"
        "問いへの答えを読者 (脅威情報の分析者) 向けの物語として書いてください。"
    )
    rules = (
        "**厳守 — これは射影であって再分析ではない**:\n"
        "- 判定の確度を変えない。確度語は 高確度=「可能性が高い」/ "
        "中確度=「可能性がある・とみられる」/ 低確度=「確証はなく〜の可能性も否定できない」"
        "で本文に必ず言語化する。\n"
        "- 材料に無い主張・帰属・断定を足さない。証拠抜粋の範囲で書く。\n"
        "- 「claim を改訂した」「確度を引き上げた」のような台帳の操作は書かない。"
        "変化は世界側の事実で書く。\n"
        "- 答えが動いていないなら「動いていない」と明記し、鮮度 (最新判定の日付) を添える。"
    )
    fields = (
        "次の JSON 欄を日本語で書いてください:\n"
        "- analysis_notes: 本文の前の分析メモ (読者には出さない)。"
        "①証拠の独立性 (同一媒体の再掲を複数と数えていないか、直接証拠か間接か) "
        "②時制 (本期間に新たに起きたことと継続中のことの区別) "
        "③前回の答えとの差分と、その理由が世界側の事実か "
        "④答えを阻害している欠落 ⑤日本の直接証拠の有無 — の 5 点を番号付きで。\n"
        "- answer: 問いへの答え (BLUF)。1-2 文、確度語つき。\n"
        "- story: 根拠の物語。何が分かっていて、どう繋がって、なぜその答えになるか (3-6 文)。\n"
        "- change: 前回の答えから何が動いたか。動いていなければその旨と鮮度 (1-3 文)。\n"
        "- gaps_and_indicators: 答えを変えうる欠落と、見張るべき指標 (2-4 文)。"
    )
    parts = [
        _persona(),
        "",
        intro,
        "",
        rules,
        "",
        _rev_block(latest, "現在の判定"),
        "",
        _rev_block(prior, "前回の判定") if prior else "[前回の判定] なし (初回)",
        "",
        "【評価済み証拠 (新しい順)】",
        *(ev_lines or ["(評価済み証拠なし)"]),
        "",
        fields,
    ]
    return "\n".join(parts)


async def main_async(args: argparse.Namespace) -> int:
    store = SituationStore(db_path=Path("data/run_history.db"))
    rows = [r for r in store.load_situations(("active", "dormant")) if r.kind == STANDING_KIND]
    print(f"常設の問い {len(rows)} 件")
    items: list[dict[str, Any]] = []
    for row in rows:
        latest = store.latest_revision(row.situation_id)
        if latest is None:
            print(f"  {row.situation_id}: 判定なし — skip")
            continue
        prior = store.latest_revision_before(
            row.situation_id, until_iso=latest.created_at.replace("+00:00", "")[:19]
        )
        if prior is not None and prior.rev == latest.rev:
            prior = None
        evidence = store.evidence_items(row.situation_id, limit=_EVIDENCE_LIMIT)
        prompt = build_prompt(question=row.title, latest=latest, prior=prior, evidence=evidence)
        items.append({"situation_id": row.situation_id, "question": row.title, "prompt": prompt})
        print(
            f"  {row.situation_id}: prompt {len(prompt)} 字 / 証拠 {len(evidence)} / "
            f"rev {latest.rev}"
        )
    if args.dry_run:
        if items:
            print("\n===== 例 (1 件目) =====\n" + items[0]["prompt"][:3000])
        return 0
    llm = build_llm_for_ref(args.model, Step.SYNTHESIS_NARRATIVE, load_app_config())
    out: list[dict[str, Any]] = []
    for it in items:
        try:
            r = await llm.generate_structured(
                it["prompt"],
                _WireAnswer,
                temperature=_TEMPERATURE,
                max_tokens=_MAX_TOKENS,
                think=False,
            )
            it = {**it, "output": r.model_dump(), "model": args.model}
            print(
                f"  {it['situation_id']} ok (answer {len(r.answer)} 字 / story {len(r.story)} 字)"
            )
        except Exception as exc:  # noqa: BLE001 — 1 問の失敗で全体を落とさない
            print(f"  {it['situation_id']} FAIL {type(exc).__name__}: {str(exc)[:80]}")
            it = {**it, "error": type(exc).__name__}
        out.append(it)
        args.out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"完了 → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="kuebiko-sft:n17m30")
    ap.add_argument("--out", type=Path, default=Path("data/mlx/pir_answer_proto.json"))
    ap.add_argument("--dry-run", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
