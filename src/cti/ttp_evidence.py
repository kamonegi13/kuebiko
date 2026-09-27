"""TTP の本文裏付け (2026-09-27) — LLM が付けた ATT&CK 技術を、本文に手口の記述があるときだけ採る。

LLM の TTP は本文で裏付けられるのが 3-4 割しかない (Opus 盲検、
docs/research/event_knowledge_graph.md §19)。定番の技術を当て推量で付けるため、
手法の次元 (グラフ・キャンペーンの線・アクターの手口) を汚していた。

照合の順 (最初に当たったものを根拠の種類として返す):
1. ``id``      本文に T 番号そのもの (副技術は親の番号でも可)
2. ``name``    ATT&CK の技術名 (英語、辞書 data/cti/attack_techniques.json)
3. ``keyword`` config/cti/ttp_evidence.yaml の言い回し (日本語・同義の手口)

照合は決定論 (LLM なし)。語彙の無い技術は ID と名前だけで判定する。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from src.cti.attack_techniques import load_technique_catalog
from src.logging_config import get_logger

_log = get_logger(__name__)

DEFAULT_EVIDENCE_PATH = Path("config/cti/ttp_evidence.yaml")

EvidenceKind = Literal["id", "name", "keyword"]


@lru_cache(maxsize=2)
def load_evidence_patterns(
    path: str = str(DEFAULT_EVIDENCE_PATH),
) -> dict[str, tuple[re.Pattern[str], ...]]:
    """技術 ID → 裏付け語の正規表現。ファイルが無ければ空 (ID と名前だけで判定する)。"""
    p = Path(path)
    if not p.exists():
        _log.warning("ttp_evidence_patterns_missing", path=path)
        return {}
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 技術 ID → 正規表現の一覧の形ではない")
    out: dict[str, tuple[re.Pattern[str], ...]] = {}
    for tid, pats in raw.items():
        if not isinstance(pats, list):
            raise ValueError(f"{path}: {tid} の値が一覧ではない")
        out[str(tid).upper()] = tuple(re.compile(str(x), re.IGNORECASE) for x in pats)
    return out


def _id_pattern(technique_id: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w.]){re.escape(technique_id)}(?![\d])", re.IGNORECASE)


def find_ttp_evidence(
    technique_id: str,
    text: str,
    *,
    patterns: Mapping[str, tuple[re.Pattern[str], ...]] | None = None,
    names: Mapping[str, str] | None = None,
) -> EvidenceKind | None:
    """本文 ``text`` が技術 ``technique_id`` を裏付けるか。根拠の種類 (無ければ None)。"""
    tid = technique_id.strip().upper()
    if not tid or not text:
        return None
    parent = tid.split(".", 1)[0]
    if _id_pattern(tid).search(text) or (parent != tid and _id_pattern(parent).search(text)):
        return "id"
    name_map = names if names is not None else _technique_names()
    name = name_map.get(tid, "")
    if len(name) >= _MIN_NAME_CHARS and name.lower() in text.lower():
        return "name"
    pats = (patterns if patterns is not None else load_evidence_patterns()).get(tid, ())
    if any(p.search(text) for p in pats):
        return "keyword"
    return None


#: 技術名がこれより短いと一般語に当たりすぎる (例 "Bash" は語彙側で扱う)
_MIN_NAME_CHARS = 8


@lru_cache(maxsize=1)
def _technique_names() -> dict[str, str]:
    return {tid: info.name for tid, info in load_technique_catalog().items()}


#: 関門の動作 (on = 裏付けの無い LLM の技術を落とす / shadow = 記録だけ / off = 無効)
GATE_ENV = "TTP_EVIDENCE_GATE"


def gate_mode() -> str:
    """``TTP_EVIDENCE_GATE`` (既定 on)。未知の値は on として扱う。"""
    import os

    mode = os.environ.get(GATE_ENV, "on").strip().lower()
    return mode if mode in ("on", "shadow", "off") else "on"


def filter_llm_techniques(techniques: list[str], text: str, *, article_id: str = "") -> list[str]:
    """LLM が付けた技術のうち、本文で裏付けられるものだけを返す (新しい一覧)。

    T 番号でない値 (CVE 等が混ざる) はそのまま通す。本文が空なら判定できないので全部通す
    (関門の失敗で抽出を変えない)。
    """
    mode = gate_mode()
    if mode == "off" or not text.strip():
        return list(techniques)
    kept: list[str] = []
    dropped: list[str] = []
    for t in techniques:
        norm = str(t).strip().upper()
        is_technique = norm.startswith("T") and norm[1:2].isdigit()
        if not is_technique or find_ttp_evidence(norm, text) is not None:
            kept.append(t)
        else:
            dropped.append(norm)
    if dropped:
        _log.info(
            "ttp_evidence_gate_drop",
            mode=mode,
            article_id=article_id,
            dropped=dropped[:10],
            kept=len(kept),
        )
    return kept if mode == "on" else list(techniques)
