"""アクターの役割 (攻撃主体 vs 報告/防御機関) の判定 (2026-07-27)。

docs/body_extraction_and_entity_integrity_redesign.md §5 (D1)。

攻撃の帰属集計 (地図の攻撃フロー・国家情勢ボードの攻撃元・概況の国籍別件数) は
``article_entities`` の **言及 (mention)** actor を数えており、共同アドバイザリを発行した
同盟国の **報告/防御機関** (NSA / US Cyber Command / GCHQ / Mossad 等) が「攻撃元アクター」
として混入していた (実測 44 記事)。

これらの機関は辞書上 ``family='state_organ'`` かつ **同盟国 nation** で登録されている。
敵性国家の state_organ (GRU/MSS/RGB 等) は正当な攻撃主体なので除外しない。この非対称性で
「報告機関の言及」だけを帰属集計から外す (subject/mention 分離の集計層への適用)。
"""

from __future__ import annotations

from src.cti.actor_normalizer import load_actor_aliases

# 敵性国家 (これらの state_organ は正当な攻撃主体 = 除外しない)。situation/overview と同一集合。
_ADVERSARY_NATIONS: frozenset[str] = frozenset({"cn", "ru", "kp", "ir"})


def reporter_org_actor_ids() -> frozenset[str]:
    """攻撃帰属に数えるべきでない「報告/防御機関」の actor id 集合。

    辞書の ``family='state_organ'`` かつ nation が敵性国家でないエントリ
    (= 同盟国の情報/サイバー機関)。共同アドバイザリの発行者として本文に頻出するが
    攻撃実行主体ではない。辞書 mtime で自動リロードされる registry を参照する。
    """
    registry = load_actor_aliases()
    ids: set[str] = set()
    for a in registry.actors:
        nation = (a.nation or "").strip().lower()
        if getattr(a, "family", "") == "state_organ" and nation not in _ADVERSARY_NATIONS:
            ids.add(a.id)
    return frozenset(ids)
