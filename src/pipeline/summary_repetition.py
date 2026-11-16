"""要約の自由文欄の「同じ文の繰り返し」の検出・切り詰め・作り直しの関門 (SUMMARY_REPETITION_GUARD)。

長い記事で構造化出力が 2 文目以降を同じ 1 文の繰り返し (数十〜250 回) で埋める故障の対策。
既存の ``summary_runaway`` (常時 on) と違い、英語の句点 (.) も文区切りにし、旗で shadow / 本適用を
切り替える。検出・切り詰めは標準ライブラリだけの純粋関数。**文面はログにも戻り値にも出さない**。

CLI (オフライン検証用、件数だけ出す)::

    python -m src.pipeline.summary_repetition < file.jsonl   # 各行の "output" (dict) を検査
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel

from src.logging_config import get_logger

_log = get_logger(__name__)

GuardMode = Literal["0", "shadow", "1"]
GUARD_ENV = "SUMMARY_REPETITION_GUARD"

#: 同じ文がこの回数以上出たら繰り返し
SENTENCE_REPEAT_MIN = 3
#: この長さ (空白除く前後 strip 後) 未満の文は数えない
SENTENCE_MIN_CHARS = 12
#: 作り直しのときに現在の temperature へ足す量
RETRY_TEMPERATURE_DELTA = 0.2
#: 検査する自由文の欄 (SummaryOutput の文字列欄のうち読者に届くもの)
FREE_TEXT_FIELDS = ("summary", "bluf", "analyst_note", "remediation", "title_ja")

#: 文 = 区切り記号 (。！？.!? ・改行) までの塊。終端の記号は文に含める (改行は含めない)
_SENTENCE = re.compile(r"[^。！？.!?\n]+(?:[。！？.!?]+|(?=\n)|$)")
#: 他の生成物 (Spotlight 等) の関門が同じ文区切りを使うための公開名
SENTENCE_PATTERN = _SENTENCE


@dataclasses.dataclass(frozen=True)
class RepetitionMatch:
    """繰り返しの検出結果。文面は持たない。"""

    field: str
    count: int
    first_index: int  # 文の通し番号 (0 始まり、短い文も数えた上での番号)
    sentence_chars: int


def _sentences(text: str) -> list[tuple[int, int, str]]:
    """(開始, 終了, 比較キー) の列。比較キーは前後の空白を除いた文 (短い文は空文字)。"""
    out: list[tuple[int, int, str]] = []
    for m in _SENTENCE.finditer(text):
        key = m.group(0).strip()
        out.append((m.start(), m.end(), key if len(key) >= SENTENCE_MIN_CHARS else ""))
    return out


def _repeated(text: str) -> tuple[str, int, int] | None:
    """最も多く繰り返された文 (同数なら先に出た方) の (キー, 回数, 初出の文番号)。"""
    sents = _sentences(text)
    counts = Counter(k for _, _, k in sents if k)
    if not counts:
        return None
    best_key, best = "", 0
    for key in counts:  # 挿入順 = 初出順
        if counts[key] > best:
            best_key, best = key, counts[key]
    if best < SENTENCE_REPEAT_MIN:
        return None
    first = next(i for i, (_, _, k) in enumerate(sents) if k == best_key)
    return best_key, best, first


def find_repetition(text: str) -> RepetitionMatch | None:
    """同じ文が ``SENTENCE_REPEAT_MIN`` 回以上あれば検出結果 (欄名は呼び出し側が埋める)。"""
    found = _repeated(text)
    if found is None:
        return None
    key, count, first = found
    return RepetitionMatch(field="", count=count, first_index=first, sentence_chars=len(key))


def truncate_repetition(text: str) -> str:
    """繰り返された文の初出の直後で切る (後ろの文は捨てる)。繰り返しが無くなるまで繰り返す。"""
    current = text
    while _repeated(current) is not None:
        key, _, _ = _repeated(current) or ("", 0, 0)
        for _, end, k in _sentences(current):
            if k == key:
                cut = current[:end].rstrip()
                break
        else:  # pragma: no cover - _repeated が見つけた文は必ずある
            return current
        if cut == current:
            break
        current = cut
    return current


def _field_values(output: BaseModel | Mapping[str, Any]) -> dict[str, str]:
    data = output.model_dump() if isinstance(output, BaseModel) else dict(output)
    return {f: v for f in FREE_TEXT_FIELDS if isinstance(v := data.get(f), str) and v}


def check_summary_output(output: BaseModel | Mapping[str, Any]) -> list[RepetitionMatch]:
    """自由文の欄ごとに検査し、繰り返しのあった欄の結果を返す (純粋関数)。"""
    matches: list[RepetitionMatch] = []
    for name, value in _field_values(output).items():
        found = find_repetition(value)
        if found is not None:
            matches.append(dataclasses.replace(found, field=name))
    return matches


def guard_mode() -> GuardMode:
    """``SUMMARY_REPETITION_GUARD`` (既定 shadow)。未知の値は shadow。"""
    raw = os.environ.get(GUARD_ENV, "shadow").strip().lower()
    if raw in ("0", "off"):
        return "0"
    if raw in ("1", "on"):
        return "1"
    return "shadow"


def _truncated[S: BaseModel](output: S, matches: list[RepetitionMatch]) -> S:
    update = {m.field: truncate_repetition(str(getattr(output, m.field))) for m in matches}
    return output.model_copy(update=update)


async def guard_summary_repetition[S: BaseModel](
    summary: S,
    regenerate: Callable[[float], Awaitable[S]],
    *,
    base_temperature: float,
    article_id: str | None = None,
    model: str = "",
) -> S:
    """旗に従い繰り返しを記録 (shadow) / 1 回作り直し→なお残れば切り詰め (1)。

    何も検出されなければ入力をそのまま返す (挙動不変)。``regenerate(temperature)`` は同じ
    プロンプトで引き直す関数。
    """
    mode = guard_mode()
    if mode == "0":
        return summary
    matches = check_summary_output(summary)
    if not matches:
        return summary
    for m in matches:
        _log.warning(
            "summary_repetition_detected",
            mode=mode,
            article_id=article_id,
            field=m.field,
            repeat_count=m.count,
            sentence_chars=m.sentence_chars,
            model=model,
        )
    if mode == "shadow":
        return summary
    retried = await regenerate(base_temperature + RETRY_TEMPERATURE_DELTA)
    if not check_summary_output(retried):
        _log.info("summary_repetition_retry_clean", article_id=article_id, retries=1)
        return retried
    fixed = _truncated(summary, matches)
    _log.warning(
        "summary_repetition_truncated",
        article_id=article_id,
        retries=1,
        fields=[m.field for m in matches],
        model=model,
    )
    return fixed


def main(argv: list[str] | None = None) -> int:
    """標準入力の JSONL (各行 ``output`` dict) を検査し、件数だけ出す。"""
    del argv
    rows = hit = 0
    by_field: Counter[str] = Counter()
    for line in sys.stdin:
        if not line.strip():
            continue
        rows += 1
        obj = json.loads(line)
        out = obj.get("output") if isinstance(obj, dict) else None
        if not isinstance(out, dict):
            continue
        matches = check_summary_output(out)
        hit += bool(matches)
        by_field.update(m.field for m in matches)
    print(json.dumps({"rows": rows, "repetitive_rows": hit, "by_field": dict(by_field)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
