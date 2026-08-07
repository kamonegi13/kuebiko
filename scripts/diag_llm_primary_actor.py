"""診断 (2026-07-26): summarizer の primary_actor_id が実際に出るか標本検証する。

蓄積を待たず、本文現存記事で summarizer を再実行して routing_flags.primary_actor_id の
産出を直接測る。intent/diamond が「過負荷 summarizer の末尾フィールド枯死」で focused
分類器に移された前例があり、primary_actor_id も同病の疑い — これを実証/反証する。

対象標本 = 「本文にアクター名があるのに title には無い」cyber 記事 (= LLM 層が本来
拾うべき本文主題型)。summarizer を回し、primary_actor_id の産出率・確度・辞書解決率・
言及所属率 (= 主題化されたはずか) を集計する。LLM 呼び出しは標本数だけ (既定 30)。

usage:
    uv run python scripts/diag_llm_primary_actor.py            # 30 件
    uv run python scripts/diag_llm_primary_actor.py --n 50
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from src.config_loader import load_app_config
from src.cti.actor_normalizer import load_actor_aliases
from src.cti.subject_actor import resolve_actor_by_name
from src.pipeline.dispatch import _load_template
from src.pipeline.summary import SummaryOutput
from src.storage.run_history import RunHistoryRepository
from src.tools.article_model import Article
from src.tools.model_tiers import Step, build_llm_for

_CYBER = ("apt", "malware", "incident", "incident_breach", "breach", "advisory", "phishing")


async def diagnose(n: int) -> None:
    repo = RunHistoryRepository()
    registry = load_actor_aliases()
    # 本文にアクター言及 (entity) があり、title には名前が無く、主題未確定の cyber 記事
    cyber_ph = ",".join("?" for _ in _CYBER)
    sql = (
        "SELECT a.article_id, a.title, a.body, a.category, MAX(a.created_at) AS ts "
        "FROM articles a "
        "JOIN article_entities ae ON ae.article_id = a.article_id AND ae.entity_type='actor' "
        f"WHERE a.category IN ({cyber_ph}) "
        "AND a.body IS NOT NULL AND a.body <> '' "
        "AND (a.subject_actor_ids IS NULL OR a.subject_actor_ids = '') "
        "GROUP BY a.article_id, a.title, a.body, a.category "
        "ORDER BY ts DESC LIMIT ?"
    )
    with repo._connect() as conn:  # noqa: SLF001 (intra-tool script)
        rows = conn.execute(sql, (*_CYBER, n)).fetchall()

    # title にアクター名が無いものだけ残す (title 層で拾えない = LLM 層が本来の担当)
    targets = []
    for r in rows:
        title = str(r["title"] or "")
        if registry.find(title) is None:
            targets.append(r)

    llm = build_llm_for(Step.ARTICLE_SUMMARY, load_app_config())
    template = _load_template()

    produced = resolved = in_mention = 0
    samples: list[str] = []
    for r in targets:
        aid = str(r["article_id"])
        body = str(r["body"] or "")
        art = Article(
            id=aid,
            title=str(r["title"] or ""),
            url="",
            summary_html="",
            published=datetime.now(UTC),
            feed_title="",
            feed_url="",
        )
        prompt = template.render(article=art, body=body[:8000])
        try:
            out = await llm.generate_structured(prompt, schema=SummaryOutput, think=False)
        except Exception as e:  # noqa: BLE001
            samples.append(f"  [err] {aid}: {e}")
            continue
        pid = str((out.routing_flags or {}).get("primary_actor_id") or "").strip()
        conf = str((out.routing_flags or {}).get("confidence") or "")
        if pid:
            produced += 1
            hit = resolve_actor_by_name(pid.replace("-", " ").replace("_", " "), registry)
            if hit:
                resolved += 1
                # 本文にその actor の言及があるか (二重ゲートの言及所属)
                if hit.id in {a.id for a in registry.find_all(body)}:
                    in_mention += 1
            if len(samples) < 15:
                samples.append(
                    f"  pid={pid!r} conf={conf} resolved={hit.id if hit else None} "
                    f"| {str(r['title'])[:55]}"
                )

    total = len(targets)
    print(f"=== summarizer primary_actor_id 診断 (n={total}) ===")
    print("対象 = 本文にアクター言及・title に名前なし・主題未確定の cyber 記事")
    if total:
        print(f"primary_actor_id 産出: {produced}/{total} ({produced / total:.0%})")
    if produced:
        print(f"  うち辞書解決: {resolved}/{produced}")
        print(f"  うち本文言及所属 (主題化されたはず): {in_mention}/{produced}")
    print("samples:")
    print("\n".join(samples))
    print("\n判定指針:")
    print("  産出率が高い (>50%) → 層は機能・保存ギャップだった → 再解析 backfill 有効")
    print("  産出率が低い (<20%) → 末尾フィールド枯死 → focused 分類器へ移設が本丸")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(diagnose(args.n))


if __name__ == "__main__":
    main()
