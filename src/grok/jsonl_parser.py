"""Grok JSONL output parser (Phase Diamond Grok 再設計)。

新 prompt (slot 1: X Native Signal v3.1 / slot 2: JP East Asia Signal v2) は
narrative ではなく per-tweet JSONL を出力する。本 module はそれを
``TweetRecord`` list に parse する。

入力例 (1 行 1 JSON):
    {"tweet_id":"...","url":"https://x.com/...","author_handle":"@...",
     "posted_at":"2026-05-25T03:42:00Z","text":"...","matched_theme":"B",...}

防御的に:
- ``` で囲まれた code block (Grok が markdown 化することがある) を剥がす
- 前後の説明文 (違反だが Grok がたまに付ける) を skip
- 1 行ずつ parse、不正 record は skip + log
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.logging_config import get_logger

_log = get_logger(__name__)

# Code block を剥がすための regex (Grok が ```json ... ``` で wrap することがある)
_CODE_BLOCK_RE = re.compile(r"^```(?:json|jsonl)?\s*\n(.+?)\n```\s*$", re.DOTALL)

# JSONL 行の判定 (最初の `{` から最後の `}` まで)
_JSONL_LINE_RE = re.compile(r"^\s*\{.*\}\s*$")


class TweetEngagement(BaseModel):
    """tweet engagement 数値。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    like: int = 0
    retweet: int = 0
    quote: int = 0
    reply: int = 0

    @property
    def signal_score(self) -> int:
        """engagement filter で使う合計 score (like + retweet)。"""
        return self.like + self.retweet


