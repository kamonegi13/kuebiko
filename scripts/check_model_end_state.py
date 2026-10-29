"""本番の割当が 2 枠の終着の状態に入っているか確かめる (ops 用、読み取りのみ)。

使い方 (ローカルの full instance を相手にする):
    uv run python scripts/check_model_end_state.py [--base-url http://127.0.0.1:8001]

違反があれば 1 を返す。MODEL_RESIDENT_ENFORCE=1 にする前に 0 件であること
(順番は src/tools/model_end_state.py の docstring)。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import date

from src.tools.model_end_state import find_violations

_TIMEOUT_SECONDS = 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    url = f"{args.base_url.rstrip('/')}/api/v1/model-tiers"
    with urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS) as resp:  # noqa: S310
        data = json.load(resp)
    violations = find_violations(
        data["tiers"], data["step_overrides"], data["local_fallbacks"], today=date.today()
    )
    for v in violations:
        sys.stdout.write(f"{v}\n")
    sys.stdout.write(f"violations: {len(violations)} (config version {data['config_version']})\n")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
