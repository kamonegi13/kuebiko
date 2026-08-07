"""frontend の再生 guard (docs/vocabulary_label_architecture.md §4.6)。

backend 配信 vocab へ移行済みの静的ラベルマップが frontend に再生していないことを、
同一 pytest 内で保証する。移行済み識別子が **宣言として** 再登場したら失敗 =
「また labels.ts / 各画面に静的マップを作った」を CI で弾く。値ラベルは backend vocab
(useVocab / vocabLabel) が唯一の SSoT。
"""

from __future__ import annotations

import re
from pathlib import Path

# 多コピーのドリフト源だったため vocab へ集約済みの静的ラベルマップ識別子。
_MIGRATED = (
    "IMPORTANCE_LABELS",
    "RUN_STATUS_LABELS",
    "STANCE_LABELS",
    "ARTICLE_STATUS_LABELS",
    "TRIGGER_LABELS",
    "HEALTH_STATUS_LABELS",
    "CATEGORY_LABELS",
    "CATEGORY_GROUP_LABELS",
    "CONFIDENCE_LABELS",
    "INTENT_CONFIDENCE_LABEL",
    "ARTICLE_TYPE_LABELS",
    "SECTOR_LABELS",
    "COUNTRY_LABELS",
    "NATION_LABELS",
    "TRANSPORT_LABEL",
    "PREVIEW_KIND_LABEL",
    # long-tail (単一 consumer だったが将来 multi-copy 化させないため封じる)
    "DELTA_JA",
    "POLARITY_JA",
    "ASSIGNED_BY_JA",
    "HYP_LABEL",
    "ACTIVITY_STATE_LABEL",
    "ENTITY_TYPE_LABELS",
    "MATCHED_VIA_LABEL",
    "DETECTED_VIA_LABEL",
    "ACTOR_KIND_LABEL",
    "EVENT_BASIS_LABEL",
    "INTENT_OPTIONS",
)

_FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"
_DECL = re.compile(r"\b(?:const|let|export const)\s+(?:" + "|".join(_MIGRATED) + r")\b")


def test_migrated_label_maps_not_regenerated() -> None:
    if not _FRONTEND_SRC.exists():
        return  # frontend が同梱されない実行環境では skip
    offenders: list[str] = []
    for path in _FRONTEND_SRC.rglob("*.ts*"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("//") or stripped.startswith("*"):
                continue  # コメントは対象外 (「再生しないこと」の注記を許容)
            if _DECL.search(line):
                offenders.append(f"{path.relative_to(_FRONTEND_SRC)}:{lineno}: {stripped[:80]}")
    assert not offenders, (
        "移行済みの静的ラベルマップが再生している"
        " (backend vocab + vocabLabel/useVocab を使うこと):\n" + "\n".join(offenders)
    )
