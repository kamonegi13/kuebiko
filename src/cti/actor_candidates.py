"""新興アクター候補の採取 (Actor Recall Layer Part C1)。

closed-vocab 辞書が構造的に取りこぼす新顔を、**過剰帰属させずに「候補」として**拾う。
高精度な 2 信号のみ:
  1. **LLM が本文から抽出した primary_actor_id** のうち辞書未一致のもの (自称 crew を含む)。
     LLM は既にこれを毎記事生成しているが、辞書一致時しか使われず捨てられていた。
  2. **ベンダ命名 designation** (Storm-####, UNC####, TA####, CL-XXX-####, TAG-###, DEV-####,
     UAT-####, APT##)。一般語と衝突しない高精度パターン。新興クラスタは正式 Group 化の
     数ヶ月前にこれで報じられる。UAT/APT は taxonomy pattern_5 退役 (2026-08-01) の引き継ぎ。

候補は確定 actor ではない。人承認で辞書化されるまで「暗定」(actor_provisional) として扱う。
設計: docs/actor_recall_layer.md。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

from src.cti.actor_normalizer import ActorAliasRegistry

_HARVEST_ENV = "ACTOR_CANDIDATE_HARVEST"


def harvest_enabled() -> bool:
    """新興候補採取の有効化フラグ (既定 off、deploy-dark)。docker-compose env で注入。

    確定アクター層には無影響 (harvest/暗定永続化のみ gate)。ROUTING_RULES_ENGINE と同型。
    """
    return os.environ.get(_HARVEST_ENV, "0").strip().lower() in ("1", "true", "yes", "on")


# ベンダが新規侵入クラスタに付ける一時 designation。\b は ASCII 境界で機能 (英数記号トークン)。
# 2026-08-01: UAT (Talos) / APT 番号 を追加 — taxonomy pattern_5 (タイトル正規表現の旧 MVP) を
# 退役しリコール層へ一本化するため、旧経路が担っていた designation を引き継ぐ。
# 既知名 (APT28 等) は knows_name で候補化されないため、未知番号のみが暗定に落ちる。
_VENDOR_DESIGNATION_RE = re.compile(
    r"\b(Storm-\d{3,4}|UNC\d{3,5}|TA\d{3,4}|TAG-\d{2,3}|DEV-\d{3,4}|CL-[A-Z]{3}-\d{3,4}"
    r"|UAT-\d{3,5}|APT-?\d{1,3})\b",
    re.IGNORECASE,
)
# LLM が「該当なし」を表す常套句 (actor 名ではない)。候補化しない。
_NON_ACTOR_TOKENS: frozenset[str] = frozenset(
    {"unknown", "n/a", "na", "none", "null", "various", "multiple", "unattributed", "unidentified"}
)

# llm_primary 由来の地政学ノイズ対策 (2026-08-01): 地政学記事では LLM の primary actor が
# 国家・政府・軍・武装組織になり、週次で数十件の非アクター提案がレビュー queue を圧迫した
# (China/Russia/Iran/US Air Force 等 30+ 件/週の実測)。国名は countries.yaml (taxonomy SSoT)
# を参照。統治・軍事の語は **末尾一致のみ** で弾く — 中間一致まで広げると実在の hacktivist
# (Cyber Army of Russia Reborn / IT Army of Ukraine) を巻き込むため。企業名は列挙不能につき
# 人レビューの却下学習 (dedup_key で再提案されない) に委ねる。
_GEOPOLITICAL_SUFFIXES: tuple[str, ...] = (
    "administration",
    "regime",
    "government",
    "military",
    "ministry",
    "parliament",
    "army",
    "navy",
    "air force",
    "coast guard",
    "armed forces",
)
_GEOPOLITICAL_EXTRA: frozenset[str] = frozenset(
    {"u.s", "u.s.", "u.k", "e.u", "nato", "un", "eu", "hezbollah", "houthis", "hamas", "taliban"}
)


@lru_cache(maxsize=1)
def _country_name_keys() -> frozenset[str]:
    """countries.yaml の alias lookup キー (小文字) — 国名判定の SSoT を再利用。"""
    from src.cti.taxonomy_normalizer import load_normalizer

    return frozenset(load_normalizer().country_lookup.keys())


def is_geopolitical_noise(key: str) -> bool:
    """正規化済み候補 key が地政学ノイズ (国名/統治・軍事組織) か (純粋関数)。

    取込 filter (下) / 提案ゲート (actor_candidate_proposals) / 過去蓄積の掃除 script
    (scripts/purge_geopolitical_provisional.py) が共有する SSoT。
    """
    if key in _GEOPOLITICAL_EXTRA or key in _country_name_keys():
        return True
    return any(key == s or key.endswith(" " + s) for s in _GEOPOLITICAL_SUFFIXES)


_EXCERPT_RADIUS = 80
_MIN_KEY_LEN = 3


@dataclass(frozen=True)
class ActorCandidate:
    """辞書未収録の新興アクター候補 (確定 actor ではない)。"""

    raw_name: str
    key: str  # 正規化キー (dedup / actor_provisional entity の value / proposal dedup_key)
    signal: str  # "llm_primary" | "vendor_designation"
    excerpt: str  # 本文中の出現文脈 (proposal レビュー時の根拠)


def normalize_actor_key(name: str) -> str:
    """候補名を dedup/検索用の正規キーに (小文字・空白圧縮・周辺記号除去)。"""
    s = " ".join(name.strip().split()).lower()
    return s.strip(" \t\r\n.,;:\"'()[]{}|/\\")


def _excerpt(body: str, name: str) -> str:
    idx = body.lower().find(name.lower())
    if idx < 0:
        return ""
    start = max(0, idx - _EXCERPT_RADIUS)
    end = min(len(body), idx + len(name) + _EXCERPT_RADIUS)
    return " ".join(body[start:end].split())


def harvest_candidates(
    *, body: str, primary_actor_id: str, registry: ActorAliasRegistry
) -> list[ActorCandidate]:
    """1 記事から辞書未収録の新興アクター候補を採取する (登場順・dedup 済み)。

    既知アクター (ambiguous 含む) は ``registry.knows_name`` で除外。確定帰属は辞書ゲートのまま。
    """
    out: list[ActorCandidate] = []
    seen: set[str] = set()

    def _add(raw: str, signal: str) -> None:
        key = normalize_actor_key(raw)
        if len(key) < _MIN_KEY_LEN or key in seen or key in _NON_ACTOR_TOKENS:
            return
        if registry.knows_name(raw):  # 既に辞書にある actor は候補化しない
            return
        seen.add(key)
        out.append(
            ActorCandidate(
                raw_name=raw.strip(), key=key, signal=signal, excerpt=_excerpt(body, raw)
            )
        )

    # 信号1: 辞書未一致の LLM primary_actor_id (自称 crew を含む新顔)。
    # 地政学ノイズ (国家・政府・軍) はここでのみ弾く (designation 信号には不要)。
    _pid_key = normalize_actor_key(primary_actor_id or "")
    if _pid_key and not is_geopolitical_noise(_pid_key):
        _add(primary_actor_id or "", "llm_primary")

    # 信号2: 本文中のベンダ命名 designation
    for m in _VENDOR_DESIGNATION_RE.finditer(body or ""):
        _add(m.group(0), "vendor_designation")

    return out
