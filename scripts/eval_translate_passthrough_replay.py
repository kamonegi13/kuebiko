"""翻訳の素通し (TRANSLATE_RETAIN_PASSTHROUGH) の決定論の再現 (LLM・GPU なし、件数のみを出す)。

入力: data/eval/passk_tr_1116 (11-16 の 120 窓の原文・n20r 2 標本・n20rd 1 標本・Opus 判定)。
  PYTHONPATH=. uv run python scripts/eval_translate_passthrough_replay.py [--dir data/eval/passk_tr_1116]
出力: 窓の数・保持行つきの窓・LLM を呼ばなくなる窓・URL/識別子の重い誤りのうち、新経路が
構造で除ける数 (原文の URL がすべて原文どおり戻る / LLM を呼ばず創作が起きない)。本文は出さない。
判定の標本の並びは judge_tr1116c.py と同じ種 (tr1116c:<key>) で復元する。
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from src.cti.translate_passthrough import ProtectedChunk, protect_chunk

#: 識別子・URL まわりの誤りとみなす判定メモの語
_NOTE_RE = re.compile(r"URL|識別子|IoC|IOC|ハッシュ|IP|ID|ドメイン|リンク", re.IGNORECASE)
_URL_RE = re.compile(r"(?:https?|hxxps?)://[^\s<>\"'「」）)\]]+", re.IGNORECASE)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _ordered_samples(
    key: str, n20r: list[dict[str, Any]], rd: dict[str, Any]
) -> list[dict[str, Any]]:
    """判定時の並び (judge_tr1116c.py の shuffle) を復元した標本。"""
    pool = [("n20r", r) for r in sorted(n20r, key=lambda r: r["sample"])] + [("n20rd", rd)]
    random.Random(f"tr1116c:{key}").shuffle(pool)
    return [r for _, r in pool]


def source_urls(source: str, protected: ProtectedChunk) -> list[str]:
    """原文の URL (保持行の連なりの中のものと散文中のもの)。"""
    urls = [u for _, u in protected.inline_urls]
    for run in protected.runs:
        urls.extend(_URL_RE.findall(run))
    return urls


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/eval/passk_tr_1116")
    d = Path(ap.parse_args().dir)
    inputs = {r["key"]: r for r in _read_jsonl(d / "translate_inputs.jsonl")}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for r in _read_jsonl(d / "translate_tr1116.samples.jsonl"):
        by_key.setdefault(r["key"], []).append(r)
    rd = {r["key"]: r for r in _read_jsonl(d / "translate_tr1116_n20rd.samples.jsonl")}
    verdicts = json.loads((d / "translate_tr1116c.verdicts.json").read_text())["res"]

    c: Counter[str] = Counter()
    for key, inp in inputs.items():
        p = protect_chunk(inp["source"])
        c["windows"] += 1
        c["with_tokens"] += p.has_tokens
        c["with_retained_runs"] += bool(p.runs)
        c["with_inline_urls"] += bool(p.inline_urls)
        c["would_skip_llm"] += p.should_skip_llm
        if key not in verdicts or key not in rd:
            continue
        samples = _ordered_samples(key, by_key.get(key, []), rd[key])
        urls = source_urls(inp["source"], p)
        for v in verdicts[key]["samples"]:
            out = samples[int(v["sample"])]["output"]
            c["samples"] += 1
            lost = any(u not in out for u in urls)
            c["samples_with_url_lost"] += lost
            if not v.get("heavy_error"):
                continue
            c["heavy"] += 1
            if not _NOTE_RE.search(str(v.get("note", ""))) and not (p.has_tokens and lost):
                continue
            c["heavy_url_related"] += 1
            if p.should_skip_llm:
                c["heavy_url_related_removed_by_skip"] += 1
            elif p.has_tokens and lost:
                c["heavy_url_related_removed_by_restore"] += 1
            else:
                c["heavy_url_related_not_affected"] += 1
    for k, v in sorted(c.items()):
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
