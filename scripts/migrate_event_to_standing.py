#!/usr/bin/env python3
"""積み重ね型の event 情勢を standing (常設情報要求) へ移す (2026-09-19、T3)。

背景 (docs/tracking_unit_separation_design.md): 追跡の単位は 2 種類あり、**積み重ね型**
(問いが境界・閉じない) が event として開設されると 3 つの破綻が起きる — 主題の混在 /
同一主題の分裂 / 無関係な証拠の吸い寄せ。revision 20 件以上の非サイバー 9 件が
全 revision の 45% を消費していた。

この移行は:
1. 問いを ``question_store.promote`` で登録する (資格判定を通る = 決心と証拠条件が要る)
2. 旧 event の**評価済み証拠**を新 standing へ付け替える (未評価の割当は移さない — 問いが
   変わると polarity の意味が変わるため、改めて証拠条件で拾い直す)
3. 旧 event を closed にし、墓標を残す (アクター辞書の merge と同形。id は消さない)
4. **revision は移さない**。問いが変わる以上、答えの履歴は引き継げない (初回評価からやり直す)

⚠ 定義は `--plan` の JSON ファイルで与える。1 件ずつ dry-run → apply すること。
    docker exec kuebiko python scripts/migrate_event_to_standing.py --plan plan.json
    docker exec kuebiko python scripts/migrate_event_to_standing.py --plan plan.json --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.question_draft import QuestionDraft, qualify, question_text  # noqa: E402
from src.assessment.question_store import promote  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402

_DB = Path("data/run_history.db")


def load_plan(path: Path) -> list[dict[str, Any]]:
    """移行計画 (JSON)。各要素: {from: [event id...], frame_id, slots, decision,
    evidence_condition, note}。``from`` が複数 = 分裂していた主題の統合。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("plan は配列であること")
    return data


def draft_of(item: dict[str, Any]) -> QuestionDraft:
    return QuestionDraft(
        frame_id=str(item["frame_id"]),
        slots=dict(item["slots"]),
        decision=str(item.get("decision", "")),
        evidence_condition=dict(item.get("evidence_condition", {})),
        answer_can_move_ack=bool(item.get("answer_can_move_ack", False)),
        note=str(item.get("note", "")),
    )


def plan_report(item: dict[str, Any], store: SituationStore) -> dict[str, Any]:
    """1 件の移行内容を数える (LLM も書込も無し)。"""
    draft = draft_of(item)
    q = qualify(draft)
    srcs = [str(x) for x in item["from"]]
    evidence: dict[str, int] = {}
    revisions: dict[str, int] = {}
    for sid in srcs:
        row = store.get_situation(sid)
        evidence[sid] = len(store.evidence_items(sid, limit=10_000)) if row else -1
        rev = store.latest_revision(sid)
        revisions[sid] = rev.rev if rev else 0
    return {
        "question": question_text(draft) if q.ok else "(資格要件を満たさない)",
        "qualified": q.ok,
        "failures": list(q.failures),
        "from": srcs,
        "evidence_assessed": evidence,
        "latest_rev": revisions,
    }


def migrate(item: dict[str, Any], store: SituationStore, *, now_iso: str) -> dict[str, Any]:
    """1 件を移行する。**冪等** — 既に昇格済みなら証拠の付け替えだけ行う。"""
    draft = draft_of(item)
    # SituationStore は SituationOpener の口 (get_situation / open_situation) を満たす
    sid = promote(draft, store=store, now_iso=now_iso, db_path=_DB)  # type: ignore[arg-type]
    moved = 0
    for src in (str(x) for x in item["from"]):
        for ev in store.evidence_items(src, limit=10_000):
            if store.record_assignment(
                situation_id=sid,
                article_id=str(ev["article_id"]),
                added_at=now_iso,
                assigned_by="anchor",
            ):
                moved += 1
        store.touch_situation(src, last_evidence_at=now_iso, status="closed")
    return {"situation_id": sid, "question": question_text(draft), "evidence_moved": moved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--only", help="この問い (frame_id:slot 値の連結) だけ処理する")
    ap.add_argument("--apply", action="store_true", help="実際に書く (既定は dry-run)")
    args = ap.parse_args()
    store = SituationStore(db_path=_DB)
    now_iso = datetime.now(UTC).isoformat()
    items = load_plan(args.plan)
    ok = True
    for item in items:
        rep = plan_report(item, store)
        if args.only and args.only not in rep["question"]:
            continue
        print(f"\n■ {rep['question']}")
        print(f"  資格: {'OK' if rep['qualified'] else 'NG ' + ' / '.join(rep['failures'])}")
        print(
            f"  元: {rep['from']} 評価済み証拠 {rep['evidence_assessed']}\n"
            f"  最新 rev: {rep['latest_rev']}"
        )
        if not rep["qualified"]:
            ok = False
            continue
        if not args.apply:
            print("  (dry-run — 書込なし)")
            continue
        res = migrate(item, store, now_iso=now_iso)
        print(f"  → {res['situation_id']} へ証拠 {res['evidence_moved']} 件を付け替え、元は closed")
    if not args.apply:
        print("\n(dry-run。実行は --apply)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
