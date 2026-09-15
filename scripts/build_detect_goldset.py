#!/usr/bin/env python3
"""detect の凍結審判を**現行非依存**で作り直す (2026-09-15)。

なぜ作り直すか: 既存の `data/mlx/detect_labels.json` は「開設されたか / 後から遅延開設
されたか」で正例を付けており、**現行の挙動から作られた採点表**になっている。開設した事象は
証拠が集まるので正例に見えやすく、開設しなかったものは遅延開設された場合しか正例にならない
→ 現行が構造的に有利。実測でもこの採点表では独立採点が 7.5% 対 13.0% で負けるが、**中身を
読むと独立採点は CVSS 10 の Entra ID RCE・NEC ルータの認証欠如・Citrix Pre-Auth RCE を
拾っており、採点表の「不正解」が CTI 的には重い**。採点表の方が疑わしい
(2026-09-04 の「現状を真値に置くバイアス」と同型)。

設計:
- **母集団は 3 層**: ①現行が開設した記事 ②独立採点の上位 ③**それ以外からの無作為抽出**。
  ③が無いと「両方が取りこぼしたもの」が見えず、2 方式の優劣しか測れない。
- **審判は盲検**: どちらの方式が選んだか・現行が開設したかを審判に見せない。
  記事の見出しと要約だけで判定させる。
- **審判に聞かないこと**は聞かない: ①初報か続報か ②既存事象へ吸収すべきか — どちらも
  `situation_evidence` の割当履歴から**データで導出できる**。審判に聞くと能動情勢 135 件の
  タイトルを毎回 prompt に載せることになり (~3.6k tok × 190 件)、CoT 収穫より高くつく。
- **集計は事象単位で行う** (記事単位ではない)。同一事象の記事が 3 本あれば 1 本開けば
  正解で、残り 2 本を「見逃し」と数えてはいけない
  (2026-08-31「ペア単位 88% を製品の成績として語らない — 群単位 83%」と同型)。
- 外部 LLM を使う (利用者指示により**水曜夜以降の枠**で実行)。既定は dry-run。

使い方:
    docker exec kuebiko python scripts/build_detect_goldset.py              # dry-run (枠を使わない)
    docker exec -e LLM_LOCAL_FALLBACK=0 kuebiko python \\
        scripts/build_detect_goldset.py --model claudecode:sonnet --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config_loader import load_app_config  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402
from src.tools.model_tiers import Step, build_llm_for_ref  # noqa: E402

LABELS = Path("data/mlx/detect_labels.json")
LLM_FEATS = Path("data/mlx/detect_llm_feats.jsonl")
OUT = Path("data/mlx/detect_goldset.jsonl")
_HELD_OUT_RATIO = 0.7  # 時系列分割: 後ろ 30% を held-out に使う
#: ③ 無作為抽出の件数。**両方が取りこぼしたもの**の割合を推定するための層なので、
#: 少なすぎると見逃し率が推定できない (60 件だと正例 3-6 件で誤差が支配的)。
#: 1 件あたり ~500 tok と安いため広めに取る。
_RANDOM_SAMPLE = 120
_SEED = 13

_SYSTEM = (
    "あなたは日本の CTI アナリストです。1 本の記事について、"
    "**情勢台帳に新規の追跡事象 (Situation) を開くべきか**を判定します。"
)

_TEMPLATE = """# 任務
日本標的の脅威を漏らさず検知する。優先関心: 中国/北朝鮮/ロシア/イランの国家系活動、
日本の重要インフラ・企業への標的化、サプライチェーン、悪用が始まった重大脆弱性。

# 判定する記事
見出し: {title}
要約: {summary}

# 問い — **2 つの軸を別々に**答えること

台帳の追跡枠は有限 (現在 active 135 件・再評価は 1 回 12 件) なので、
「重要か」と「追跡単位になるか」を分けて判定する。

1. **importance** (重要度 0-3): 任務に照らしてこの事象はどれだけ重いか。
   影響範囲・深刻度・日本との関係で測る。**続報の有無とは無関係**。

2. **trackable** (追跡価値 0-3): この事象は**この先いくつかの続報が積み上がり、
   確度や見立てが動く**か。
   - 3 = 被害範囲・原因・帰属が後から判明していく (侵害インシデント、進行中のキャンペーン)
   - 2 = 悪用が始まっており、範囲や被害が広がりうる
   - 1 = 単発で完結しうるが、続報があれば価値がある
   - 0 = **1 本で完結する** (勧告・パッチ公開・分析記事・制裁発表・統計・意見記事)。
     重要でも 0 になりうる — CVSS 10 の脆弱性でも、悪用が観測されず勧告だけなら
     追跡単位にはならない。

3. **open**: 上記を踏まえ、**いま新しい追跡事象を開くべきか** (true/false)。
   枠が有限である以上、重要でも追跡単位にならないものは開かない。

4. **watch** (true/false): いまは追跡単位にならないが、**後に悪用・被害・帰属が観測されたら
   追跡すべき事象になる素地**があるか。
   - true の典型: 重大脆弱性の勧告・PoC 公開・攻撃手法の研究公表 —
     悪用が観測されれば、そのとき初めて追跡単位になる
   - false の典型: 制裁・統計・意見記事・既に終わった事案の分析 —
     この先何が起きても追跡単位にはならない
   ⚠ open=false でも watch=true はありうる (**見送り**と**見張り**は別)。

# 判定の前提
- **この記事が初報か続報かは判定しなくてよい** (別途データから導出する)。
  記事が述べている**事象そのもの**に追跡価値があるかを答えること。
