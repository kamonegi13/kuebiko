"""A/B 検証 v2 (2026-07-26): 全判断セットの「統合 1 呼び出し」vs「summarizer 実出力」。

v1 は既移設フィールド (stance/intent/subject) で統合 ≈ 現行 focused を確認した。v2 は
**まだ summarizer に残る/暗黙に枯死している判断フィールド** (importance 分布・
victim_sector/country・article_type・primary_actor) について、**summarizer 実出力 (枯死元)
vs 統合分類器** を測り、統合が救済するかを実証する。

importance は充足でなく **分布** で見る (100% 埋まるが low=3% に退化 = 判定放棄の疑い)。
victim/article_type は充足率と分布。primary_actor は救済の再確認 (summarizer 実測 0% 想定)。

usage:
    uv run python scripts/ab_consolidated_judgment.py --n 20
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from pydantic import BaseModel, ConfigDict

from src.config_loader import load_app_config
from src.cti.actor_normalizer import load_actor_aliases
from src.pipeline.dispatch import _load_template
from src.pipeline.persistence import _relevant_actors
from src.pipeline.summary import SummaryOutput
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.llm_client import LLMClient
from src.tools.model_tiers import Step, build_llm_for

_CYBER = ("apt", "malware", "incident", "breach", "advisory", "phishing", "vulnerability")


class _JudgmentOut(BaseModel):
    """統合判断分類器の全判断セット (抽出系は含まない)。"""

    model_config = ConfigDict(extra="ignore")

    importance: str = "medium"
    category: str = "other"
    article_type: str = "breaking"
    editorial_stance: str = "unknown"
    intent: str = "unknown"
    intent_confidence: str = "low"
    technical: str | None = None
    event_date: str | None = None
    victim_sector: str | None = None
    victim_country: str | None = None
    i_infra: bool = False
    subject_actor_id: str = ""


_JUDGMENT_PROMPT = """\
あなたは CTI アナリスト。記事の判断軸を一度に判定し JSON のみ出力する。

# importance (high|medium|low) — 具体的サイバー脅威/被害/脆弱性への直結度で判定
- high: 実際に悪用中(in the wild/KEV)/0day/緊急パッチ命令/重大インシデント。
  **報道だから medium にしない**
- medium: 注視すべき新規 TTP・重要脆弱性(PoC 含)・進行中だが非緊急の脅威
- low: 一般動向・論説・解説・観察対象・脅威含意の薄い記事・プロパガンダ。**該当は必ず low にする**
脆弱性の high は厳格: パッチ済/PoC のみ/CVSS 高いだけ は medium 以下。

# category (1つ): apt/apt_leak/vulnerability/malware/incident/breach/advisory/
policy/geopolitical/research/other

# article_type (1つ): breaking(進行中事案)/advisory(CVE警告)/recap(週次まとめ・Top N)/
tutorial(How to)/research(論文)/press(製品発表)/opinion(論説)。難しければ breaking

# editorial_stance: factual_report(客観報道・既定)/analytical/opinion/propaganda/unknown

# intent (主体の戦略的動機、1つ): espionage/financial/prepositioning/disruption/influence/
hacktivism/coercion/deterrence/territorial/subversion/diplomacy/unknown。
intent_confidence=high|medium|low (弱い証拠でも方向が読めれば最有力+low、材料無い時のみ unknown)

# technical: Capability⇄Infrastructure の技術結線 1 行 (string|null)
# event_date (YYYY-MM-DD|null): 記事が報じる主要事象の日付 (過去参照日は使わない)
# victim_sector (string|null): 被害組織の業種 / victim_country (ISO2|null): 被害国
# i_infra (true|false): 重要インフラ(医療/電力/通信/交通/金融基盤/政府/防衛/OT)への攻撃なら true

# subject_actor_id: 記事が **主題** とするアクターを下の候補 id から 1 つ (主題=記事の主語、
単なる言及・比較は除く)。**候補外は返さない**。曖昧なら空。
候補: {candidates}

# 出力 (JSON のみ)
{{"importance":"","category":"","article_type":"","editorial_stance":"","intent":"",
"intent_confidence":"low","technical":null,"event_date":null,"victim_sector":null,
"victim_country":null,"i_infra":false,"subject_actor_id":""}}

