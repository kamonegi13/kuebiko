"""判定基準エディタの表示メタ (rubric_guide) の unit test (WP-B1)。

肝は **網羅の機械強制** と **根拠の実在検証**:

- ``SummaryOutput`` にフィールドを足して ``FIELD_GUIDE`` への追記を忘れると赤くなる
  (``fill_rate_audit.METRICS`` と同じ「新フィールド追加 PR は 1 行足す」規約)。
- ``sources`` (効く先の根拠コード) が消えたら赤くなる。根拠は腐らせない。
"""

from __future__ import annotations

from pathlib import Path

from src.pipeline.summary import SummaryOutput
from src.prompts.rubric_guide import (
    FIELD_GUIDE,
    GROUPS,
    OTHER_GROUP_ID,
    UNMAPPED_ORDER,
    guide_payload,
    resolve_group,
)
from src.prompts.rubric_store import load_seed_rubric

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED_PATH = REPO_ROOT / "config" / "prompts" / "summarizer_rubric.yaml"

# 「効く先」はカード内の 1 行。長くなると読まれない (UI が折り返して枠を壊す)。
MAX_EFFECT_CHARS = 120


def test_field_guide_covers_every_summary_output_field() -> None:
    """契約 (SummaryOutput) の全フィールドに表示メタがある = unmapped が構造的に空。"""
    # Arrange
    contract = set(SummaryOutput.model_fields)

    # Act
    mapped = set(FIELD_GUIDE)

    # Assert
    assert mapped - contract == set(), "SummaryOutput に無いフィールドの定義が残っている"
    assert contract - mapped == set(), "SummaryOutput に足したフィールドの定義が漏れている"
    assert len(FIELD_GUIDE) == 24


def test_every_field_belongs_to_a_defined_group() -> None:
    """group id が GROUPS に実在し、fail-open 用の other は誰も名乗らない。"""
    group_ids = {g.id for g in GROUPS}

    unknown = {g.field_id: g.group for g in FIELD_GUIDE.values() if g.group not in group_ids}

    assert unknown == {}
    # other は「定義漏れの受け皿」であり、明示的な割当先ではない
    assert not [g.field_id for g in FIELD_GUIDE.values() if g.group == OTHER_GROUP_ID]


def test_order_is_a_gapless_sequence_within_each_group() -> None:
    """グループ内 order が 1..N の重複なし連番 (表示順が決定的である保証)。"""
    by_group: dict[str, list[int]] = {}
    for guide in FIELD_GUIDE.values():
        by_group.setdefault(guide.group, []).append(guide.order)

    offenders = {
        group: sorted(orders)
        for group, orders in by_group.items()
        if sorted(orders) != list(range(1, len(orders) + 1))
    }

    assert offenders == {}
    # §1 の決定表と同じ内訳 (2 + 5 + 4 + 4 + 9 = 24)。
    # 2026-08-18: LLM が実際には返していなかった 3 つを suppressed へ移動した。
    # article_type (出力が 100% breaking に枯死) / routing_flags・pmesii_axes
    # (gold set 258 件で返却ゼロ、値は決定論層と judgment 分類器が供給)。
    # 「routing」群は所属 0 になったため定義ごと消えている。
    assert {g: len(o) for g, o in by_group.items()} == {
        "classification": 2,
        "victim": 5,
        "technical": 4,
        "narrative": 4,
        "suppressed": 9,
    }


def test_effect_text_is_present_and_short() -> None:
    """「効く先」が全件あり、1 行に収まる長さであること。"""
    offenders = [
        guide.field_id
        for guide in FIELD_GUIDE.values()
        if not guide.effect.strip() or len(guide.effect) > MAX_EFFECT_CHARS
    ]

    assert offenders == [], f"effect が空 / {MAX_EFFECT_CHARS} 字超: {offenders}"


def test_sources_point_at_existing_modules() -> None:
    """根拠として示す消費者コードが実在する (リファクタで腐ったら赤くする)。"""
    missing = [
        f"{guide.field_id}: {source}"
        for guide in FIELD_GUIDE.values()
        for source in guide.sources
        if not (REPO_ROOT / source).exists()
    ]

    assert missing == [], "存在しない根拠パス:\n" + "\n".join(missing)
    assert all(guide.sources for guide in FIELD_GUIDE.values()), "根拠の無い effect がある"


def test_resolve_group_prefers_kind_and_fails_open() -> None:
    """kind=suppressed が最優先 / 未登録は other に fail-open する。"""
    # kind は compose() の実挙動 (本文が空ならプロンプトに出ない) と表示を一致させる
    assert resolve_group("bluf", "suppressed") == "suppressed"
    # 宣言側の kind が rubric でも、guide 上 suppressed 群のフィールドは動かない
    assert resolve_group("bluf", "rubric") == "suppressed"
    assert resolve_group("category", "rubric") == "classification"
    # 定義漏れでも UI からカードを消さない
    assert resolve_group("brand_new_field", "rubric") == OTHER_GROUP_ID
    assert resolve_group("brand_new_field", "suppressed") == "suppressed"


def test_guide_payload_maps_every_seeded_section() -> None:
    """seed yaml の 24 セクションが全件 guide に載り、未定義が 0 件であること。"""
    # Arrange
    rubric = load_seed_rubric(SEED_PATH)
    assert rubric is not None, f"seed yaml を読めない: {SEED_PATH}"
    section_ids = [s.field_id for s in rubric.sections]
    kinds = {s.field_id: s.kind for s in rubric.sections}

    # Act
    payload = guide_payload(section_ids, kinds)

    # Assert
    fields = payload["fields"]
    assert isinstance(fields, list)
    assert len(fields) == len(section_ids) == 24
    assert payload["unmapped_fields"] == []
    assert {f["field_id"] for f in fields} == set(SummaryOutput.model_fields)
    # kind=suppressed の 9 件は yaml の宣言どおり suppressed 群に入る
    assert sum(1 for f in fields if f["group"] == "suppressed") == 9
    assert all(f["effect"] for f in fields)


def test_guide_payload_flags_unknown_fields() -> None:
    """契約に無いフィールドは other + unmapped_fields で申告される (定義漏れの可視化)。"""
    payload = guide_payload(["category", "brand_new_field"], {})

    fields = payload["fields"]
    assert isinstance(fields, list)
    assert payload["unmapped_fields"] == ["brand_new_field"]
    unknown = next(f for f in fields if f["field_id"] == "brand_new_field")
    assert unknown["group"] == OTHER_GROUP_ID
    assert unknown["order"] == UNMAPPED_ORDER
    assert unknown["effect"] == ""
    assert unknown["sources"] == []
