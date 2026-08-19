"""legacy .j2 から block 方式の skeleton + seed yaml を機械抽出する (層分けの移行道具)。

手で転記しない (転記ミス防止 — summarizer 1 本目で確立した作法)。cut-plan yaml に
「どの行範囲がどの block か」だけを宣言し、skeleton (マーカー行入り) と seed yaml を
生成する。**再組立 = 元ファイルと byte 一致**を書き出し前に検証する。

cut-plan の形式:
    prompt_id: status_synthesis
    legacy: prompts/synthesis/status_synthesis.j2
    skeleton_out: prompts/synthesis/status_synthesis_skeleton.j2
    seed_out: config/prompts/status_synthesis_rubric.yaml
    blocks:
      - {id: mission, title: ミッション, start: 5, end: 6}   # 1-based 両端含む

Usage:
    uv run python scripts/extract_prompt_blocks.py <cut_plan.yaml> [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO if (_REPO / "src").is_dir() else Path("/app")))

from src.prompts.block_composer import compose_blocks  # noqa: E402
from src.prompts.rubric_model import SummarizerRubric  # noqa: E402


def _build(plan: dict[str, Any], legacy_text: str) -> tuple[str, dict[str, Any]]:
    """(skeleton_text, seed_dict) を作る。block は非重複・昇順が前提 (検証あり)。"""
    lines = legacy_text.splitlines(keepends=True)
    blocks = plan["blocks"]
    prev_end = 0
    for b in blocks:
        if not (0 < int(b["start"]) <= int(b["end"]) <= len(lines)):
            raise SystemExit(f"block {b['id']}: 行範囲が不正 ({b['start']}-{b['end']})")
        if int(b["start"]) <= prev_end:
            raise SystemExit(f"block {b['id']}: 前 block と重複/逆順")
        prev_end = int(b["end"])

    skeleton_parts: list[str] = []
    sections: list[dict[str, Any]] = []
    cursor = 0  # 0-based 行 index
    for b in blocks:
        start, end = int(b["start"]) - 1, int(b["end"])  # → [start, end)
        skeleton_parts.append("".join(lines[cursor:start]))
        body_raw = "".join(lines[start:end])
        # マーカー行は "{# BLOCK: id #}\n" — 置換対象はマーカー文字列 (改行は残る) なので
        # body は末尾改行を 1 つ剥がして格納する (再組立で byte 一致になる)。
        if not body_raw.endswith("\n"):
            raise SystemExit(f"block {b['id']}: 末尾が改行で終わっていません (境界を見直す)")
        skeleton_parts.append(f"{{# BLOCK: {b['id']} #}}\n")
        sections.append(
            {
                "field_id": str(b["id"]),
                "kind": "rubric",
                "title": str(b.get("title", b["id"])),
                "body": body_raw[:-1],
                "note": str(b.get("note", "")),
            }
        )
        cursor = end
    skeleton_parts.append("".join(lines[cursor:]))
    skeleton_text = "".join(skeleton_parts)
    seed = {"schema_version": 1, "intro": "", "sections": sections, "examples": []}
    return skeleton_text, seed


def main() -> None:
    ap = argparse.ArgumentParser(description="legacy .j2 → skeleton + seed yaml の機械抽出")
    ap.add_argument("cut_plan", type=Path)
    ap.add_argument("--apply", action="store_true", help="ファイルに書き出す (既定は検証のみ)")
    args = ap.parse_args()

    plan = yaml.safe_load(args.cut_plan.read_text(encoding="utf-8"))
    legacy_text = Path(plan["legacy"]).read_text(encoding="utf-8")
    skeleton_text, seed = _build(plan, legacy_text)

    # 検証 1: pydantic 境界 (seed が model を通ること)
    rubric = SummarizerRubric.model_validate(seed)
    # 検証 2: 再組立 = 元ファイルと byte 一致 (層分けの不変量)
    recomposed = compose_blocks(skeleton_text, rubric)
    if recomposed != legacy_text:
        for i, (a, b) in enumerate(zip(recomposed, legacy_text, strict=False)):
            if a != b:
                raise SystemExit(f"byte 不一致: offset {i} (再組立 {a!r} vs 元 {b!r})")
        raise SystemExit(f"長さ不一致: 再組立 {len(recomposed)} vs 元 {len(legacy_text)}")

    print(f"blocks: {len(seed['sections'])} / 再組立 byte 一致 OK ({len(legacy_text)} bytes)")
    if args.apply:
        Path(plan["skeleton_out"]).write_text(skeleton_text, encoding="utf-8")
        Path(plan["seed_out"]).write_text(
            yaml.safe_dump(seed, allow_unicode=True, sort_keys=False, width=100),
            encoding="utf-8",
        )
        print(f"書出: {plan['skeleton_out']} / {plan['seed_out']}")
    else:
        print("(検証のみ — --apply で書き出し)")


if __name__ == "__main__":
    main()
