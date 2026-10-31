"""被害組織名の別名辞書 (org_alias_dict) を shadow の「同一」判定から作る / 更新する (ORG-D-IMPL)。

材料 = ``org_identity_pairs`` の同一の組 (``list_org_identity_same_pairs``)。連結して
照合キー → 代表キーの写像にし、伏字 (``*``) を含む名前のキーを除いた上で ``org_alias_dict`` の
``source='pairs'`` の行を置き換える。他の source の行は触らない。

既定は **dry-run** (差分を出すだけ。DB へ書かない)。``--apply`` で書く。
**使い捨てコンテナで流す前提**。本番 DB に対して ``--apply`` しない (辞書を読むのは
``EVENTNEWS_ORG_ALIAS_SOURCE=dict`` のときだけで、本番の既定 ``pairs`` では未使用)。

使い方 (使い捨てコンテナ / dev):
  uv run python scripts/build_org_alias_dict.py            # dry-run
  uv run python scripts/build_org_alias_dict.py --apply
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.eventnews.org_alias_dict import (  # noqa: E402
    DICT_SOURCE_PAIRS,
    build_dict_mapping,
    diff_mapping,
    masked_keys,
)
from src.storage.run_history import RunHistoryRepository  # noqa: E402

_SQL_MASKED_NAMES = (
    "SELECT DISTINCT TRIM(value) FROM article_entities"
    " WHERE entity_type = 'victim_org' AND (value LIKE '%*%' OR value LIKE '%＊%')"
)


def _masked_names(repo: RunHistoryRepository) -> list[str]:
    with repo._connect() as conn:  # noqa: SLF001 — 他の scripts と同型
        return [str(r[0]) for r in conn.execute(_SQL_MASKED_NAMES).fetchall()]


def refresh(repo: RunHistoryRepository, *, apply: bool) -> dict[str, int]:
    """辞書を作り直す。戻り値は件数の要約 (pairs / keys / masked_dropped / 差分 3 種)。"""
    pairs = repo.list_org_identity_same_pairs()
    masked = masked_keys(_masked_names(repo))
    new = build_dict_mapping(pairs, masked)
    dropped = len(build_dict_mapping(pairs)) - len(new)
    try:
        current = repo.load_org_alias_dict()
    except Exception:  # noqa: BLE001 — 表がまだ無い dry-run は空として差分を出す
        current = {}
    added, changed, removed = diff_mapping(current, new)
    if apply:
        repo.replace_org_alias_dict(new, source=DICT_SOURCE_PAIRS)
    return {
        "pairs": len(pairs),
        "keys": len(new),
        "masked_dropped": dropped,
        "added": len(added),
        "changed": len(changed),
        "removed": len(removed),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="書く (既定は dry-run)")
    args = parser.parse_args(argv)
    summary = refresh(RunHistoryRepository(), apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN (書いていない)"
    print(f"[{mode}] " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
