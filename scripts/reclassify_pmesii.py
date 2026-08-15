"""既存 article の PMESII 8 軸を新 prompt で再分類して UPDATE。

prompt 修正 (briefing/summarizer.j2 で I-infra / T の definition broader 化) を既存 4,648
件に反映するための軽量 LLM 再走 script。

input: articles テーブル (title + summary + body 先頭 1500 char)
output: articles.pmesii_p/m/e/s/i_infra/i_cyber/p_env/t を UPDATE

LLM call: 1 article = 1 LLM call で PMESII 8 軸のみ判定 (short prompt)
所要時間: ~5 sec/article × 4648 = ~6.5 時間 (sequential)

Usage:
    docker cp scripts/reclassify_pmesii.py kuebiko:/app/scripts/
    docker exec -d -w /app -e PYTHONPATH=/app kuebiko \\
        /app/.venv/bin/python /app/scripts/reclassify_pmesii.py [--limit N] [--offset N]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from src.config_loader import load_app_config
from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository
from src.tools.llm_client import OllamaClient

_log = get_logger(__name__)

PMESII_AXES = ["P", "M", "E", "S", "I-infra", "I-cyber", "P-env", "T"]

PROMPT_TEMPLATE = """以下の CTI article について PMESII-PT 8 軸の multi-label 分類を行ってください。

## 軸 definition

- ``P`` (Political): サイバー政策・規制・attribution・国際協調 (CISA/NCSC 等の advisory)
- ``M`` (Military): 国家系 APT (PLA/GRU/IRGC/RGB)・cyber warfare doctrine・兵器開発
- ``E`` (Economic): cybercrime 経済・ransomware・サプライチェーン・制裁経済・企業 breach
- ``S`` (Social): ハクティビスト・情報操作 (InfoOps)・認知戦・ディスインフォ
- ``I-infra`` (Infrastructure): 民生重要インフラまたは重要インフラ運営組織への攻撃/言及。
  ✓ 立てる: 医療 (病院/製薬/治験)、 電力、 ガス、 水道、 通信 (キャリア/ISP/海底ケーブル)、
            交通 (鉄道/航空/港湾/物流)、 金融 backend (決済/SWIFT)、 政府 services、
            防衛 industry の operational facility、 半導体 fab、 OT/ICS/SCADA。
  ✗ 立てない: 一般 enterprise IT 環境への攻撃 (= corporate breach)、 AI 兵器そのもの、
              個別 CVE / 脆弱性解説、 SaaS / Cloud 脆弱性、 cybersecurity 企業の自社製品 release。
- ``I-cyber`` (Information): TTP / IoC / 脆弱性 / malware (本 pipeline の主産物)
- ``P-env`` (Physical Env): 地政地理・データ主権・海底ケーブル・データセンター位置
- ``T`` (Time): 時間軸 / 経時的視点が明示。
  ✓ 立てる: campaign 継続期間 (「3 ヶ月活動」)、 kill chain 段階解説 (initial access → ...)、
           attack timing 重要性 (祝日狙い/patch 直後/election 前)、 trend 解説 (前年比/スパイク)、
           historical comparison (「過去 5 年で最大」)。
  ✗ 立てない: 単発 incident の発生時刻のみ、 単一 CVE の公開日のみ、 単純 breaking news。

## 代表的な判定例 (= 厳格に従ってください)
- APT41 が日本製造業に侵入: ["M", "I-cyber"]
- Dragos OT/ICS report: ["I-infra", "I-cyber"]
- CVE advisory: ["I-cyber", "P"]
- 病院 ransomware 64 万人漏洩: ["I-infra", "E"]
- APT 6 ヶ月継続活動 report: ["M", "I-cyber", "T"]
- 2025Q1 cybercrime trend report: ["E", "I-cyber", "T"]
- Volt Typhoon 米通信侵入: ["I-infra", "M"]
- 7-Eleven Salesforce breach: ["E"] (✗ I-infra にしない)
- Notepad++ RCE 脆弱性: ["I-cyber"] (✗ I-infra ✗ T にしない)
- AI 兵器開発加速 (中国 LLM-driven autonomy): ["M", "I-cyber"] (✗ I-infra にしない)
- 米軍機がイラン船舶攻撃: ["M", "P-env"]
- 中東情勢: 米イラン緊迫: ["P", "P-env"] (✗ T にしない、 現状であって trend でない)

