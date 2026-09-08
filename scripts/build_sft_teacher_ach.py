#!/usr/bin/env python3
"""ACH (台帳増分採点) の SFT 教師対を外部 LLM で生成する (§31 Phase A)。

背景 (2026-09-08 実測): N1 は ACH 未学習のため仮定が主張を鵜呑みにする (base 挙動)。
懐疑的スタンスは SFT が運ぶ側 (event news の caveats で実証済み) — ACH ドメインの
教師対を作り蒸留する。教師は Sonnet + think OFF (think A/B で leading 10/10 一致・
コスト半分、SYNTHESIS.md §30)。

入力は deep_review と同じ組み立てを過去日窓で再構築する (spotlight 教師と同型):
日 D の窓 [D-24h, D] の対象 situation に対し、prior = D-24h 以前の revision。
評価予約: 凍結スナップショット (ledger_ab_snapshot*.pkl) の (sid, 日) は収穫しない。

使用例 (コンテナ内、bridge 必須):
    docker compose run --rm --no-deps -T -e LLM_LOCAL_FALLBACK=0 kuebiko \\
        python scripts/build_sft_teacher_ach.py --model claudecode:sonnet --days 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pickle
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.assessment.deep_review import _MAX_SOURCES, _select_targets  # noqa: E402
from src.assessment.situation_store import SituationStore  # noqa: E402
from src.assessment.standing import STANDING_KIND  # noqa: E402
from src.assessment.stateful import _build_source, _prior_view  # noqa: E402
from src.config_loader import load_app_config  # noqa: E402
from src.cti.source_basis import classify_source_tier  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES  # noqa: E402
from src.synthesis.grounded.incremental import incremental_ground_and_score  # noqa: E402
from src.tools.llm_client import LLMClient  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

DEFAULT_OUT = Path("data/mlx/teacher/ach.jsonl")
_T = TypeVar("_T", bound=BaseModel)


class RecordingClient:
    """(prompt, 構造化出力 JSON) を捕獲する透過ラッパ (spotlight 教師と同型)。"""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.last: tuple[str, str] | None = None

    @property
    def model(self) -> str:
        return self._inner.model

    async def generate_structured(self, prompt: str, schema: type[_T], **kw: Any) -> _T:
        out = await self._inner.generate_structured(prompt, schema, **kw)
        self.last = (prompt, out.model_dump_json())
        return out


def _reserved_keys(patterns: list[Path]) -> set[str]:
    """凍結スナップショットの (sid, 採取日) を予約 (収穫しない)。"""
    reserved: set[str] = set()
    for p in patterns:
        try:
            items = pickle.loads(p.read_bytes())
        except Exception:  # noqa: BLE001 — 予約ファイルの欠損は空扱い
            continue
        day = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC).date().isoformat()
        for it in items:
            reserved.add(f"{it['sid']}:{day}")
    return reserved


def _done_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["key"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_app_config()
    teacher = build_llm_for_ref(args.model, Step.SYNTHESIS_ANALYSIS, cfg)
    rec = RecordingClient(teacher)
    repo = RunHistoryRepository()
    store = SituationStore()

    reserved = _reserved_keys(sorted(Path("data/mlx").glob("ledger_ab_snapshot*.pkl")))
    done = _done_keys(args.out)
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    print(f"予約 {len(reserved)} / 済 {len(done)}", file=sys.stderr)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    ok = skipped = failed = 0
    consecutive = 0
    with args.out.open("a", encoding="utf-8") as fh:
        for d in range(args.eval_reserve_days, args.days):
            until_dt = today - timedelta(days=d - 1)  # 窓の終端 (その日の 00:00 UTC)
            since_iso = (until_dt - timedelta(hours=24)).isoformat()
            day = until_dt.date().isoformat()
            revs = store.revisions_since(since_iso, until_iso=until_dt.isoformat())
            targets, _ = _select_targets(store, revs, since_iso=since_iso, cap=args.per_day)
            for sid, today_aids in targets:
                key = f"{sid}:{day}"
                if key in done or key in reserved:
                    continue
                if ok >= args.limit:
                    print(f"\n上限 {args.limit} 到達: 採用 {ok} / skip {skipped} / 失敗 {failed}")
                    return 0
                row = store.get_situation(sid)
                if row is None:
                    continue
                sources, tier_by_id = [], {}
                for aid in today_aids[:_MAX_SOURCES]:
                    src = _build_source(repo, aid)
                    if src:
                        sources.append(src)
                        tier_by_id[aid] = classify_source_tier(
                            src["feed_title"], src["feed_url"]
                        )
                prior = store.latest_revision_before(sid, until_iso=since_iso)
                if not sources or prior is None:
                    skipped += 1
                    fh.write(json.dumps({"key": key, "skipped": True}) + "\n")
                    fh.flush()
                    continue
                rec.last = None
                try:
                    await incremental_ground_and_score(
                        llm=rec,
                        situation_title=row.title,
                        prior=_prior_view(prior, store.evidence_excerpts(sid)),
                        domain=row.domain,
                        sources=sources,
                        tier_by_id=tier_by_id,
                        hypotheses_override=(
                            POSTURE_HYPOTHESES if row.kind == STANDING_KIND else None
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 — 1 件失敗で全体を落とさない
                    failed += 1
                    consecutive += 1
                    print(f"  {key} FAIL {type(exc).__name__}: {str(exc)[:80]}", flush=True)
                    if consecutive >= 5:
                        print("連続失敗上限 — 中断 (rc=1)", file=sys.stderr)
                        return 1
                    continue
                consecutive = 0
                if rec.last is None:
                    skipped += 1
                    fh.write(json.dumps({"key": key, "skipped": True}) + "\n")
                    fh.flush()
                    continue
                prompt, completion = rec.last
                fh.write(
                    json.dumps(
                        {"key": key, "prompt": prompt, "completion": completion},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                fh.flush()
                ok += 1
                if ok % 20 == 0:
                    print(f"  採用 {ok} / skip {skipped} / 失敗 {failed}", flush=True)

    print(f"\n完了: 採用 {ok} / skip {skipped} / 失敗 {failed} → {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, help="教師 ref (例 claudecode:sonnet)")
    ap.add_argument("--days", type=int, default=30, help="過去何日分の窓まで遡るか")
    ap.add_argument("--eval-reserve-days", type=int, default=1, help="直近の予約日数")
    ap.add_argument("--per-day", type=int, default=15, help="1 日窓あたりの対象上限")
    ap.add_argument("--limit", type=int, default=300, help="採用対の総上限")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
