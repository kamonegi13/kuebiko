"""続報の「何が加わったか」の復元 (src/eventnews/version_diff.py)。"""

from __future__ import annotations

import json

from src.eventnews import version_diff
from src.eventnews.models import UPDATE_DRIVER_TYPES


def test_labels_cover_all_driver_entity_types() -> None:
    # Arrange / Act / Assert — 型が増えたら日本語ラベルも足す (生 enum を画面に出さない)
    assert set(version_diff.DRIVER_TYPE_LABELS) == set(UPDATE_DRIVER_TYPES)


def test_normalized_key_maps_back_to_source_wording() -> None:
    # Arrange — 記録されるのは正規化キー、記事側は原文表記
    payload = json.dumps(
        {"article_id": "a1", "added_entities": {"victim_org": ["federalreserveboard"]}}
    )
    rows = [("victim_org", "Federal Reserve Board"), ("victim_org", "NASA")]

    # Act
    added = version_diff.resolve_additions(payload, rows)

    # Assert — 加わった 1 件だけが、読める形で返る
    assert added == [
        {"type": "victim_org", "label": "被害組織", "values": ["Federal Reserve Board"]}
    ]


def test_unresolvable_keys_are_dropped() -> None:
    # Arrange — 記事側に対応する entity が残っていない (再抽出等でずれた場合)
    payload = json.dumps({"article_id": "a1", "added_entities": {"cve": ["cve-2026-1"]}})

    # Act / Assert — 読めない文字列を画面に出すくらいなら何も出さない
    assert version_diff.resolve_additions(payload, []) == []


def test_malformed_json_does_not_raise() -> None:
    # Arrange / Act / Assert — 表示経路なので、記録が壊れていても記事は出す
    assert version_diff.resolve_additions("{not json", []) == []
    assert version_diff.contributing_article_id(None) is None
    assert version_diff.corroboration_note("") == ""


def test_corroboration_change_is_separate_from_new_facts() -> None:
    # Arrange
    payload = json.dumps(
        {
            "article_id": "a1",
            "media_delta": 2,
            "tier_transition": "news->official",
            "importance_transition": "medium->high",
        }
    )

    # Act
    note = version_diff.corroboration_note(payload)

    # Assert
    assert "独立した媒体が 2 件増えた" in note
    assert "一次情報源" in note
    assert "重要度" in note