# 記事
カテゴリ(取込時): {category}
タイトル: {title}
本文:
{body}
"""


async def _consolidated(
    llm: LLMClient, *, title: str, body: str, category: str, candidates: list
) -> _JudgmentOut:
    cand_lines = ", ".join(f"{c.id}" for c in candidates) or "(なし)"
    prompt = _JUDGMENT_PROMPT.format(
        candidates=cand_lines, category=category, title=title, body=body[:8000]
    )
    try:
        return await llm.generate_structured(prompt, schema=_JudgmentOut, think=False)
    except Exception as e:  # noqa: BLE001
        print(f"  [consolidated err] {e}")
        return _JudgmentOut()


def _filled(v: object) -> bool:
    s = str(v or "").strip().lower()
    return s not in ("", "unknown", "none", "null", "uncategorized")


async def diagnose(n: int) -> None:
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())
    template = _load_template()
    cyber_ph = ",".join("?" for _ in _CYBER)
    sql = (
        "SELECT a.article_id, a.title, a.body, a.category, MAX(a.created_at) ts "
        "FROM articles a "
        f"WHERE a.category IN ({cyber_ph}) AND a.body IS NOT NULL AND a.body <> '' "
        "AND a.feed_url NOT LIKE ? "
        "GROUP BY a.article_id, a.title, a.body, a.category "
        "ORDER BY ts DESC LIMIT ?"
    )
    with repo._connect() as conn:  # noqa: SLF001
        rows = conn.execute(sql, (*_CYBER, "%ransomware.live%", n)).fetchall()

    imp_sum: Counter[str] = Counter()
    imp_con: Counter[str] = Counter()
    at_sum: Counter[str] = Counter()
    at_con: Counter[str] = Counter()
    fill = {
        k: [0, 0] for k in ("victim_sector", "victim_country", "primary_actor")
    }  # [summarizer, consolidated]
    total = 0

    for r in rows:
        title = str(r["title"] or "")
        body = str(r["body"] or "")
        category = str(r["category"] or "")
        cands = _relevant_actors(registry.find_all(body), category)
        total += 1

        # --- summarizer 実出力 (枯死元) ---
        art = Article(
            id=str(r["article_id"]),
            title=title,
            url="",
            summary_html="",
            published=__import__("datetime").datetime.now(__import__("datetime").UTC),
            feed_title="",
            feed_url="",
        )
        try:
            s = await llm.generate_structured(
                template.render(article=art, body=body), schema=SummaryOutput, think=False
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [summarizer err] {e}")
            continue
        s_rf = s.routing_flags or {}
        imp_sum[s.importance or "?"] += 1
        at_sum[s.article_type or "?"] += 1
        if _filled(s.victim_sector):
            fill["victim_sector"][0] += 1
        if _filled(s.victim_country):
            fill["victim_country"][0] += 1
        if _filled(s_rf.get("primary_actor_id")):
            fill["primary_actor"][0] += 1

        # --- consolidated ---
        c = await _consolidated(llm, title=title, body=body, category=category, candidates=cands)
        imp_con[c.importance or "?"] += 1
        at_con[c.article_type or "?"] += 1
        if _filled(c.victim_sector):
            fill["victim_sector"][1] += 1
        if _filled(c.victim_country):
            fill["victim_country"][1] += 1
        c_subj = c.subject_actor_id if c.subject_actor_id in {x.id for x in cands} else ""
        if _filled(c_subj):
            fill["primary_actor"][1] += 1

    print(f"\n=== A/B v2: summarizer 実出力 vs 統合分類器 (n={total}) ===\n")
    print("importance 分布 (low の退化が回復するか):")
    print(f"  summarizer : {dict(imp_sum)}")
    print(f"  consolidated: {dict(imp_con)}")
    print("\narticle_type 分布 (breaking への流し込み退化):")
    print(f"  summarizer : {dict(at_sum)}")
    print(f"  consolidated: {dict(at_con)}")
    print("\n充足率 (summarizer → consolidated):")
    for k, (sm, co) in fill.items():
        print(f"  {k:16s} {sm}/{total} → {co}/{total}")
    print(
        "\n判定: importance の low が回復し / victim・primary_actor の充足が上がれば"
        "\n      統合分類器が『暗黙の枯死』も救済する = 全判断セットを統合してよい。"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20)
    args = parser.parse_args()
    asyncio.run(diagnose(args.n))


if __name__ == "__main__":
    main()
