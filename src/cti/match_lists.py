"""語彙拡張② ユーザー定義の宣言的マッチリスト。

運用者が「名前付きキーワード集合」を **コードなし** で定義し、それを routing rule の
条件 (`keyword_list`) として使えるようにする。新フラグ＝検出関数を書く代わりに、
「これらの語に言及する記事」という*宣言的*な検出を config で賄う (in_config の一般化)。

- SSoT は DB (config_store key ``match_lists``)。保存ごとに版履歴 (config-history で revert 可)。
- 検出は case-insensitive な部分文字列マッチ (title+bluf+summary+body)。語は運用者が選ぶ前提。
  単語境界は見ない (MVP)。誤検知が出たら語をより限定的にする運用で対処する。
- yaml seed は持たない (空から始まる、純粋に運用者が育てる config)。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from src.cti.keyword_match import keyword_in_text
from src.logging_config import get_logger
from src.storage import config_store

_log = get_logger(__name__)

MATCH_LISTS_CONFIG_KEY = "match_lists"


class MatchList(BaseModel):
    """1 つの名前付きマッチリスト。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    name: str
    description: str = ""
    terms: tuple[str, ...] = ()

    @field_validator("terms", mode="before")
    @classmethod
    def _coerce_terms(cls, v: object) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(t).strip() for t in v if str(t).strip())
        return ()


_CACHE: list[MatchList] | None = None


def get_match_lists(*, force_reload: bool = False) -> list[MatchList]:
    """match_lists をプロセスキャッシュ越しに取得 (DB-first、未保存なら空)。"""
    global _CACHE
    if force_reload or _CACHE is None:
        _CACHE = _load_from_db()
    return _CACHE


def _load_from_db() -> list[MatchList]:
    try:
        raw = config_store.get_config(MATCH_LISTS_CONFIG_KEY)
    except Exception as e:  # noqa: BLE001 — DB 障害でも routing を壊さない
        _log.warning("match_lists_load_failed", error=str(e))
        return []
    if not isinstance(raw, list):
        return []
    out: list[MatchList] = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            try:
                out.append(MatchList(**item))
            except Exception:  # noqa: BLE001 — 壊れた entry は飛ばす
                continue
    return out


def invalidate_match_lists_cache() -> None:
    global _CACHE
    _CACHE = None


def save_match_lists(lists: list[dict[str, object]], *, note: str = "UI 編集") -> int:
    """検証済の match_lists を config_store に版保存。返り値 = 新 version。"""
    version = config_store.save_config(MATCH_LISTS_CONFIG_KEY, lists, note=note)
    invalidate_match_lists_cache()
    return version


def validate_match_lists(lists: list[dict[str, object]]) -> list[str]:
    """保存前検証。name 重複 / 空 name / 空 terms を弾く。"""
    errs: list[str] = []
    seen: set[str] = set()
    for i, item in enumerate(lists):
        if not isinstance(item, dict):
            errs.append(f"[{i}]: object である必要があります")
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            errs.append(f"[{i}]: name は必須")
            continue
        if not name.replace("_", "").isalnum():
            errs.append(f"[{i}] '{name}': name は英数字と _ のみ")
        if name in seen:
            errs.append(f"'{name}': name が重複")
        seen.add(name)
        terms = item.get("terms")
        if not isinstance(terms, list) or not [t for t in terms if str(t).strip()]:
            errs.append(f"'{name}': terms は 1 つ以上必要")
    return errs


def match_lists_for_text(text: str, *, lists: list[MatchList] | None = None) -> frozenset[str]:
    """text にいずれかの term が含まれる match_list の name 集合を返す。

    case-insensitive。M4 (有機的結合監査): 旧実装は境界なし substring で、PIR keyword が
    監査 P3 で直した誤爆 (`OT`→not/protocol 等) を運用者定義キーワードで再現していた —
    共有ヘルパ (keyword_match) で ASCII=語境界 / 日本語=substring に統一。
    """
    pool = lists if lists is not None else get_match_lists()
    if not pool:
        return frozenset()
    low = text.lower()
    return frozenset(ml.name for ml in pool if any(keyword_in_text(t, low) for t in ml.terms if t))
