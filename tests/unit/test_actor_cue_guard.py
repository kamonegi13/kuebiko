"""actor_aliases.yaml の context_cues 衛生 guard (docs/actor_identity_cue_design.md §3.2)。

ジャンル語 cue (ransomware/諜報 等) は CTI コーパスで常に成立し曖昧解消にならない。
Tick 汚染 (2026-07-30 の誤帰属) の再発を、実 yaml に対する常設テストで構造的に遮断する。
新規アクター追加・cue 編集・同期 job のどの経路で混入しても CI で検出される。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.cti.actor_normalizer import GENRE_CUE_WORDS

_YAML = Path("config/cti/actor_aliases.yaml")


def _load_actors() -> list[dict[str, Any]]:
    raw = yaml.safe_load(_YAML.read_text(encoding="utf-8"))
    lst = raw if isinstance(raw, list) else raw.get("actors", [])
    return [a for a in lst if isinstance(a, dict) and "id" in a]


def test_context_cues_contain_no_genre_words() -> None:
    """全アクターの context_cues にジャンル語が 1 つも無いこと。"""
    violations: list[str] = []
    for actor in _load_actors():
        cues: list[str] = list(actor.get("context_cues") or [])
        for cue in cues:
            if str(cue).lower().strip() in GENRE_CUE_WORDS:
                violations.append(f"{actor['id']}: '{cue}'")
    assert not violations, (
        "context_cues にジャンル語が混入 (同一性の曖昧解消に使えず誤帰属の温床): "
        + ", ".join(violations)
        + " — 固有の別名/マルウェア名/作戦名に置き換えること"
    )


def test_ambiguous_actors_are_declared() -> None:
    """ambiguous フラグの存在確認 (照合ゲートの前提が消えていないこと)。"""
    ambiguous = [a["id"] for a in _load_actors() if a.get("ambiguous")]
    # 既知の一般語衝突アクターが ambiguous を失っていないこと (回帰防止)
    for required in ("tick", "play", "deadlock", "chaos", "anonymous"):
        assert required in ambiguous, f"{required} の ambiguous フラグが消えている"
