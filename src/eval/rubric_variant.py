"""rubric の変種を **DB に保存せず** メモリ上で作る。

評価のために本番のプロンプトを書き換えてはいけない (DB の rubric を保存すると
production が即座に新版を使う)。ここは「もしこのフィールドを外したら」を
オフラインで試すための変種を組み立てる。

判定基準セクションだけでなく **few-shot 例からも同じキーを外す** のが要点。
片方だけ外すと「基準では触れないが例では示している」という中途半端な状態になり、
何を測ったのか分からなくなる。
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from src.prompts.rubric_model import SummarizerRubric


def drop_fields(rubric: SummarizerRubric, field_ids: Iterable[str]) -> SummarizerRubric:
    """``field_ids`` を判定基準セクションと few-shot 例の双方から除いた変種を返す。

    元の ``rubric`` は変更しない (評価中に本番の状態を汚さない)。
    例の JSON が壊れている場合はその例をそのまま残す (評価の前提を静かに変えない)。
    """
    drop = set(field_ids)
    if not drop:
        return rubric

    sections = [s for s in rubric.sections if s.field_id not in drop]

    examples = []
    for ex in rubric.examples:
        examples.append(ex.model_copy(update={"json_text": _strip_keys(ex.json_text, drop)}))

    return rubric.model_copy(update={"sections": sections, "examples": examples})


def _strip_keys(json_text: str, drop: set[str]) -> str:
    """出力例の JSON から指定キーを除く。解析できなければ原文のまま返す。"""
    try:
        obj = json.loads(json_text)
    except (json.JSONDecodeError, TypeError):
        return json_text
    if not isinstance(obj, dict):
        return json_text
    kept = {k: v for k, v in obj.items() if k not in drop}
    return json.dumps(kept, ensure_ascii=False, indent=2)