class TweetRecord(BaseModel):
    """Grok JSONL output の 1 record。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tweet_id: str
    url: str
    author_handle: str
    author_name: str = ""
    posted_at: str  # ISO 8601 UTC
    lang: str = ""
    text: str
    is_retweet: bool = False
    retweeted_tweet_id: str | None = None
    is_quote: bool = False
    quoted_tweet_id: str | None = None
    quoted_text: str | None = None
    reply_to_tweet_id: str | None = None
    media_urls: list[str] = Field(default_factory=list)
    external_urls: list[str] = Field(default_factory=list)
    engagement: TweetEngagement = Field(default_factory=TweetEngagement)
    matched_theme: str  # A-F or J1-J6
    # hourly 運用対応 (2026-08-15): author の種別。投稿直後 (engagement 未蓄積) でも
    # 信頼種別なら filter を通すために使う。旧レポートには無いフィールド (空 = 未分類)。
    account_class: str = ""

    @property
    def posted_at_dt(self) -> datetime | None:
        """posted_at を datetime に。parse 失敗時は None。"""
        try:
            return datetime.fromisoformat(self.posted_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    @property
    def is_slot1_theme(self) -> bool:
        """slot 1 (A-F) の theme か。"""
        return self.matched_theme in {"A", "B", "C", "D", "E", "F", "G", "H", "K"}

    @property
    def is_slot2_theme(self) -> bool:
        """slot 2 (J1-J6) の theme か。"""
        return self.matched_theme in {"J1", "J2", "J3", "J4", "J5", "J6", "I2"}


@dataclass(frozen=True)
class JsonlParseResult:
    """parse 結果のサマリ。"""

    records: list[TweetRecord]
    total_lines: int  # 試行した行数
    parsed_count: int  # 成功 record 数
    skipped_lines: list[str]  # parse 失敗行 (先頭 200 chars)
    # 事象ゼロ窓のハートビート行 ({"status":"no_events",...}) の数。
    # 0 records でもこれが 1 以上なら「静穏」、0 なら「空出力 = 障害疑い」と区別できる。
    heartbeat_count: int = 0


def is_jsonl_output(body: str) -> bool:
    """body が JSONL 形式かを heuristic 判定。

    判定基準:
    - 最初の non-blank line が `{` で始まり `}` で終わる、または
    - 先頭 10 行以内に `{...}` で完結する行 + `matched_theme` フィールドが現れる
      (Grok UI が code block の前に "JSON" "コピー" 等の label を入れる対策)
    """
    stripped = body.strip()
    if not stripped:
        return False

    # code block wrap を剥がす
    m = _CODE_BLOCK_RE.match(stripped)
    if m:
        stripped = m.group(1).strip()

    # 最初の non-blank 行が `{` 始まり `}` 終わりなら確実に JSONL
    first_line = stripped.split("\n", 1)[0].strip()
    if first_line.startswith("{") and first_line.endswith("}"):
        return True

    # Grok UI prefix ("JSON" / "コピー" / "Copy" 等) を skip して
    # 先頭 10 行以内で JSONL line + matched_theme が見つかれば JSONL
    lines = stripped.split("\n")
    for line in lines[:10]:
        line_stripped = line.strip()
        if (
            line_stripped.startswith("{")
            and line_stripped.endswith("}")
            and "matched_theme" in line_stripped
        ):
            return True
    return False


def _extract_jsonl_payload(body: str) -> str:
    """body から JSONL 部分を抽出。

    - code block wrap (```json ... ```) を剥がす
    - 前後の説明文を skip (最初の `{` から最後の `}` まで)
    """
    stripped = body.strip()
    m = _CODE_BLOCK_RE.match(stripped)
    if m:
        return m.group(1).strip()
    # `{` で始まる最初の行から取る
    lines = stripped.split("\n")
    start_idx = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("{"):
            start_idx = i
            break
    # `}` で終わる最後の行まで
    end_idx = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().endswith("}"):
            end_idx = i + 1
            break
    return "\n".join(lines[start_idx:end_idx])


def parse_jsonl(body: str) -> JsonlParseResult:
    """Grok JSONL output を parse して TweetRecord list を返す。

    Args:
        body: Grok レポート本文 (markdown wrap や前後説明文を含み得る)

    Returns:
        JsonlParseResult: 成功した record list + 統計情報
    """
    payload = _extract_jsonl_payload(body)
    lines = [line for line in payload.split("\n") if line.strip()]

    records: list[TweetRecord] = []
    skipped: list[str] = []
    heartbeats = 0

    for line in lines:
        line_stripped = line.strip()
        if not _JSONL_LINE_RE.match(line_stripped):
            skipped.append(line_stripped[:200])
            continue
        try:
            data = json.loads(line_stripped)
        except json.JSONDecodeError as e:
            _log.warning(
                "jsonl_parse_decode_error",
                error=str(e),
                line_preview=line_stripped[:120],
            )
            skipped.append(line_stripped[:200])
            continue

        if not isinstance(data, dict):
            skipped.append(line_stripped[:200])
            continue

        # 事象ゼロ窓のハートビート (追加ルール①)。record でも失敗でもない正常信号。
        # Grok が key/value の underscore を落とす揺らぎ ("noevents") を実地観測 (2026-08-15)
        # したため、正規化して許容する
        status_normalized = str(data.get("status") or "").replace("_", "")
        if status_normalized == "noevents" and "tweet_id" not in data:
            heartbeats += 1
            _log.info("jsonl_no_events_heartbeat", window=data.get("window_minutes"))
            continue

        try:
            record = TweetRecord.model_validate(data)
        except ValidationError as e:
            _log.warning(
                "jsonl_parse_validation_error",
                error_count=e.error_count(),
                first_error=str(e.errors()[0]) if e.errors() else "",
                line_preview=line_stripped[:120],
            )
            skipped.append(line_stripped[:200])
            continue

        records.append(record)

    _log.info(
        "jsonl_parse_complete",
        total_lines=len(lines),
        parsed_count=len(records),
        skipped_count=len(skipped),
        heartbeat_count=heartbeats,
    )

    return JsonlParseResult(
        records=records,
        total_lines=len(lines),
        parsed_count=len(records),
        skipped_lines=skipped,
        heartbeat_count=heartbeats,
    )


# engagement floor をバイパスする account_class (追加ルール③、2026-08-15)。
# hourly 収集では投稿から 30-90 分しか経たず engagement が構造的に低いため、
# 発信者の信頼種別を主要シグナルとする (無名アカウントは従来どおり floor 適用)。
TRUSTED_ACCOUNT_CLASSES: frozenset[str] = frozenset(
    {"vendor_official", "gov_official", "analyst_known", "affected_party"}
)


def filter_records(
    records: list[TweetRecord],
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
    engagement_floor_default: int = 3,
    engagement_floor_theme_b: int = 1,
) -> list[TweetRecord]:
    """record list に filter を適用 (engagement floor + 24h 範囲)。

    Args:
        records: parse 済 record list
        now: 基準時刻 (テスト用、未指定なら現時刻 UTC)
        max_age_hours: posted_at がこれより古い record は drop
        engagement_floor_default: theme B 以外の engagement 下限 (like+retweet)
        engagement_floor_theme_b: theme B の engagement 下限 (より緩い)

    Returns:
        filter 通過した record の list
    """
    base = now or datetime.now(UTC)
    out: list[TweetRecord] = []

    for r in records:
        # 24h 窓
        dt = r.posted_at_dt
        if dt is None:
            _log.info("jsonl_filter_drop_invalid_date", tweet_id=r.tweet_id)
            continue
        age_hours = (base - dt).total_seconds() / 3600
        if age_hours > max_age_hours:
            _log.info(
                "jsonl_filter_drop_too_old",
                tweet_id=r.tweet_id,
                age_hours=round(age_hours, 1),
            )
            continue

        # engagement floor — 信頼種別は投稿直後 (拡散未蓄積) でも通す (追加ルール③)
        if r.account_class in TRUSTED_ACCOUNT_CLASSES:
            if r.engagement.signal_score < (
                engagement_floor_theme_b if r.matched_theme == "B" else engagement_floor_default
            ):
                _log.info(
                    "jsonl_filter_trusted_bypass",
                    tweet_id=r.tweet_id,
                    account_class=r.account_class,
                    theme=r.matched_theme,
                )
            out.append(r)
            continue
        floor = engagement_floor_theme_b if r.matched_theme == "B" else engagement_floor_default
        if r.engagement.signal_score < floor:
            _log.info(
                "jsonl_filter_drop_low_engagement",
                tweet_id=r.tweet_id,
                signal_score=r.engagement.signal_score,
                threshold=floor,
                theme=r.matched_theme,
            )
            continue

        out.append(r)

    _log.info(
        "jsonl_filter_complete",
        input_count=len(records),
        output_count=len(out),
    )
    return out
