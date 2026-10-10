"""M3b: 本番 Discord routing を DB ruleset v12 から ``M3B_RULES`` (v13) へ切替える投入スクリプト。

既定は **dry-run** (DB write 一切なし)。``--apply`` を付けたときだけ、UI の保存 API
(``src/ui/api/routing_rules.py:save_routing_rules``) と同じ経路 (``validate_routing_rules`` →
``src.storage.config_store.save_config`` → ``invalidate_rules_cache``) で DB (config_store,
key=``routing_rules``) に新 version として保存する (旧 version は履歴に残り、UI /
``POST /api/v1/config-history/routing_rules/revert`` から巻き戻せる)。

読み込み (``load_routing_rules`` → ``src.storage.config_store.get_config``) は
``GET /api/v1/routing-rules`` (アプリの読み込み) と全く同じ経路。validate
(``validate_routing_rules``)・channel 語彙 (``valid_channels`` → channel registry) も
アプリの保存 API と同じ関数を使う。

Usage::

    uv run python scripts/apply_m3b_routing_rules.py          # dry-run (既定)
    uv run python scripts/apply_m3b_routing_rules.py --apply  # DB へ保存 (このタスクでは実行しない)

または (本番 DB に read-only で当てる場合、コンテナ内)::

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro \\
        kuebiko python scripts/apply_m3b_routing_rules.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_APPLY_NOTE = "M3b v13 投入 (scripts/apply_m3b_routing_rules.py, 利用者判断 2026-10-10)"


def _current_rules_and_version() -> tuple[list[dict[str, Any]], int | None]:
    """現行 (v12) ルールセットと config_store の現 version を返す (read-only)。

    ``load_routing_rules`` / ``list_history`` はいずれも
    ``src/ui/api/routing_rules.py`` / ``src/ui/api/config_history.py`` が使う関数そのもの。
    """
    from src.cti.routing_rules import CONFIG_KEY, load_routing_rules
    from src.storage.config_store import list_history

    rules = load_routing_rules(force_reload=True)
    history = list_history(CONFIG_KEY, limit=1)
    version = history[0].version if history else None
    return rules, version


def _rule_ids(rules: list[dict[str, Any]]) -> list[str]:
    return [str(r.get("id") or "(id なし)") for r in rules]


def _print_rule_list(title: str, rules: list[dict[str, Any]]) -> None:
    print(title)
    for r in rules:
        rid = r.get("id") or "(id なし)"
        label = r.get("label") or ""
        ch = r.get("channel") or ""
        print(f"  {rid:<28} -> {ch:<14} {label}")


def _print_revert_instructions(version: int | None) -> None:
    if version is None:
        print("\n巻き戻し手順: v12 の version が取得できなかったため UI の履歴から確認してください")
        return
    print(
        "\n巻き戻し手順 (v12 に戻す場合):\n"
        f"  UI: 「設定 → 記録 → 履歴・監査」→ routing_rules → version {version} を revert\n"
        "  API: POST /api/v1/config-history/routing_rules/revert\n"
        f'    body: {{"version": {version}}}'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="アプリの保存 API へ送り新 version として保存する (既定は dry-run)",
    )
    parser.add_argument(
        "--api-base",
        default="http://kuebiko:8000",
        help="保存 API の base URL (compose ネットワーク内の full instance)",
    )
    args = parser.parse_args()

    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.routing_rules import CONFIG_KEY, valid_channels, validate_routing_rules
    from src.cti.routing_v2 import M3B_RULES

    current_rules, current_version = _current_rules_and_version()
    _print_rule_list(
        f"現行ルールセット (config_store key='{CONFIG_KEY}', version={current_version}):",
        current_rules,
    )
    _print_rule_list("\n投入予定ルールセット (v13 = src.cti.routing_v2.M3B_RULES):", M3B_RULES)

    print("\n登録済み channel (routable):", sorted(valid_channels()))
    errors = validate_routing_rules(M3B_RULES, set(KNOWN_ARTICLE_CATEGORIES))
    if errors:
        print("\n検証エラー (validate_routing_rules):")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\n検証: OK (validate_routing_rules エラーなし)")

    current_ids = _rule_ids(current_rules)
    new_ids = _rule_ids(M3B_RULES)
    removed = [rid for rid in current_ids if rid not in new_ids]
    added = [rid for rid in new_ids if rid not in current_ids]
    print("\nルール id 差分:")
    print(f"  削除: {removed or '(なし)'}")
    print(f"  追加: {added or '(なし)'}")

    if not args.apply:
        print("\ndry-run のため DB への保存は行っていません (--apply で実行)。")
        print("(投入後に v12 へ戻す場合の手順)")
        _print_revert_instructions(current_version)
        return

    if errors:
        print("\n検証エラーがあるため --apply を中止します。")
        raise SystemExit(1)

    # ⚠ DB へ直接書かない — 動いているアプリのルールキャッシュには期限が無く、このプロセスで
    #    invalidate しても届かない。アプリの保存 API を通せば、検証・キャッシュ破棄・設定履歴への
    #    記録がアプリの中で一度に行われる (compose ネットワーク上の kuebiko サービスへ送る)。
    import json
    import urllib.request

    req = urllib.request.Request(
        f"{args.api_base}/api/v1/routing-rules",
        data=json.dumps({"rules": M3B_RULES}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — 固定の内部 URL
        result = json.loads(resp.read().decode("utf-8"))
    new_version = result.get("version")
    print(f"\n保存しました (新 version={new_version})。")
    _print_revert_instructions(current_version)


if __name__ == "__main__":
    main()