- 判断の理由を 1 文で述べ、確信度も返すこと。
"""


class _Verdict(BaseModel):
    """審判の判定。**重要度と追跡価値を分けて持つ**のが肝。

    現行 detect も独立採点も、基準は書かれているのが重要度 (PIR 直結 > 日本関連 > …、
    mission_fit / severity) で、「この先続報が積み上がるか」を問う軸を持っていない。
    2026-09-15 の中身精査では、独立採点が CVSS 10 の勧告を選び、現行が小さいが続報の
    積み上がる侵害を選んでいた — **この差が測れるラベルでないと方式を比べられない**。
    """

    model_config = ConfigDict(extra="ignore")

    importance: int = 0  # 0-3 任務に照らした重さ (続報の有無とは無関係)
    trackable: int = 0  # 0-3 続報が積み上がり見立てが動くか (0 = 1 本で完結)
    open: bool = False
    #: 見送りと見張りの区別。勧告・PoC は今は追跡単位にならないが、悪用が観測されれば
    #: なる。これが無いと「見送ってよい」と「見張るべき」が同じラベルに潰れ、watch 層
    #: (CVE の見張り) を作るべきかの判断材料が永久に失われる。
    watch: bool = False
    reason: str = ""
    confidence: str = "low"  # high | moderate | low


def _held_out() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(LABELS.read_text(encoding="utf-8"))
    rows.sort(key=lambda r: str(r["run_at"]))
    return rows[int(len(rows) * _HELD_OUT_RATIO) :]


def _scores() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for line in LLM_FEATS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            out[str(row["article_id"])] = row
    return out


def select_population(limit_random: int = _RANDOM_SAMPLE) -> list[dict[str, Any]]:
    """3 層の母集団 (現行の開設 / 独立採点の上位 / 無作為) を層ラベルつきで返す。

    層ラベルは**集計用であって審判には見せない** (盲検)。
    """
    test = _held_out()
    feats = _scores()
    opened = [r for r in test if r["decision"] == "opened"]
    ranked = sorted(
        (r for r in test if r["article_id"] in feats),
        key=lambda r: (
            feats[r["article_id"]]["open_score"],
            feats[r["article_id"]]["mission_fit"],
            feats[r["article_id"]]["severity"],
        ),
        reverse=True,
    )
    top = ranked[: len(opened)]
    picked = {r["article_id"] for r in (*opened, *top)}
    rest = [r for r in test if r["article_id"] not in picked]
    rng = random.Random(_SEED)
    sample = rng.sample(rest, min(limit_random, len(rest)))

    strata: dict[str, str] = {}
    for r in opened:
        strata[r["article_id"]] = "incumbent"
    for r in top:
        strata[r["article_id"]] = (
            "both" if strata.get(r["article_id"]) == "incumbent" else "scoring"
        )
    for r in sample:
        strata[r["article_id"]] = "random"
    return [{**r, "stratum": strata[r["article_id"]]} for r in (*opened, *top, *sample)]


def _articles(repo: RunHistoryRepository, ids: list[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    with repo._connect() as conn:  # noqa: SLF001 — 評価スクリプトの接続 seam 共有
        for i in range(0, len(ids), 200):
            chunk = ids[i : i + 200]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT article_id AS a, title AS t, summary AS s "  # noqa: S608 — 定数 IN 展開
                f"FROM articles WHERE article_id IN ({placeholders})",
                tuple(chunk),
            ).fetchall()
            for row in rows:
                out[str(row["a"])] = (str(row["t"] or ""), str(row["s"] or ""))
    return out


async def main_async(args: argparse.Namespace) -> int:
    population = select_population(args.random_sample)
    seen = {
        json.loads(line)["article_id"]
        for line in (OUT.read_text(encoding="utf-8").splitlines() if OUT.exists() else [])
        if line.strip()
    }
    repo = RunHistoryRepository()
    texts = _articles(repo, [str(r["article_id"]) for r in population])

    from collections import Counter

    strata = Counter(r["stratum"] for r in population)
    todo = [r for r in population if r["article_id"] not in seen and r["article_id"] in texts]
    print(
        f"母集団 {len(population)} 件 {dict(strata)} / 本文あり {len(texts)} / 未判定 {len(todo)}"
    )
    if not args.apply:
        print("\n(dry-run — 外部枠を使わない。実行は --model <ref> --apply)")
        return 0

    llm = build_llm_for_ref(args.model, Step.SYNTHESIS_DETECT, load_app_config())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    ok = failed = 0
    with OUT.open("a", encoding="utf-8") as fh:
        for row in todo:
            title, summary = texts[str(row["article_id"])]
            prompt = f"{_SYSTEM}\n\n{_TEMPLATE.format(title=title, summary=summary[:600])}"
            try:
                verdict = await llm.generate_structured(
                    prompt, _Verdict, temperature=0.0, max_tokens=400, think=False
                )
            except Exception as exc:  # noqa: BLE001 — 1 件の失敗で全体を落とさない
                failed += 1
                print(f"  FAIL {row['article_id'][:40]} {type(exc).__name__}", flush=True)
                continue
            fh.write(
                json.dumps(
                    {
                        "article_id": row["article_id"],
                        "stratum": row["stratum"],
                        "gold_open": verdict.open,
                        "importance": verdict.importance,
                        "trackable": verdict.trackable,
                        "watch": verdict.watch,
                        "reason": verdict.reason,
                        "confidence": verdict.confidence,
                        "legacy_label": row["label"],
                        "legacy_decision": row["decision"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
            ok += 1
    print(f"\n完了: 判定 {ok} / 失敗 {failed} → {OUT}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--model", default="claudecode:sonnet", help="審判モデル ref")
    ap.add_argument("--random-sample", type=int, default=_RANDOM_SAMPLE)
    ap.add_argument("--apply", action="store_true", help="外部 LLM を呼ぶ (既定は dry-run)")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
