"""victim_org の AI ベンダ汚染掃除 (監査 2026-08-01 ⑦)。

AI 記事 (prompt injection / jailbreak / モデル汚染の研究・解説) で OpenAI/Anthropic/
Hugging Face 等が「被害組織」として抽出され、victim 台帳と地図 (組織本社 tier の偽点)
を汚していた。プロンプト側の除外指示 (summarizer.j2 ⑤) と対で過去分を掃除する。

保守則: denylist 該当でも **category が breach/incident の行は残す** — そのベンダ自身が
実際に侵害された記事 (OpenAI 社内 Slack 侵害等) を巻き込まないため。dry-run で対象記事の
タイトルを必ず目視すること。

既定 dry-run。--apply で実行:
  docker exec kuebiko python -m scripts.purge_vendor_victim_org [--apply]
"""

from __future__ import annotations

import argparse

from src.logging_config import get_logger
from src.storage.run_history import RunHistoryRepository

_log = get_logger(__name__)

_BACKUP = "_backup_victim_org_purge_20260801"

# AI プラットフォーム/ベンダ (小文字比較)。「製品への攻撃手法の記事」で victim 扱い
# される常連。実侵害の可能性がある行は category 条件で保護するため、列挙は攻めてよい。
_AI_VENDOR_DENYLIST: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "hugging face",
        "huggingface",
        "google deepmind",
        "deepmind",
        "meta ai",
        "mistral",
        "mistral ai",
        "cohere",
        "stability ai",
        "xai",
        "perplexity",
        "perplexity ai",
    }
)
# 実被害の可能性があるため掃除対象から除外する category
_PROTECTED_CATEGORIES = ("breach", "incident")


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        prot_ph = ",".join("?" for _ in _PROTECTED_CATEGORIES)
        rows = con.execute(
            "SELECT ae.id AS eid, ae.value, a.category, a.title, a.article_id"
            " FROM article_entities ae JOIN articles a ON a.article_id = ae.article_id"
            " WHERE ae.entity_type='victim_org'"
            f" AND (a.category IS NULL OR a.category NOT IN ({prot_ph}))",
            list(_PROTECTED_CATEGORIES),
        ).fetchall()
        targets = [r for r in rows if str(r["value"]).strip().lower() in _AI_VENDOR_DENYLIST]
        print(f"\n=== victim_org AI ベンダ掃除 ({mode}) — breach/incident は保護 ===")
        print(f"対象 {len(targets)} 行:")
        for r in targets:
            print(f"  [{r['category']}] {r['value']:20} | {str(r['title'])[:60]}")
        if not targets:
            print("(対象なし)")
            return
        if apply:
            ids = [int(r["eid"]) for r in targets]
            ph = ",".join("?" for _ in ids)
            con.execute(f"DROP TABLE IF EXISTS {_BACKUP}")  # noqa: S608 — 固定名
            con.execute(
                f"CREATE TABLE {_BACKUP} AS SELECT * FROM article_entities"  # noqa: S608
                f" WHERE id IN ({ph})",
                ids,
            )
            deleted = con.execute(
                f"DELETE FROM article_entities WHERE id IN ({ph})",  # noqa: S608 — ph は ? 固定
                ids,
            ).rowcount
            print(f"\nDELETE {deleted} 行 (退避 = {_BACKUP})")
        else:
            print("\n(dry-run — --apply で実行。タイトルを目視し実被害の巻き込みが無いか確認)")
    _log.info("purge_vendor_victim_org_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="victim_org の AI ベンダ汚染掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
