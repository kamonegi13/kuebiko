"""統合判断分類器の出力を実データで目視確認する (2026-07-26)。

充足率・分布でなく **中身の正誤** を確認するためのダンプ。各記事のタイトルと、統合分類器の
全判断出力 + summarizer の該当値 (article_type / primary_actor) を並べて表示する。
分類が妥当か (article_type が本当に advisory/recap か、subject が主題として正しいか、
victim/intent が本文と整合するか) を人が読んで判断する。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

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
    model_config = ConfigDict(extra="ignore")

    importance: str = "medium"
    category: str = "other"
    article_type: str = "breaking"
    editorial_stance: str = "unknown"
    intent: str = "unknown"
    intent_confidence: str = "low"
    technical: str | None = None
    event_date: str | None = None
    i_infra: bool = False
    subject_actor_id: str = ""


_JUDGMENT_PROMPT = """\
あなたは CTI アナリスト。記事の判断軸を一度に判定し JSON のみ出力する。

# importance (high|medium|low) — 具体的サイバー脅威/被害/脆弱性への直結度で判定
- high: 実際に悪用中(in the wild/KEV)/0day/緊急パッチ命令/重大インシデント。
  **報道だから medium にしない**
- medium: 注視すべき新規 TTP・重要脆弱性(PoC 含)・進行中だが非緊急の脅威
- low: 一般動向・論説・解説・観察対象・脅威含意の薄い記事・プロパガンダ。**該当は必ず low**
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
# i_infra (true|false): 重要インフラ(医療/電力/ガス/水道/通信/交通/物流/金融基盤/政府/防衛/
半導体/OT・ICS)への攻撃・防護・政策に **実質的に** 関わるなら true。**一般企業の IT breach・
個別 CVE 解説・SaaS 脆弱性 (重要インフラでの運用が明示されない限り) は false**

# subject_actor_id: 記事が **主題** とするアクターを下の候補 id から 1 つ (主題=記事の主語、
単なる言及・比較は除く)。**候補外は返さない**。曖昧なら空。
候補: {candidates}

# 出力 (JSON のみ)
{{"importance":"","category":"","article_type":"","editorial_stance":"","intent":"",
"intent_confidence":"low","technical":null,"event_date":null,"i_infra":false,
"subject_actor_id":""}}

# 記事
カテゴリ(取込時): {category}
タイトル: {title}
本文:
{body}
"""


async def _consolidated(
    llm: LLMClient, *, title: str, body: str, category: str, candidates: list
) -> _JudgmentOut:
    cand_lines = ", ".join(c.id for c in candidates) or "(なし)"
    prompt = _JUDGMENT_PROMPT.format(
        candidates=cand_lines, category=category, title=title, body=body[:8000]
    )
    return await llm.generate_structured(prompt, schema=_JudgmentOut, think=False)


async def dump(n: int) -> None:
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

    for i, r in enumerate(rows, 1):
        title = str(r["title"] or "")
        body = str(r["body"] or "")
        category = str(r["category"] or "")
        cands = _relevant_actors(registry.find_all(body), category)
        cand_ids = ",".join(c.id for c in cands) or "-"
        art = Article(
            id=str(r["article_id"]),
            title=title,
            url="",
            summary_html="",
            published=datetime.now(UTC),
            feed_title="",
            feed_url="",
        )
        try:
            s = await llm.generate_structured(
                template.render(article=art, body=body), schema=SummaryOutput, think=False
            )
            c = await _consolidated(llm, title=title, body=body, category=category, candidates=cands)
        except Exception as e:  # noqa: BLE001
            print(f"[{i}] err: {e}")
            continue
        s_pid = str((s.routing_flags or {}).get("primary_actor_id") or "") or "-"
        print(f"\n[{i}] {title[:78]}")
        print(f"    取込cat={category} | 言及候補=[{cand_ids}]")
        print(
            f"    統合: imp={c.importance} cat={c.category} type={c.article_type} "
            f"stance={c.editorial_stance} intent={c.intent}/{c.intent_confidence}"
        )
        print(
            f"          i_infra={c.i_infra} subject={c.subject_actor_id or '-'} "
            f"event={c.event_date or '-'}"
        )
        print(f"    summ: type={s.article_type} primary={s_pid} imp={s.importance}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(dump(args.n))


if __name__ == "__main__":
    main()
