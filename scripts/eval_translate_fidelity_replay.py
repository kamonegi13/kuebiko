"""翻訳の化けの関門 (TRANSLATE_FIDELITY_GATE) を過去の訳に当てる再現 (読み取り専用)。

本番 DB には書かない。入力は psql で書き出した JSONL (1 行 = article_id, body, body_ja)。
書き出し (同じ article_id は複数行ありうるので distinct、直近 N 日):

  docker exec postgres psql -U kuebiko -d kuebiko -At -c "SELECT row_to_json(t) FROM (
    SELECT DISTINCT ON (article_id) article_id, body, body_ja FROM articles
    WHERE created_at > now() - interval '14 days' AND body IS NOT NULL
      AND body_ja IS NOT NULL AND body_ja <> ''
    ORDER BY article_id, created_at DESC) t" > export.jsonl

使い方: python scripts/eval_translate_fidelity_replay.py export.jsonl
        [--examples 5] [--dump-hits hits.jsonl]

単位: 本番は ``split_for_translation`` で分けたチャンクごとに判定する。1 チャンクの記事は
本番と同一の判定。複数チャンクの記事は、チャンク別の訳が残らない (連結後の body_ja のみ)
ため記事全体を 1 単位として当てる近似 (``approx_multi_chunk`` に件数を出す)。
消える割合 = その記事で 1 つでも該当すれば訳を保存しない (1 チャンクでも化ければ全体を捨てる)。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.cti.body_translator import should_block_translation, split_for_translation
from src.cti.translation_fidelity import GarbleReport, detect_garbling

MODES = ("on", "ident", "shadow")
KINDS = ("word", "ident", "number")
SNIPPET_RADIUS = 70


def _snippet(text: str, needle: str | None) -> str:
    if not needle:
        return ""
    pos = text.find(needle)
    if pos < 0:
        return ""
    lo = max(0, pos - SNIPPET_RADIUS)
    return text[lo : pos + len(needle) + SNIPPET_RADIUS].replace("\n", " ")


def _hits(report: GarbleReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"kind": "word", "broken": h.broken, "candidate": h.repaired} for h in report.word_hits
    ]
    rows += [
        {"kind": "ident", "broken": h.broken, "candidate": h.repaired, "ident_kind": h.kind}
        for h in report.identifier_hits
    ]
    rows += [
        {"kind": "number", "broken": h.broken, "candidate": h.nearest_source}
        for h in report.number_flags
    ]
    return rows


def replay(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """各記事を判定して集計と全ヒットを返す。"""
    kind_articles: Counter[str] = Counter()
    blocked_articles: Counter[str] = Counter()
    garbled = 0
    approx_multi = 0
    all_hits: list[dict[str, Any]] = []
    for r in rows:
        body, body_ja = str(r["body"]), str(r["body_ja"])
        multi = len(split_for_translation(body)) > 1
        approx_multi += int(multi)
        report = detect_garbling(body, body_ja)
        if not report.is_garbled:
            continue
        garbled += 1
        for kind, n in zip(
            KINDS,
            (len(report.word_hits), len(report.identifier_hits), len(report.number_flags)),
            strict=True,
        ):
            kind_articles[kind] += int(n > 0)
        for mode in MODES:
            blocked_articles[mode] += int(should_block_translation(mode, report))
        for h in _hits(report):
            all_hits.append(
                {
                    "article_id": r["article_id"],
                    "multi_chunk": multi,
                    **h,
                    "source_snippet": _snippet(body, h["candidate"]),
                    "output_snippet": _snippet(body_ja, h["broken"]),
                }
            )
    total = len(rows)

    def pct(n: int) -> float:
        return round(100 * n / total, 1) if total else 0.0

    summary = {
        "articles": total,
        "approx_multi_chunk": approx_multi,
        "garbled_any": {"n": garbled, "pct": pct(garbled)},
        "by_kind_articles": {
            k: {"n": kind_articles[k], "pct": pct(kind_articles[k])} for k in KINDS
        },
        "dropped_if_mode": {
            m: {"n": blocked_articles[m], "pct": pct(blocked_articles[m])} for m in MODES
        },
    }
    return summary, all_hits


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("input", help="psql で書き出した JSONL")
    ap.add_argument("--examples", type=int, default=5, help="種類ごとの例の最大件数")
    ap.add_argument("--dump-hits", help="全ヒット (前後の文脈つき) を JSONL で書き出す先")
    args = ap.parse_args()

    rows = [
        json.loads(line)
        for line in Path(args.input).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summary, hits = replay(rows)
    examples = {
        k: [
            {"article_id": h["article_id"], "broken": h["broken"], "candidate": h["candidate"]}
            for h in [x for x in hits if x["kind"] == k][: args.examples]
        ]
        for k in KINDS
    }
    print(json.dumps({**summary, "examples": examples}, ensure_ascii=False, indent=2))
    if args.dump_hits:
        Path(args.dump_hits).write_text(
            "".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hits), encoding="utf-8"
        )


if __name__ == "__main__":
    main()
