#!/usr/bin/env python3
"""detect ML のバックテスト — held-out 期間の ML 選抜と現行の開設を並べて目視する (2026-09-17)。

shadow を数日待つ代わりに、学習済みモデルを held-out 25 日 (1,576 記事) に当て、
「ML だけ」「現行だけ」「両方」を記事タイトル・種別・確率・審判 (あれば) つきで一覧にする。
出力: data/mlx/detect_ml_backtest.md (目視用) + 標準出力に集計。

ホストで DATABASE_URL を本番 PG に向けて (eval_detect_ml_timesplit.py と同じ):
    uv run python scripts/report_detect_ml_backtest.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.synthesis.grounded.detect_ml import (  # noqa: E402
    build_detect_articles,
    load_detect_model,
    score_articles,
    shadow_select,
)

D = Path("data/mlx")
GOLD = D / "detect_goldset.jsonl"
LABELS = D / "detect_labels.json"
KIND_FILES = (D / "detect_gold_kinds.jsonl", D / "detect_heldout_kinds.jsonl")
OUT = D / "detect_ml_backtest.md"
_HELD_OUT_RATIO = 0.7
_TITLE = 70


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _verdict(g: dict[str, Any] | None) -> str:
    if g is None:
        return "未審判"
    if g["gold_open"]:
        return f"審判=開設 (imp{g['importance']}/trk{g['trackable']})"
    if g["watch"]:
        return f"審判=見張り (imp{g['importance']}/trk{g['trackable']})"
    return f"審判=見送り (imp{g['importance']}/trk{g['trackable']})"


def main() -> int:
    model = load_detect_model()
    if model is None:
        print("モデルが無い (config/models/detect_model.json)", file=sys.stderr)
        return 1
    gold = {str(g["article_id"]): g for g in _jsonl(GOLD)}
    kinds = {str(r["article_id"]): str(r["kind"]) for f in KIND_FILES for r in _jsonl(f)}
    labels: list[dict[str, Any]] = json.loads(LABELS.read_text(encoding="utf-8"))
    labels.sort(key=lambda r: str(r["run_at"]))
    held = labels[int(len(labels) * _HELD_OUT_RATIO) :]
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in held:
        by_day[str(r["run_at"])[:10]].append(r)
    arts = build_detect_articles(
        RunHistoryRepository(), [str(r["article_id"]) for r in held], kinds
    )
    scores = score_articles(model, dict(arts))
    opened = {str(r["article_id"]) for r in held if r["decision"] == "opened"}

    lines = [
        "# detect ML バックテスト (held-out 25 日、閾値 "
        f"{model.threshold:.3f}、1 日あたり上限 {6} 件)",
        "",
        "各日: ML の選抜 (確率順) と現行の開設。◎ = 両方 / ● = ML だけ / ○ = 現行だけ",
        "",
    ]
    tally: Counter[str] = Counter()
    judged: dict[str, Counter[str]] = defaultdict(Counter)
    for day in sorted(by_day):
        ids = [str(r["article_id"]) for r in by_day[day] if str(r["article_id"]) in scores]
        day_scores = {a: scores[a] for a in ids}
        picks = dict(shadow_select(day_scores, threshold=model.threshold))
        cur = {a for a in ids if a in opened}
        lines.append(f"## {day} — ML {len(picks)} / 現行 {len(cur)} / 候補 {len(ids)}")
        rows = []
        for a in sorted(set(picks) | cur, key=lambda x: -day_scores.get(x, 0)):
            mark = "◎" if a in picks and a in cur else ("●" if a in picks else "○")
            tally[mark] += 1
            g = gold.get(a)
            if g is not None:
                judged[mark][
                    "開設" if g["gold_open"] else ("見張り" if g["watch"] else "見送り")
                ] += 1
            art = arts[a]
            rows.append(
                f"- {mark} p={day_scores.get(a, 0):.2f} [{art.kind}] "
                f"{art.title[:_TITLE]} — {_verdict(g)}"
            )
        lines += rows + [""]
    summary = [
        f"件数: 両方 {tally['◎']} / ML だけ {tally['●']} / 現行だけ {tally['○']}",
        "審判済みの内訳 (開設/見張り/見送り): "
        + " | ".join(f"{m} {dict(judged[m])}" for m in ("◎", "●", "○") if judged[m]),
    ]
    OUT.write_text("\n".join(summary + [""] + lines), encoding="utf-8")
    print("\n".join(summary))
    print(f"書込: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
