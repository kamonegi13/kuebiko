"""問いの型 (QuestionFrame) — 文型 + スロット + 仮説骨格。

設計: docs/pir_brief_design.md §6c/§6d/§6e。

**型 = 3 つの部品**: ①文型とスロット ②競合仮説の骨格 ③証拠の適格条件。
③ は段B-1 で `PROPERTY_CATALOG` に `intent` / `victim_country` / `involved_country` が
載ったことで match ツリー (データ) として書けるようになったため、ここには持たない。

**穴埋め方式の利点** (§6d): スロットの語彙が照合器と同じ SSoT から来るので、
**穴を埋めた時点で 3 つが同時に決まる** — 問い文 / 証拠条件 / 仮説文。
よってスロットは必ず `domain` を宣言する (複製辞書を作らない)。

**§6e 公開ツールとしての制約**: 本ツールは MIT 公開で、運用者ごとに任務も決心も違う。
- コードが所有するのは**型だけ**。具体的な問い・主語・決心はデータ
- 文型に特定の国名・分野名を焼き込まない (焼き込むと別用途で使えない)
- 決心 (要件 6) は問いのデータに持つ。frame は「要る」と宣言するだけで
  **決心の一覧を持たない**

⚠ 現行の `STANDING_SEEDS` (standing.py) はコード所有の問い 4 件で、この原則に反している。
型が出揃った段階でデータへ移す。それまで問い文は両方に在るため、
``tests/unit/test_question_frame.py`` が二重管理のずれを検知する。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.synthesis.grounded.hypotheses import POSTURE_HYPOTHESES, Hypothesis

#: 分野スロットの「全重要インフラ」を表す値。R1-R3 の ``is_ci_sector`` (NISC 全分野) と
#: 同義で、証拠条件では ``victim_sector in <NISC 集合>`` に展開される。
#: 単独分野を選べば ``victim_sector eq <その分野>`` になる。
SCOPE_ALL_CI = "critical_infrastructure"

#: 語彙に載らない特殊スコープの表示名 (NISC 分野そのものは nisc_sectors が SSoT)。
_SCOPE_LABELS: dict[str, str] = {SCOPE_ALL_CI: "重要インフラ"}


@dataclass(frozen=True)
class SlotSpec:
    """文型の穴 1 つ。``domain`` は語彙 SSoT の名前 (PROPERTY_CATALOG と同じ名前空間)。"""

    slot_id: str
    label: str
    domain: str


@dataclass(frozen=True)
class QuestionFrame:
    """問いの型。**汎用フレームのみ** — 用途固有の値はデータ側 (§6e)。"""

    frame_id: str
    label: str
    template: str
    slots: tuple[SlotSpec, ...]
    hypotheses: tuple[Hypothesis, ...]
    #: 要件 6 — どの決心を支えるかが言えない問いは常設にしない。
    #: 決心の中身はデータ (運用者ごとに違うため、ここには一覧を置かない)。
    requires_decision: bool = True


def _country_label(value: str) -> str | None:
    """ISO 国コード → 表示名 (SSoT = config/cti/countries.yaml、vocab registry 経由)。"""
    try:
        from src.vocab.registry import get_vocabulary

        vocab = get_vocabulary("country")
    except Exception:  # noqa: BLE001 — 語彙が引けなくても問い文は出す
        return None
    if vocab is None:
        return None
    upper = value.upper()
    return next((i.label for i in vocab.items if i.value.upper() == upper), None)


def _intent_label(value: str) -> str | None:
    """intent → 表示名 (SSoT = src/cti/diamond_model.INTENT_LABELS_JA)。"""
    from src.cti.diamond_model import INTENT_LABELS_JA

    return INTENT_LABELS_JA.get(value)


def _sector_label(value: str) -> str | None:
    """分野 → 表示名。全 CI の特殊値以外は NISC 分野の名前をそのまま使う。"""
    if value in _SCOPE_LABELS:
        return _SCOPE_LABELS[value]
    from src.cti.nisc_sectors import NISC_SECTORS

    return NISC_SECTORS.get(value)


#: domain → 表示名の解決器。**すべて既存 SSoT を引く** (ここに辞書を持たない)。
_LABEL_RESOLVERS = {
    "countries": _country_label,
    "intents": _intent_label,
    "nisc_sectors": _sector_label,
}


def _slot_label(slot: SlotSpec, value: str) -> str:
    """スロット値 → 表示名。語彙に無ければ生値 (編集途中で画面を壊さない)。"""
    resolver = _LABEL_RESOLVERS.get(slot.domain)
    return (resolver(value) if resolver else None) or value


def render_question(frame: QuestionFrame, values: Mapping[str, str]) -> str:
    """文型にスロット値を差し込んで問い文を作る。

    Raises:
        KeyError: 埋まっていないスロットがあるとき。**穴の空いた問いを黙って作らない**
            — 問いは不変で動くのは答えだけ、という不変条件を入口で守る (§6d)。
    """
    rendered = {s.slot_id: _slot_label(s, values[s.slot_id]) for s in frame.slots}
    return frame.template.format(**rendered)


# ---- 型 A: 存在・進行 (現行の posture 4 問がこの型のインスタンス) ----
FRAME_PRESENCE = QuestionFrame(
    frame_id="presence",
    label="存在・進行",
    template="{subject}の国家アクターは{target_country}の{target_scope}に対する{action}を進めているか",
    slots=(
        SlotSpec("subject", "主体 (国)", "countries"),
        SlotSpec("target_country", "対象国", "countries"),
        SlotSpec("target_scope", "対象分野", "nisc_sectors"),
        SlotSpec("action", "行為", "intents"),
    ),
    hypotheses=POSTURE_HYPOTHESES,
)


FRAMES: tuple[QuestionFrame, ...] = (FRAME_PRESENCE,)
FRAME_BY_ID: dict[str, QuestionFrame] = {f.frame_id: f for f in FRAMES}
