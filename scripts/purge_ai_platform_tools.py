"""tools に混入した正規 AI 製品名の掃除 (2026-08-19)。

取込関門 (``malware_aliases.yaml`` の ai_platform_drop → ``normalize_tool``) と
**同じ SSoT** で過去分を掃除する。判定基準 tools は「絶対に含めない② AI モデル /
プラットフォーム名」と明記していたが、実測で 62 件が混入していた
(Claude Code 15 / ChatGPT 9 / DeepSeek 7 / Ollama 5 等)。

削除基準は「**今の関門なら drop になる値**」(normalize_tool が drop を返す) — AI 名の
ほか、generic drop 語が tool に紛れていた行も同時に拾う。加えて、観測用の広い regex
(audit_prompt_prohibitions._AI_PLATFORM_REGEX) には当たるが関門は通す値を「関門の
取りこぼし候補」として別枠で表示する (削除はしない — 人が見て yaml に足すか決める)。

⚠ dry-run で値を必ず目視すること。既定 dry-run。--apply で実行:
  docker exec -w /app kuebiko python -m scripts.purge_ai_platform_tools [--apply]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))
sys.path.insert(0, str(_REPO / "scripts"))

from audit_prompt_prohibitions import _AI_PLATFORM_REGEX  # noqa: E402

from src.cti.malware_normalizer import load_malware_normalizer  # noqa: E402
from src.logging_config import get_logger  # noqa: E402
from src.storage.run_history import RunHistoryRepository  # noqa: E402

_log = get_logger(__name__)

_BACKUP = "_backup_tool_ai_platform_20260819"


def _run(apply: bool, repo: RunHistoryRepository | None = None) -> None:
    repo = repo if repo is not None else RunHistoryRepository()
    mode = "APPLY" if apply else "DRY-RUN"
    normalizer = load_malware_normalizer()
    broad = re.compile(_AI_PLATFORM_REGEX)
    with repo._connect() as con:  # noqa: SLF001 — 修復スクリプト
        rows = con.execute(
            "SELECT id AS eid, value FROM article_entities WHERE entity_type='tool'"
        ).fetchall()
        targets = [r for r in rows if normalizer.normalize_tool(str(r["value"]))[1] == "drop"]
        # 関門は通すが観測 regex に当たる値 = 関門の取りこぼし候補 (削除しない)
        target_ids = {int(r["eid"]) for r in targets}
        suspects = sorted(
            {
                str(r["value"])
                for r in rows
                if int(r["eid"]) not in target_ids and broad.match(str(r["value"]).strip().lower())
            }
        )
        print(f"\n=== tools の AI 製品名掃除 ({mode}) — 基準 = 取込関門と同一 ===")
        by_value: dict[str, int] = {}
        for r in targets:
            by_value[str(r["value"])] = by_value.get(str(r["value"]), 0) + 1
        print(f"削除対象 {len(targets)} 行 / 走査 {len(rows)} 行:")
        for value, count in sorted(by_value.items(), key=lambda kv: -kv[1]):
            print(f"  {value:40} {count}")
        if suspects:
            print(f"\n関門の取りこぼし候補 (観測 regex のみ該当、削除しない) {len(suspects)} 値:")
            for value in suspects:
                print(f"  {value}")
        if not targets:
            print("(削除対象なし)")
            return
        if apply:
            ids = sorted(target_ids)
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
            print("\n(dry-run — --apply で実行。値を目視し実在の攻撃ツールが無いか確認)")
    _log.info("purge_ai_platform_tools_done", apply=apply, targets=len(targets))


def main() -> None:
    ap = argparse.ArgumentParser(description="tools の AI 製品名掃除")
    ap.add_argument("--apply", action="store_true", help="実際に削除する (既定は dry-run)")
    _run(ap.parse_args().apply)


if __name__ == "__main__":
    main()
