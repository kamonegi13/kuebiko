"""問いの起草と資格判定 — 単発 (RFI) から常設情報要求への昇格関門。

設計 docs/pir_brief_design.md §6c:

> **すべての問いが常設情報要求になれるわけではない。**
> 作るべきは大きな型のライブラリではなく**この資格判定**であり、
> 通った問いを型へ写像する経路である。

**6 要件のうち 3 つは型の選択が構造的に保証する** — 関門で再判定しない:

| 要件 | 誰が保証するか |
|---|---|
| 1 競合仮説が立つ | **型** (仮説骨格を持つ) |
| 2 証拠条件が宣言的に書ける | **関門** (`PROPERTY_CATALOG` で検証) |
| 3 答えが時間で動きうる | **人** — 機械に判定できない |
| 4 予測でない | **型の不在** (予測の型を用意しない) |
| 5 自分側データを要しない | **型の不在** (FFIR の型を用意しない) |
| 6 決心が言える | **関門** (`decision` が空でない) |

⭐ 原則 G (予測しない) / F (自分側データ) を**指示でなく型の不在で守る**のがこの設計の
要点。禁止は指示では止まらない (docs: deterministic_gates_over_instructions)。

要件 3 だけは機械に判定できないので、起草者の明示的な確認 (`answer_can_move_ack`) を
求める。黙って通さないことが目的で、確認そのものが真実性を保証するわけではない。

**問いはデータ** (§6e) — このモジュールは型を持つだけで、具体的な問いを持たない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.assessment.question_frame import FRAME_BY_ID, render_question

#: 語彙解決に使う domain → 値の集合を引く関数 (表示ラベルではなく **値の妥当性**)。
#: 表示は生値へ fallback するが、台帳に載る問いは語彙統制の内側でなければならない。
_VOCAB_DOMAINS = ("countries", "intents", "nisc_sectors")


@dataclass(frozen=True)
class QuestionDraft:
    """起草された問い 1 件。**データ** — コード所有の seed ではない (§6e)。"""

    frame_id: str
    slots: dict[str, str]
    #: 要件 6 — この問いがどの決心を支えるか。運用者ごとに違うのでコードは一覧を持たない。
    decision: str
    #: 要件 2 — 証拠の適格条件 (routing/PIR と共有の match ツリー)。
    evidence_condition: dict[str, Any] = field(default_factory=dict)
    #: 要件 3 — 機械に判定できないため起草者が明示的に確認する。
    answer_can_move_ack: bool = False
    note: str = ""


@dataclass(frozen=True)
class QualificationResult:
    """資格判定の結果。**落ちた理由を全部返す** (1 つ直すたびに次が出る、を避ける)。"""

    ok: bool
    failures: tuple[str, ...]


def question_text(draft: QuestionDraft) -> str:
    """起草内容から問い文を組み立てる (型が SSoT)。"""
    frame = FRAME_BY_ID[draft.frame_id]
    return render_question(frame, draft.slots)


def _domain_values(domain: str) -> set[str] | None:
    """domain の許容値。引けないときは None (語彙検証を skip = 起草を殺さない)。"""
    try:
        if domain == "countries":
            from src.vocab.registry import get_vocabulary

            vocab = get_vocabulary("country")
            return {i.value.upper() for i in vocab.items} if vocab else None
        if domain == "intents":
            from src.cti.diamond_model import SOCIO_POLITICAL_INTENTS

            return set(SOCIO_POLITICAL_INTENTS)
        if domain == "nisc_sectors":
            from src.assessment.question_frame import SCOPE_ALL_CI
            from src.cti.nisc_sectors import NISC_SECTORS

            return {SCOPE_ALL_CI, *NISC_SECTORS}
    except Exception:  # noqa: BLE001 — 語彙が引けないことを起草の失敗にしない
        return None
    return None


def _slot_failures(draft: QuestionDraft) -> list[str]:
    frame = FRAME_BY_ID[draft.frame_id]
    out: list[str] = []
    for slot in frame.slots:
        value = str(draft.slots.get(slot.slot_id, "")).strip()
        if not value:
            out.append(f"スロット '{slot.slot_id}' ({slot.label}) が埋まっていません")
            continue
        allowed = _domain_values(slot.domain)
        if allowed is None:
            continue
        probe = value.upper() if slot.domain == "countries" else value
        if probe not in allowed:
            out.append(
                f"スロット '{slot.slot_id}' の値 '{value}' が語彙 ({slot.domain}) にありません"
            )
    return out


def _evidence_failures(draft: QuestionDraft) -> list[str]:
    """要件 2 — routing と同じ検証器を使う (照合器を二重に持たない)。"""
    if not draft.evidence_condition:
        return ["証拠条件が空です (全件該当になり、問いの証拠として機能しません)"]
    from src.config_loader import KNOWN_ARTICLE_CATEGORIES
    from src.cti.routing_rules import _validate_condition

    return _validate_condition(
        draft.evidence_condition, set(KNOWN_ARTICLE_CATEGORIES), "evidence_condition"
    )


def qualify(draft: QuestionDraft) -> QualificationResult:
    """常設情報要求としての資格を判定する。

    要件 1/4/5 はここで見ない — 型の選択が構造的に保証するため
    (型が仮説骨格を持つ / 予測・FFIR の型を用意しない)。
    """
    if draft.frame_id not in FRAME_BY_ID:
        known = sorted(FRAME_BY_ID)
        return QualificationResult(False, (f"未知の型 '{draft.frame_id}' (可: {known})",))

    failures: list[str] = [*_slot_failures(draft), *_evidence_failures(draft)]
    if not draft.decision.strip():
        failures.append("要件 6: この問いがどの決心を支えるかが書かれていません")
    if not draft.answer_can_move_ack:
        failures.append(
            "要件 3: 答えが時間で動きうることの確認がありません"
            " (機械に判定できないため起草者の明示が要ります)"
        )
    return QualificationResult(not failures, tuple(failures))
