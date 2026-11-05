"""RP-4-1 の純関数 (prereg docs/replay_prereg/grok_summary_grounding.md)。DB・LLM を使わない。"""

from __future__ import annotations

import random
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from scripts.replay._stats import Interval, wilson_interval
from src.pipeline.summary_grounding import ungrounded_reasons

JST = timedelta(hours=9)
#: X の snowflake: 上位ビットが ms 時刻 (epoch 2010-11-04)
_TWITTER_EPOCH_MS = 1288834974657
_SNOWFLAKE_SHIFT = 22
#: 窓 (prereg §2.2): 投稿の時刻 2026-10-25 00:00 JST 以上、2026-11-04 00:00 JST 未満
WINDOW_START = datetime(2026, 10, 25, tzinfo=UTC) - JST
WINDOW_END = datetime(2026, 11, 4, tzinfo=UTC) - JST
SEED = 20261105
FIRED_SAMPLE_N = 30
NOFIRE_SAMPLE_N = 6
#: 非発火側の参考は、発火側の後に使った回数がこの数以下のときだけ続ける (prereg §5)
NOFIRE_CONTINUE_MAX_USED = 33
MIN_POPULATION = 100
#: 合否の線 (prereg §6.2)
ON_MAX_FIRE_RATE = 0.15
ON_MIN_POINT = 0.70
ON_MIN_LOW = 0.50
OFF_MAX_POINT = 0.50
OFF_MAX_HIGH = 0.60
SMALL_FIRED_N = 20
MAX_FAIL_RATE = 0.20

_STATUS = re.compile(r"/status/(\d+)")
_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.IGNORECASE)

Decision = Literal["on", "shadow", "off", "hold"]


def status_id_of(url: str) -> str | None:
    m = _STATUS.search(url or "")
    return m.group(1) if m else None


def posted_at(status_id: str) -> datetime:
    ms = (int(status_id) >> _SNOWFLAKE_SHIFT) + _TWITTER_EPOCH_MS
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def cves_of(text: str) -> frozenset[str]:
    return frozenset(c.upper() for c in _CVE.findall(text or ""))


def fire_reasons(row: Mapping[str, Any]) -> list[str]:
    """briefing._grounding_reasons と同じ引数で関門を当てる。"""
    published = row.get("published_at")
    years: tuple[int, ...] = (int(str(published)[:4]),) if published else ()
    text = f"{row.get('summary') or ''}\n{row.get('remediation') or ''}"
    return ungrounded_reasons(
        text, body=row.get("body") or "", title=row.get("title") or "", exempt_years=years
    )


def reason_kinds(reasons: Sequence[str]) -> list[str]:
    return sorted({r.split(":", 1)[0] for r in reasons})


@dataclass(frozen=True)
class Funnel:
    rows_in: int
    no_status: int
    empty_body_or_summary: int
    duplicate_rows: int
    outside_window: int
    population: int


def fold_population(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], Funnel]:
    """畳み (同じ status_id は created_at・id が最小の行) と窓。除外の件数を返す。"""
    no_status = empty = dup = 0
    by_status: dict[str, dict[str, Any]] = {}
    for r in rows:
        sid = status_id_of(str(r.get("url") or ""))
        if sid is None:
            no_status += 1
            continue
        if not (r.get("body") or "").strip() or not (r.get("summary") or "").strip():
            empty += 1
            continue
        cur = by_status.get(sid)
        key = (str(r["created_at"]), int(r["id"]))
        if cur is not None:
            dup += 1
            if key >= (str(cur["created_at"]), int(cur["id"])):
                continue
        by_status[sid] = {**r, "status_id": sid}
    kept: list[dict[str, Any]] = []
    outside = 0
    for sid in sorted(by_status, key=int):
        if WINDOW_START <= posted_at(sid) < WINDOW_END:
            kept.append(by_status[sid])
        else:
            outside += 1
    return kept, Funnel(len(rows), no_status, empty, dup, outside, len(kept))


def draw_sample(fired: Sequence[str], nofire: Sequence[str]) -> tuple[list[str], list[str]]:
    """status_id の数値の昇順に並べて抜く (prereg §4)。"""
    f = sorted(fired, key=int)
    n = sorted(nofire, key=int)
    fired_s = f if len(f) <= FIRED_SAMPLE_N else random.Random(SEED).sample(f, FIRED_SAMPLE_N)
    nofire_s = random.Random(f"{SEED}:nofire").sample(n, min(NOFIRE_SAMPLE_N, len(n)))
    return sorted(fired_s, key=int), sorted(nofire_s, key=int)


def presentation_order(status_ids: Sequence[str]) -> list[str]:
    """Opus への提示順 (発火側と非発火側を混ぜる、prereg §5)。"""
    ids = sorted(status_ids, key=int)
    random.Random(f"{SEED}:order").shuffle(ids)
    return ids


def is_error(verdict: str) -> bool:
    return verdict in ("minor_only", "heavy")


def precision_interval(errors: int, judged: int) -> Interval:
    return wilson_interval(errors, judged)


def decide(
    *, population: int, fired: int, judged: int, errors: int, failed: int, sample_n: int
) -> tuple[Decision, dict[str, float]]:
    """prereg §6.2・§6.3 の線。hold = 無効 (判定を保留)。"""
    fire_rate = fired / population if population else 0.0
    iv = wilson_interval(errors, judged)
    detail = {
        "fire_rate": fire_rate,
        "precision": iv.point,
        "precision_low": iv.low,
        "precision_high": iv.high,
    }
    if population < MIN_POPULATION or (sample_n and failed / sample_n > MAX_FAIL_RATE):
        return "hold", detail
    if iv.point < OFF_MAX_POINT or iv.high < OFF_MAX_HIGH:
        return "off", detail
    on_ok = fire_rate <= ON_MAX_FIRE_RATE and iv.point >= ON_MIN_POINT and iv.low >= ON_MIN_LOW
    if on_ok and fired >= SMALL_FIRED_N:
        return "on", detail
    return "shadow", detail
