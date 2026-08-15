#!/usr/bin/env python3
"""2026-06-17 countries.yaml 拡充: victim_country_iso を最新 yaml で再正規化。

config/cti/countries.yaml に約 60 ヶ国を追加した後、既存 articles で
``victim_country_iso IS NULL`` だが ``victim_country_raw`` が新 alias に match する
record を救済する (Italy / Lebanon / Singapore 等が null のまま埋もれていた)。

設計上の安全策:
  - **additive only**: 既に iso がある record は一切触らない (誤上書き防止)。
  - 解決できた record のみ UPDATE。raw が複数国/地域語 ("US, Canada" / "global") の
    ものは normalize_country が None を返すため自然に skip される。
  - raw が異常に長い (LLM 出力漏れ) record は単一国に解決しないので skip。
  - 既定は **dry-run** (集計のみ表示)、``--apply`` で初めて書き込む。

Usage (production = PG、コンテナ内で実行し DATABASE_URL を解決):
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/renormalize_victim_countries.py            # dry-run
    docker exec kuebiko /app/.venv/bin/python3 \\
        /app/scripts/renormalize_victim_countries.py --apply    # 実行
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.cti.taxonomy_normalizer import load_normalizer  # noqa: E402
from src.storage.db_backend import connect  # noqa: E402

# 単一国に解決し得ない長文 raw (LLM 出力漏れ等) を弾く保守的上限。
_MAX_RAW_LEN = 60


def main(apply: bool) -> int:
    normalizer = load_normalizer()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, victim_country_raw
               FROM articles
               WHERE victim_country_iso IS NULL
                 AND victim_country_raw IS NOT NULL""",
        )
        rows = cur.fetchall()

        print(f"=== victim_country_iso 再正規化: 候補 {len(rows)} 件 (iso=null) ===")
        resolved: Counter[str] = Counter()
        updates: list[tuple[str, object]] = []
        for r in rows:
            rid = r["id"]
            raw = r["victim_country_raw"]
            if not raw or len(raw) > _MAX_RAW_LEN:
                continue
            iso, _ = normalizer.normalize_country(raw)
            if iso is None:
                continue
            resolved[iso] += 1
            updates.append((iso, rid))

        print(f"  解決可能: {len(updates)} 件 / 候補 {len(rows)} 件")
        for iso, n in resolved.most_common():
            print(f"    {iso:<4} {n}")

        if not apply:
            print("\n[dry-run] 書き込みなし。--apply で UPDATE を実行する。")
            return 0

        for iso, rid in updates:
            cur.execute(
                "UPDATE articles SET victim_country_iso=? WHERE id=?",
                (iso, rid),
            )
        conn.commit()
        print(f"\n[applied] {len(updates)} 件の victim_country_iso を更新した。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(apply="--apply" in sys.argv[1:]))
