"""triage の PIR-driven プロンプトに「境界の型の定め (A〜I)」block (slot=boundary_rules) を
投入する dry-run-first スクリプト (2026-10-10)。

2026-10-03 利用者決定の境界の型の定め (``data/mlx/adjudicate_gold.py:BOUNDARY_RULES``、
triage gold v4 の境界再判定にそのまま使った文面) を、本番の PIR-driven triage prompt の
末尾 slot として追加する。A/B (``scripts/triage_boundary_ab.py`` →
``data/eval/triage_boundary_ab_v1.jsonl``) で v4 の非境界一致率が 89.0%→94.5% (i58) に
上がったことを根拠とする。

既定は **dry-run** (DB write 一切なし)。``--apply`` を付けたときだけ、UI の保存 API
(``PUT /api/v1/prompts/article_triage/blocks`` = ``src/ui/api/prompt_blocks.py:put_blocks``)
と同じ経路で DB (config_store, key=``triage_rubric``) に新 version として保存する
(検証・キャッシュ破棄・設定履歴への記録がアプリ側で一度に行われる)。旧 version は履歴に残り、
UI「設定 → 記録 → 履歴・監査」または
``POST /api/v1/config-history/triage_rubric/revert`` から巻き戻せる。

boundary_rules は **optional slot** (``src.tools.article_triage.OPTIONAL_SLOT_IDS``):
本スクリプトを ``--apply`` するまで DB row にこの block が無く、本番出力は従来と byte 一致の
ままである。追加する block の本文は ``config/prompts/triage_rubric.yaml`` (seed、git 管理の
正本) からそのまま読む — 既存の ``intro`` / ``low_criteria`` / ``output_rules`` は現行 DB の
値をそのまま保持し、触らない (利用者が UI で編集済みの可能性があるため上書きしない)。

Usage::

    uv run python scripts/apply_triage_boundary_rules.py          # dry-run (既定)
    uv run python scripts/apply_triage_boundary_rules.py --apply  # DB へ保存 (このタスクでは未実行)

または (read-only dry-run をコンテナ内で、本番 kuebiko コンテナは止めずに)::

    docker compose run --rm --no-deps -T -e PYTHONPATH=/app \\
        -v $PWD/src:/app/src:ro -v $PWD/scripts:/app/scripts:ro -v $PWD/config:/app/config:ro \\
        kuebiko python scripts/apply_triage_boundary_rules.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_BOUNDARY_SLOT = "boundary_rules"
_APPLY_NOTE = (
    "境界の型の定め A〜I 投入 (scripts/apply_triage_boundary_rules.py, 利用者判断 2026-10-10)"
)


def _current_rubric_and_version() -> tuple[Any, int | None]:
    """現行 (DB があれば DB、無ければ seed) rubric と config_store の現 version を返す。

    ``load_prompt_rubric`` は ``GET /api/v1/prompts/article_triage/blocks``
    (``src/ui/api/prompt_blocks.py:get_blocks``) が使う関数そのもの。
    """
    from src.prompts.prompt_store import invalidate_prompt_cache, load_prompt_rubric
    from src.prompts.registry import get_spec
    from src.storage.config_store import list_history

    spec = get_spec("article_triage")
    assert spec is not None
    invalidate_prompt_cache(spec.prompt_id)  # 同一プロセス内の古いキャッシュを使わない
    rubric = load_prompt_rubric(spec)
    if rubric is None:
        raise RuntimeError("triage_rubric の編集層が見つかりません (DB にも seed にも無い)")
    history = list_history(spec.config_key, limit=1)
    version = history[0].version if history else None
    return rubric, version


def _seed_boundary_section() -> Any:
    """seed yaml (git 管理の正本) から boundary_rules block を読む (canonical source)。"""
    from src.prompts.prompt_store import load_seed_rubric
    from src.prompts.registry import get_spec

    spec = get_spec("article_triage")
    assert spec is not None
    seed = load_seed_rubric(spec)
    if seed is None:
        raise RuntimeError(f"seed yaml が読めません: {spec.seed_path}")
    section = next((s for s in seed.sections if s.field_id == _BOUNDARY_SLOT), None)
    if section is None:
        raise RuntimeError(f"seed yaml に {_BOUNDARY_SLOT} block がありません (code 側の欠陥)")
    return section


def _print_block_preview(label: str, body: str) -> None:
    lines = body.splitlines()
    print(f"  {label}: {len(body)} 字, {len(lines)} 行")
    for line in lines[:4]:
        print(f"    | {line}")
    if len(lines) > 4:
        print(f"    | … (以下 {len(lines) - 4} 行省略)")


def _print_revert_instructions(version: int | None) -> None:
    if version is None:
        print("\n巻き戻し手順: 現行 version が取得できなかったため UI の履歴から確認してください")
        return
    print(
        "\n巻き戻し手順 (この投入前の version に戻す場合):\n"
        "  UI: 「設定 → 記録 → 履歴・監査」→ triage_rubric → "
        f"version {version} を revert\n"
        "  API: POST /api/v1/config-history/triage_rubric/revert\n"
        f'    body: {{"version": {version}}}\n'
        "  または: boundary_rules block の本文を空文字にして保存 (= 空文字合成で従来挙動に戻る)"
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

    from src.prompts.prompt_store import validate_block_rubric
    from src.prompts.registry import get_spec

    spec = get_spec("article_triage")
    assert spec is not None

    current_rubric, current_version = _current_rubric_and_version()
    seed_section = _seed_boundary_section()

    existing = next((s for s in current_rubric.sections if s.field_id == _BOUNDARY_SLOT), None)
    print(f"現行 (config_store key='{spec.config_key}', version={current_version}):")
    print(f"  block 数: {len(current_rubric.sections)}")
    print(f"  field_id 一覧: {[s.field_id for s in current_rubric.sections]}")
    if existing is None:
        print(f"  {_BOUNDARY_SLOT}: (未保存 — optional slot のため空文字合成、本番出力は不変)")
    else:
        _print_block_preview(f"{_BOUNDARY_SLOT} (現行 DB)", existing.body)

    print(f"\n投入予定の {_BOUNDARY_SLOT} block (seed={spec.seed_path} から):")
    print(f"  title: {seed_section.title}")
    _print_block_preview(f"{_BOUNDARY_SLOT} (投入予定)", seed_section.body)

    if existing is not None and existing.body == seed_section.body:
        print("\n差分なし — 現行 DB の block は seed と byte 一致です (再投入の必要なし)。")
    elif existing is not None:
        print("\n⚠ 現行 DB にはこの slot の既存 block があり、内容が seed と異なります。")
        print("  --apply は seed の内容で上書きします (他の block は現行 DB の値を保持)。")
    else:
        print(
            f"\n差分: {_BOUNDARY_SLOT} を新規追加 (他の block — intro/low_criteria/output_rules "
            "— は現行 DB の値をそのまま保持)。"
        )

    new_sections = [s for s in current_rubric.sections if s.field_id != _BOUNDARY_SLOT]
    new_sections.append(seed_section)
    new_rubric = current_rubric.model_copy(update={"sections": new_sections})

    errors = validate_block_rubric(spec, new_rubric)
    if errors:
        print("\n検証エラー (validate_block_rubric):")
        for e in errors:
            print(f"  - {e}")
    else:
        print("\n検証: OK (validate_block_rubric エラーなし)")

    if not args.apply:
        print("\ndry-run のため DB への保存は行っていません (--apply で実行)。")
        _print_revert_instructions(current_version)
        return

    if errors:
        print("\n検証エラーがあるため --apply を中止します。")
        raise SystemExit(1)

    # ⚠ DB へ直接書かない — 動いているアプリのプロンプトキャッシュ (prompt_store._STATES) には
    #    期限が無く、このプロセスで invalidate しても届かない。アプリの保存 API
    #    (PUT /api/v1/prompts/article_triage/blocks) を通せば、検証・キャッシュ破棄・設定履歴への
    #    記録がアプリの中で一度に行われる (compose ネットワーク上の kuebiko サービスへ送る)。
    import json
    import urllib.request

    payload = {"rubric": new_rubric.model_dump(), "note": _APPLY_NOTE}
    req = urllib.request.Request(
        f"{args.api_base}/api/v1/prompts/article_triage/blocks",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — 固定の内部 URL
        result = json.loads(resp.read().decode("utf-8"))
    new_version = result.get("version")
    print(f"\n保存しました (新 version={new_version})。")
    _print_revert_instructions(current_version)


if __name__ == "__main__":
    main()