該当する軸を 1〜3 個 (典型: 1-2 個) 選択。 該当なしなら空配列。

## article

title: {title}

summary: {summary}

body (先頭 1500 char):
{body}

PMESII 軸 の判定結果を JSON で出力してください。
"""


class PmesiiOutput(BaseModel):
    """PMESII 8 軸 multi-label 出力。"""

    pmesii_axes: list[str] = Field(default_factory=list)


def _trim_body(body: str | None, max_chars: int = 1500) -> str:
    if not body:
        return "(empty)"
    return body[:max_chars]


async def reclassify_one(
    llm: OllamaClient,
    article_id: int,
    title: str,
    summary: str | None,
    body: str | None,
) -> set[str] | None:
    prompt = PROMPT_TEMPLATE.format(
        title=title or "(no title)",
        summary=summary or "(no summary)",
        body=_trim_body(body),
    )
    try:
        out = await llm.generate_structured(
            prompt, PmesiiOutput, temperature=0.0, think=False,
        )
        return set(out.pmesii_axes) & set(PMESII_AXES)
    except Exception as e:  # noqa: BLE001
        _log.warning("reclassify_failed", article_id=article_id, error=str(e))
        return None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="処理件数上限")
    parser.add_argument("--offset", type=int, default=0, help="処理 article id offset")
    args = parser.parse_args()

    config = load_app_config()
    llm = OllamaClient(base_url=config.ollama_base_url, model=config.ollama_main_model)
    repo = RunHistoryRepository()

    print("=== reclassify_pmesii ===", file=sys.stderr)

    with repo._connect() as conn:  # noqa: SLF001
        cur = conn.execute(
            """
            SELECT id, article_id, title, summary, body
              FROM articles
             WHERE status = 'posted'
             ORDER BY id
             OFFSET %s
            """,
            (args.offset,),
        )
        rows = cur.fetchall()
    if args.limit is not None:
        rows = rows[: args.limit]
    print(f"  target: {len(rows)} articles (offset={args.offset})", file=sys.stderr)

    processed = 0
    updated = 0
    failures = 0
    for row in rows:
        article_db_id = row[0]
        title = row[2] or ""
        summary = row[3] or ""
        body = row[4] or ""
        axes = await reclassify_one(llm, article_db_id, title, summary, body)
        if axes is None:
            failures += 1
            processed += 1
            continue
        # axes set → 8 個の SMALLINT flag に変換
        with repo._connect() as conn:  # noqa: SLF001
            conn.execute(
                """
                UPDATE articles SET
                    pmesii_p       = %s,
                    pmesii_m       = %s,
                    pmesii_e       = %s,
                    pmesii_s       = %s,
                    pmesii_i_infra = %s,
                    pmesii_i_cyber = %s,
                    pmesii_p_env   = %s,
                    pmesii_t       = %s
                WHERE id = %s
                """,
                (
                    int("P" in axes),
                    int("M" in axes),
                    int("E" in axes),
                    int("S" in axes),
                    int("I-infra" in axes),
                    int("I-cyber" in axes),
                    int("P-env" in axes),
                    int("T" in axes),
                    article_db_id,
                ),
            )
        updated += 1
        processed += 1
        if processed % 50 == 0:
            print(
                f"  progress: {processed}/{len(rows)} updated={updated} failed={failures}",
                file=sys.stderr,
            )
            Path("data/recovery_reclassify_progress.txt").write_text(
                f"processed={processed} updated={updated} failed={failures}\n",
            )

    print(
        f"=== 完了: processed={processed} updated={updated} failed={failures} ===",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
