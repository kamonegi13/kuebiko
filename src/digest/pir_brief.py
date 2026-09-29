"""PIR ブリーフ — 常設の問いへの「いまの答え」を朝ブリーフに載せる (段D、2026-09-29)。

SIR (状況総括・事象ニュース) は「窓の中で何が届いたか」= 流れを語る。PIR ブリーフは
「決まった問いへの答えが、いまどうなっているか」= 状態を語る。読み手は毎朝同じ問いを
同じ順で読み、**動いた問いだけ理由つきで読み、動かない問いは 1 行で鮮度を確かめる**。

- 冒頭は「本日、N 問中 M 問の答えが動いた」。静穏日に 0 問と明示できることが要件
  (「静か≠安全」— 古いことでなく、古いと分からないことが危険)
- 「動いた」= 直近 ``window_hours`` の改訂のうち delta が動きの種類のもの。最新の改訂だけを
  見ると (a) 6 日前の更新がいつまでも「動いた」に数えられ、(b) 朝の拡大のあと夜に「継続」で
  上書きされた問いを取りこぼす (実データで両方あった)
- 決定論・LLM 呼出なし。データ源は ``build_standing_posture`` (問いの面 API と同じ射影)

設計: docs/pir_brief_design.md。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from src.vocab.registry import get_vocabulary

#: 「答えが動いた」と見なす delta (継続・収束の予告・未追跡は動いていない)。
MOVED_DELTAS = frozenset(
    {
        "opened",
        "hypothesis_flip",
        "strengthened",
        "weakened",
        "escalated",
        "reopened",
        "claim_revised",
    }
)
DEFAULT_WINDOW_HOURS = 24
_CONF_JA: dict[str, str] = {"high": "高確度", "moderate": "中確度", "low": "低確度"}
#: 動かない問いの 1 行で答えを切る長さ (全文は Web の問いの面で読む)。
_STILL_CLAIM_CHARS = 60
_COMPACT_CLAIM_CHARS = 90
_SEP = "━━━━━━━━━━"


@dataclass(frozen=True)
class Move:
    """窓の中の 1 回の動き。"""

    at: str
    delta_type: str
    reason: str

    @property
    def label(self) -> str:
        vocab = get_vocabulary("delta_type")
        return vocab.label_for(self.delta_type) if vocab else self.delta_type


@dataclass(frozen=True)
class QuestionStatus:
    situation_id: str
    question: str
    claim: str
    confidence: str
    moves: tuple[Move, ...]
    fired_indicators: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    days_since_evidence: int | None
    open_indicators: int

    @property
    def confidence_label(self) -> str:
        return _CONF_JA.get(self.confidence, self.confidence)


@dataclass(frozen=True)
class PirBrief:
    total: int
    moved: tuple[QuestionStatus, ...]
    still: tuple[QuestionStatus, ...]
    unassessed: int
    window_hours: int


def _parse_ts(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def _moves_in_window(card: dict[str, Any], since: datetime) -> tuple[Move, ...]:
    moves: list[Move] = []
    for rev in card.get("trajectory") or []:
        delta = str(rev.get("delta_type") or "")
        at = _parse_ts(rev.get("at"))
        if delta not in MOVED_DELTAS or at is None or at < since:
            continue
        reason = str(rev.get("reason") or rev.get("note") or "").strip()
        moves.append(Move(at=str(rev.get("at")), delta_type=delta, reason=reason))
    # 新しい順 (読み手は最新の動きから読む)
    return tuple(reversed(moves))


def _status(card: dict[str, Any], since: datetime, now: datetime) -> QuestionStatus:
    last = _parse_ts(card.get("last_evidence_at"))
    return QuestionStatus(
        situation_id=str(card.get("situation_id") or ""),
        question=str(card.get("question") or ""),
        claim=str(card.get("claim") or "").strip(),
        confidence=str(card.get("confidence") or ""),
        moves=_moves_in_window(card, since),
        fired_indicators=tuple(str(x) for x in card.get("fired_indicators") or []),
        missing_evidence=tuple(str(x) for x in card.get("missing_evidence") or []),
        days_since_evidence=max((now - last).days, 0) if last else None,
        open_indicators=sum(
            1 for i in card.get("indicators") or [] if str(i.get("status")) == "open"
        ),
    )


def build_pir_brief(
    cards: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> PirBrief:
    """posture カード列 → PIR ブリーフ (純粋関数)。問いの順は入力順を保つ。"""
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=window_hours)
    assessed = [c for c in cards if c.get("assessed")]
    statuses = [_status(c, since, now) for c in assessed]
    return PirBrief(
        total=len(cards),
        moved=tuple(s for s in statuses if s.moves),
        still=tuple(s for s in statuses if not s.moves),
        unassessed=len(cards) - len(assessed),
        window_hours=window_hours,
    )


def headline(brief: PirBrief) -> str:
    """冒頭の 1 行。"""
    text = f"本日、{brief.total} 問中 {len(brief.moved)} 問の答えが動いた"
    if brief.unassessed:
        text += f" (未評価 {brief.unassessed} 問)"
    return text


def _freshness(s: QuestionStatus) -> str:
    if s.days_since_evidence is None:
        return "証拠の日付不明"
    if s.days_since_evidence == 0:
        return "証拠は本日"
    return f"最後の証拠から {s.days_since_evidence} 日"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _format_moved(s: QuestionStatus) -> list[str]:
    lines = [f"▶ {s.question}", f"  答え ({s.confidence_label}): {s.claim}"]
    for m in s.moves:
        lines.append(f"  {m.label}: {m.reason}" if m.reason else f"  {m.label}")
    if s.fired_indicators:
        lines.append(f"  観測された指標: {' / '.join(s.fired_indicators)}")
    if s.missing_evidence:
        lines.append(f"  まだ分からないこと: {s.missing_evidence[0]}")
    return lines


def _format_still(s: QuestionStatus) -> str:
    return (
        f"・{s.question} — {_clip(s.claim, _STILL_CLAIM_CHARS)} ({s.confidence_label}・"
        f"{_freshness(s)}・待っている指標 {s.open_indicators})"
    )


def format_pir_brief_full(brief: PirBrief) -> str:
    """Web の日次ブリーフ (全文) 用。問いが 1 つも無ければ空文字。"""
    if brief.total == 0:
        return ""
    lines = ["■ PIR ブリーフ (常設の問いへの答え)", headline(brief)]
    for s in brief.moved:
        lines += ["", *_format_moved(s)]
    if brief.still:
        lines += ["", "動いていない問い:"]
        lines += [_format_still(s) for s in brief.still]
    return "\n".join(lines)


def format_pir_brief_compact(brief: PirBrief) -> str:
    """Discord の要点用。動いた問いだけを 1 行ずつ (動かない問いは Web で)。"""
    if brief.total == 0:
        return ""
    lines = [f"**PIR** {headline(brief)}"]
    for s in brief.moved:
        move = s.moves[0]
        why = f" — {move.reason}" if move.reason else ""
        lines.append(f"・{s.question}: {move.label}{why}")
        lines.append(f"　→ {_clip(s.claim, _COMPACT_CLAIM_CHARS)} ({s.confidence_label})")
    return "\n".join(lines)


def _status_payload(s: QuestionStatus) -> dict[str, Any]:
    return {
        "situation_id": s.situation_id,
        "question": s.question,
        "claim": s.claim,
        "confidence": s.confidence,
        "moves": [
            {"at": m.at, "delta_type": m.delta_type, "label": m.label, "reason": m.reason}
            for m in s.moves
        ],
        "fired_indicators": list(s.fired_indicators),
        "missing_evidence": list(s.missing_evidence),
        "days_since_evidence": s.days_since_evidence,
        "open_indicators": s.open_indicators,
    }


def pir_brief_payload(brief: PirBrief) -> dict[str, Any]:
    """Web 構造描画用の payload (daily_briefs.payload["pir_brief"])。"""
    return {
        "headline": headline(brief),
        "total": brief.total,
        "moved_count": len(brief.moved),
        "unassessed": brief.unassessed,
        "window_hours": brief.window_hours,
        "moved": [_status_payload(s) for s in brief.moved],
        "still": [_status_payload(s) for s in brief.still],
    }


def collect_pir_brief(*, now: datetime | None = None) -> PirBrief:
    """DB から常設の問いを読んでブリーフを組む (朝ブリーフの runner 用)。"""
    from src.ui.services.standing_posture import build_standing_posture

    return build_pir_brief(build_standing_posture(now=now), now=now)


__all__ = [
    "MOVED_DELTAS",
    "Move",
    "PirBrief",
    "QuestionStatus",
    "build_pir_brief",
    "collect_pir_brief",
    "format_pir_brief_compact",
    "format_pir_brief_full",
    "headline",
    "pir_brief_payload",
]
