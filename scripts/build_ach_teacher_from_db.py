#!/usr/bin/env python3
"""保存済み Sonnet ACH 判定から教師対を抽出する (§31 Phase A、bridge 枠ゼロ)。

台帳 situation_revisions には reasoning=Sonnet 期 (2026-07-23〜) の増分 ACH 判定が
~1,400 件保存されている (利用者指摘 2026-09-08)。プロンプトは未保存だが、prior
(直前 revision) と証拠窓 (situation_evidence.added_at) から再構築できる。

忠実性の設計:
- **prompt は本物の incremental_ground_and_score に捕獲クライアントを差して取る**
  (テンプレ組み立てを再実装しない — byte 一致)。捕獲後に sentinel 例外で中断し
  LLM は呼ばない。
- 時代分割: prior 抜粋 = added_at <= prior.created_at / 新着ソース = その後〜R まで。
- **引用実在チェック** (時代混在の防御、08-22 関門と同型): 保存 excerpt が再構築
  ソース本文に見つからない対は丸ごと落とす (要約の書き換え・本文更新の混入を遮断)。
- carried 指標は再構築不能のため空 — completion の fired_indicators=[] と自己整合する
  (本番 prompt には carried 節がある分の分布差は注記の上で許容)。
- confidence は後処理済み最終値 (adversarial/posture cap 通過後) — 検証済み判定を教える。
- ⚠ 残る既知の偏り: reasoning fallback (31B) 混入の可能性 ~1 割 (model 列なし)。
  抽出後の分布監査 + 精読で特徴付けること。

使用例 (コンテナ、LLM 不要):
    cp scripts/build_ach_teacher_from_db.py data/mlx/ && \\
    docker compose run --rm --no-deps -T kuebiko python data/mlx/build_ach_teacher_from_db.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

for _p in ("/app", "/path/to/kuebiko"):
    if Path(_p, "src").is_dir():
        sys.path.insert(0, _p)
        break

from src.assessment.situation_store import RevisionRow, SituationStore  # noqa: E402
from src.assessment.standing import STANDING_KIND  # noqa: E402
from src.assessment.stateful import _build_source, _prior_view  # noqa: E402
from src.cti.source_basis import classify_source_tier  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES  # noqa: E402
from src.synthesis.grounded.incremental import (  # noqa: E402
    _WireIncEvidence,
    _WireIncHypothesis,
    _WireIncremental,
    incremental_ground_and_score,
)

SONNET_ERA_START = "2026-07-23"
DEFAULT_OUT = Path("data/mlx/teacher/ach.jsonl")
_WS_RE = re.compile(r"\s+")


class _CapturedError(Exception):
    """prompt 捕獲済みの合図 (LLM は呼ばない)。"""


class CaptureClient:
    model = "capture-only"

    def __init__(self) -> None:
        self.prompt: str | None = None

    async def generate_structured(self, prompt: str, schema: Any, **_kw: Any) -> Any:
        self.prompt = prompt
        raise _CapturedError


def _norm(text: str) -> str:
    return _WS_RE.sub("", text)


def _evidence_rows(store: SituationStore, sid: str) -> list[dict[str, str]]:
    """situation_evidence の生行 (added_at つき)。store に直接 API が無いため SQL。"""
    with store._repo._connect() as conn:  # noqa: SLF001 — 読み取り専用抽出
        rows = conn.execute(
            "SELECT article_id, polarity, attribution_basis, excerpt, added_at "
            "FROM situation_evidence WHERE situation_id = ? ORDER BY added_at",
            (sid,),
        ).fetchall()
    out = []
    for r in rows:
        v = list(r.values()) if hasattr(r, "values") else list(r)
        out.append(
            {
                "article_id": str(v[0]),
                "polarity": str(v[1]),
                "attribution_basis": str(v[2]),
                "excerpt": str(v[3]),
                "added_at": str(v[4]),
            }
        )
    return out


async def _capture_prompt(
    row: Any, prior: RevisionRow, prior_excerpts: list[dict[str, str]],
    sources: list[dict[str, str]], tier_by_id: dict[str, str]
) -> str | None:
    cap = CaptureClient()
    try:
        await incremental_ground_and_score(
            llm=cap,  # type: ignore[arg-type]
            situation_title=row.title,
            prior=_prior_view(prior, prior_excerpts),
            domain=row.domain,
            sources=sources,
            tier_by_id=tier_by_id,
            hypotheses_override=POSTURE_HYPOTHESES if row.kind == STANDING_KIND else None,
        )
    except _CapturedError:
        return cap.prompt
    except Exception as exc:  # noqa: BLE001 — 組み立て自体の失敗は skip
        print(f"  組み立て失敗: {type(exc).__name__}: {str(exc)[:80]}", file=sys.stderr)
    return None


async def main_async(args: argparse.Namespace) -> int:
    repo = RunHistoryRepository()
    store = SituationStore()
    revs_by_sid = store.revisions_since(SONNET_ERA_START, until_iso="9999")
    done: set[str] = set()
    if args.out.exists():
        done = {
            json.loads(line)["key"]
            for line in args.out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)

    stats = {"ok": 0, "no_prior": 0, "no_sources": 0, "citation_fail": 0, "build_fail": 0}
    with args.out.open("a", encoding="utf-8") as fh:
        for sid, revs in revs_by_sid.items():
            row = store.get_situation(sid)
            if row is None:
                continue
            ev_all = _evidence_rows(store, sid)
            for r in sorted(revs, key=lambda x: x.rev):
                key = f"{sid}:r{r.rev}"
                if key in done:
                    continue
                if stats["ok"] >= args.limit:
                    break
                prior = store.latest_revision_before(sid, until_iso=r.created_at)
                if prior is None:
                    stats["no_prior"] += 1
                    continue
                window = [
                    e for e in ev_all if prior.created_at < e["added_at"] <= r.created_at
                ]
                if not window:
                    stats["no_sources"] += 1
                    continue
                prior_ex = [
                    {"polarity": e["polarity"], "excerpt": e["excerpt"]}
                    for e in ev_all
                    if e["added_at"] <= prior.created_at and e["excerpt"]
                ]
                sources, tier_by_id = [], {}
                for e in window[:8]:
                    src = _build_source(repo, e["article_id"])
                    if src:
                        sources.append(src)
                        tier_by_id[e["article_id"]] = classify_source_tier(
                            src["feed_title"], src["feed_url"]
                        )
                if not sources:
                    stats["no_sources"] += 1
                    continue
                # 引用実在チェック: 窓の証拠 excerpt が再構築ソースに実在すること
                joined = _norm(" ".join(s.get("text", "") for s in sources))
                used = [e for e in window if any(s["article_id"] == e["article_id"] for s in sources)]
                if not used or any(
                    e["excerpt"] and _norm(e["excerpt"])[:80] not in joined for e in used
                ):
                    stats["citation_fail"] += 1
                    continue
                prompt = await _capture_prompt(row, prior, prior_ex, sources, tier_by_id)
                if prompt is None:
                    stats["build_fail"] += 1
                    continue
                idx_by_aid = {s["article_id"]: i + 1 for i, s in enumerate(sources)}
                wire = _WireIncremental(
                    evidence=[
                        _WireIncEvidence(
                            index=idx_by_aid.get(e["article_id"], 0),
                            article_id=e["article_id"],
                            attribution_basis=e["attribution_basis"],
                            excerpt=e["excerpt"],
                            polarity=e["polarity"],
                        )
                        for e in used
                    ],
                    hypotheses=[
                        _WireIncHypothesis(**h) for h in json.loads(r.hypotheses_json or "[]")
                    ],
                    leading_hypothesis=r.leading_hypothesis,
                    confidence=r.confidence,
                    claim=r.claim,
                    claim_type=r.claim_type,
                    implication=r.implication,
                    key_assumptions=json.loads(r.assumptions_json or "[]"),
                    missing_evidence=json.loads(r.missing_json or "[]"),
                    indicators=json.loads(r.indicators_json or "[]"),
                )
                fh.write(
                    json.dumps(
                        {"key": key, "prompt": prompt, "completion": wire.model_dump_json()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                stats["ok"] += 1
                if stats["ok"] % 50 == 0:
                    print(f"  採用 {stats['ok']} …", flush=True)

    print(f"\n完了: {stats}")
    return 0


def main() -> int:
    import asyncio

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=600)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
