#!/usr/bin/env python3
"""対の判定の「同じ入力の対照」(P0-08) — **評価の夜に GPU で回す。書くだけで、まだ回さない**。

凍結した組を、保存済みの同じ要約・見出しの文で複数のモデルに当て、モデル間の一致率・
各モデルの ``llm_same`` の率・pair_model を通した ``ml_joined``・群の単位の結果を出す。
モデルは**1 本ずつ直列** (同時に 2 本を載せない)。入力が揃わない組は除外して件数を報告する。
本番のコード・DB は変えない (SELECT のみ)。

入力 (JSONL、1 行 1 組): ``{"left_id": "<記事 id>", "right_id": "<記事 id>"}``
(事象 id の組は、代表記事の id へ先に写してから渡す)

入力の作り方 (118 組): scripts/gpu_window/build_pair_input.py。
出力: ``data/eval/pair_control/<stamp>.json`` (率・一致率) と ``<stamp>_pairs.jsonl``
(組ごと・モデルごとの llm_same / ml_joined。id のみ。McNemar 用)。

使い方 (評価の夜)。**使い捨てのコンテナで** (ホストの uv は DATABASE_URL が無く SQLite に落ちる。
Ollama は host.docker.internal 経由。本番のコンテナの中では回さない — CLAUDE.md §7):
    docker compose run --rm --no-deps -T kuebiko python scripts/gpu_window/build_pair_input.py
    docker compose run --rm --no-deps -T kuebiko python scripts/pair_same_input_control.py \
        --pairs data/eval/pair_control/pairs_s23i.jsonl \
        --models kuebiko-sft:s23i,kuebiko-sft:s24ci65,gemma4:26b
DB は ``readonly_conn.open_read_only`` で読み取り専用 (SELECT のみ)。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

JST = ZoneInfo("Asia/Tokyo")
OUT_DIR = Path("data/eval/pair_control")
_CHUNK = 400

Judge = Callable[[str], Awaitable[bool | None]]
JudgeFactory = Callable[[str], Judge]


@dataclass(frozen=True)
class PairInput:
    left_id: str
    right_id: str


@dataclass(frozen=True)
class ArticleText:
    title: str
    summary: str
    feed_title: str


def load_pairs(path: Path) -> list[PairInput]:
    """JSONL を読む。形式が壊れた行は黙って捨てず、行番号つきで失敗させる。"""
    pairs: list[PairInput] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
            pairs.append(PairInput(str(raw["left_id"]), str(raw["right_id"])))
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError(f"{path}:{n} 組の形式が不正です: {e}") from e
    return pairs


def fetch_texts(conn: Any, ids: Sequence[str]) -> dict[str, ArticleText]:
    """保存済みの見出し・要約・媒体を引く (SELECT のみ)。要約が空の記事は含めない。"""
    out: dict[str, ArticleText] = {}
    unique = sorted(set(ids))
    for i in range(0, len(unique), _CHUNK):
        chunk = unique[i : i + _CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = conn.execute(
            "SELECT article_id, title, summary, feed_title FROM articles"
            f" WHERE article_id IN ({marks})",
            tuple(chunk),
        ).fetchall()
        for aid, title, summary, feed in rows:
            if summary and str(summary).strip():
                out[str(aid)] = ArticleText(str(title or ""), str(summary), str(feed or ""))
    return out


def split_usable(
    pairs: Sequence[PairInput], texts: Mapping[str, ArticleText]
) -> tuple[list[PairInput], int]:
    """両側の入力が揃った組と、除外した組の数。"""
    usable = [p for p in pairs if p.left_id in texts and p.right_id in texts]
    return usable, len(pairs) - len(usable)


def build_pair_prompt(left: ArticleText, right: ArticleText) -> str:
    from src.eventnews.pair_judge import build_prompt

    return build_prompt(
        left.title, left.summary, left.feed_title, right.title, right.summary, right.feed_title
    )


async def run_models(
    models: Sequence[str],
    pairs: Sequence[PairInput],
    texts: Mapping[str, ArticleText],
    make_judge: JudgeFactory,
) -> dict[str, list[bool | None]]:
    """モデルを**1 本ずつ直列**に当てる (次のモデルは前のモデルの全件が終わってから)。"""
    results: dict[str, list[bool | None]] = {}
    for model in models:
        judge = make_judge(model)
        verdicts: list[bool | None] = []
        for p in pairs:
            verdicts.append(await judge(build_pair_prompt(texts[p.left_id], texts[p.right_id])))
        results[model] = verdicts
    return results


def agreement(a: Sequence[bool | None], b: Sequence[bool | None]) -> tuple[int, float | None]:
    """両方が判定できた組での一致率 (件数, 率)。"""
    both = [(x, y) for x, y in zip(a, b, strict=True) if x is not None and y is not None]
    if not both:
        return 0, None
    return len(both), sum(1 for x, y in both if x == y) / len(both)


def same_rate(verdicts: Sequence[bool | None]) -> float | None:
    known = [v for v in verdicts if v is not None]
    return sum(known) / len(known) if known else None


def group_summary(pairs: Sequence[PairInput], joined: Sequence[bool | None]) -> dict[str, int]:
    """繋がった組から連結成分を作り、群の数と最大の大きさを返す (群の単位の結果)。"""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p, j in zip(pairs, joined, strict=True):
        find(p.left_id)
        find(p.right_id)
        if j:
            parent[find(p.left_id)] = find(p.right_id)
    sizes: dict[str, int] = {}
    for node in list(parent):
        root = find(node)
        sizes[root] = sizes.get(root, 0) + 1
    multi = [s for s in sizes.values() if s > 1]
    return {"groups": len(multi), "largest": max(multi, default=0), "nodes": len(parent)}


def ml_joined_for(
    model: Any, features: Mapping[str, float] | None, llm_same: bool | None
) -> bool | None:
    """保存済みの特徴 + この対照の ``llm_same`` で pair_model を通す。特徴が無ければ None。"""
    if model is None or features is None:
        return None
    names = model.feature_names[:-2]
    if any(n not in features for n in names):
        return None
    vec = [float(features[n]) for n in names] + [
        1.0 if llm_same else 0.0,
        0.0 if llm_same is None else 1.0,
    ]
    return bool(model.joins(vec, llm_available=llm_same is not None))


def fetch_shadow_features(conn: Any, p: PairInput) -> dict[str, float] | None:
    row = conn.execute(
        "SELECT features_json FROM event_pair_shadow"
        " WHERE (left_id=? AND right_id=?) OR (left_id=? AND right_id=?)"
        " ORDER BY observed_at DESC LIMIT 1",
        (p.left_id, p.right_id, p.right_id, p.left_id),
    ).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return {k: float(v) for k, v in raw.items() if isinstance(v, int | float)}


def summarize(
    models: Sequence[str],
    pairs: Sequence[PairInput],
    excluded: int,
    verdicts: Mapping[str, Sequence[bool | None]],
    ml: Mapping[str, Sequence[bool | None]],
) -> dict[str, Any]:
    return {
        "pairs_used": len(pairs),
        "pairs_excluded_no_input": excluded,
        "per_model": {
            m: {
                "llm_same_rate": same_rate(verdicts[m]),
                "llm_failed": sum(1 for v in verdicts[m] if v is None),
                "ml_joined_rate": same_rate(ml[m]),
                "groups": group_summary(
                    pairs, ml[m] if any(v is not None for v in ml[m]) else verdicts[m]
                ),
            }
            for m in models
        },
        "agreement": {
            f"{a}|{b}": dict(zip(("n", "rate"), agreement(verdicts[a], verdicts[b]), strict=True))
            for a, b in itertools.combinations(models, 2)
        },
    }


def per_pair_records(
    models: Sequence[str],
    pairs: Sequence[PairInput],
    verdicts: Mapping[str, Sequence[bool | None]],
    ml: Mapping[str, Sequence[bool | None]],
) -> list[dict[str, Any]]:
    """組ごと・モデルごとの判定 (McNemar 用)。**id と判定だけ** — 本文・要約は持たない。"""
    return [
        {
            "left_id": p.left_id,
            "right_id": p.right_id,
            "llm_same": {m: verdicts[m][i] for m in models},
            "ml_joined": {m: ml[m][i] for m in models},
        }
        for i, p in enumerate(pairs)
    ]


def write_per_pair(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )


def _real_judge_factory() -> JudgeFactory:
    from src.config_loader import load_app_config
    from src.eventnews.pair_judge import judge_pair
    from src.tools.model_tiers import Step, build_llm_for_ref

    config = load_app_config()

    def make(model: str) -> Judge:
        llm = build_llm_for_ref(model, Step.PAIR_JUDGE, config)

        async def judge(prompt: str) -> bool | None:
            return await judge_pair(llm, prompt)

        return judge

    return make


async def _amain(args: argparse.Namespace) -> int:
    from readonly_conn import open_read_only

    from src.eventnews import pair_model
    from src.storage.db_backend import connect
    from src.storage.run_history import DEFAULT_DB_PATH

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    pairs = load_pairs(Path(args.pairs))
    conn = open_read_only(connect, DEFAULT_DB_PATH)
    try:
        texts = fetch_texts(conn, [i for p in pairs for i in (p.left_id, p.right_id)])
        usable, excluded = split_usable(pairs, texts)
        shadow = {(p.left_id, p.right_id): fetch_shadow_features(conn, p) for p in usable}
    finally:
        conn.close()
    verdicts = await run_models(models, usable, texts, _real_judge_factory())
    pm = pair_model.load_model()
    ml = {
        m: [
            ml_joined_for(pm, shadow[(p.left_id, p.right_id)], v)
            for p, v in zip(usable, verdicts[m], strict=True)
        ]
        for m in models
    }
    report = summarize(models, usable, excluded, verdicts, ml)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(JST).strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / f"{stamp}.json").write_text(text, encoding="utf-8")
    write_per_pair(OUT_DIR / f"{stamp}_pairs.jsonl", per_pair_records(models, usable, verdicts, ml))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--models", default="kuebiko-sft:s23i,kuebiko-sft:s24ci65,gemma4:26b")
    return asyncio.run(_amain(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
