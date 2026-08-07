"""actor_aliases.yaml の構造化編集 (Actor 辞書 UI 用)。

raw YAML 直編集は alias のタイポ等が silent な誤帰属を生む。本モジュールは raw yaml を
dict として読み、対象 actor のみ書き換えて render する (families セクション・全 field を保持)。
**alias の重複検証** (別 actor と同名 → どちらに帰属するか曖昧) を保存時に行うのが核心。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.cti.actor_normalizer import DEFAULT_ALIASES_PATH

# UI から編集できる field (id は不変キー)。
# F6 (2026-07-26): origin は分析消費者ゼロの死にフィールドのため編集面から撤去
# (yaml の既存値は温存 — apply_actor_edit は未知キーを保持する)。代わりに照合挙動を
# 決める ambiguous / context_cues を編集可能化 (一般語 alias 汚染の再発防止は
# validate_actor_edit の generic ゲート検証が担う)。
EDITABLE_FIELDS: tuple[str, ...] = (
    "canonical",
    "aliases",
    "mitre_group",
    "nation",
    "sponsor",
    "family",
    "kind",
    "sponsor_org",
    "description",
    # source slug 名前空間キー (R2): 構造化ソースの group slug (prose 照合とは別レイヤ)
    "source_slugs",
    # 照合ゲート (Part B: 一般語と衝突する名前は文脈 cue 共起で初めてマッチ)
    "ambiguous",
    "context_cues",
    # reference 用詳細 (Stage 1)
    "summary",
    "motivation",
    "first_seen",
    "target_sectors",
    "target_regions",
    "associated_malware",
    "notable_campaigns",
    "references",
)

# list 型 field (yaml list / UI では行区切り)。それ以外は scalar。
LIST_FIELDS: frozenset[str] = frozenset(
    {
        "aliases",
        "source_slugs",
        "context_cues",
        "target_sectors",
        "target_regions",
        "associated_malware",
        "notable_campaigns",
        "references",
    }
)

# bool 型 field (yaml bool / UI では checkbox)。
BOOL_FIELDS: frozenset[str] = frozenset({"ambiguous"})

# UI 保存時に保持する doc header (code-owned → 経緯コメントを失わない)。
_ACTOR_HEADER = """\
# 国家支援型 APT / 主要犯罪集団のエイリアス辞書 (Phase 4)。
# **このファイルは Web UI (Actor 辞書) の構造化フォームから編集する。**
# actors[].aliases は一致判定に使う別名 (大文字小文字非区別)。**alias の重複は誤帰属を生む**ため
# UI 保存時に検証する。families は actor 群の分類 (Threat Operations の family filter 用)。
"""


def load_actors_raw(path: Path = DEFAULT_ALIASES_PATH) -> dict[str, Any]:
    """actor_aliases.yaml を dict として読む (families + actors を保持)。"""
    if not path.exists():
        return {"families": {}, "actors": []}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"families": {}, "actors": []}
    data.setdefault("families", {})
    if not isinstance(data.get("actors"), list):
        data["actors"] = []
    return data


def list_actors(path: Path = DEFAULT_ALIASES_PATH) -> list[dict[str, Any]]:
    """UI 一覧用に actor の編集可能 field を返す。"""
    out: list[dict[str, Any]] = []
    for a in load_actors_raw(path)["actors"]:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        row: dict[str, Any] = {"id": str(a["id"])}
        for k in EDITABLE_FIELDS:
            v = a.get(k)
            if k in LIST_FIELDS:
                row[k] = [str(x) for x in v] if isinstance(v, list) else []
            elif k in BOOL_FIELDS:
                row[k] = bool(v)
            else:
                row[k] = v
        # 既知 TTP (MITRE 同期所有の read-only field、UI では閲覧のみ)
        ttps = a.get("mitre_ttps")
        row["mitre_ttps"] = [str(x) for x in ttps] if isinstance(ttps, list) else []
        out.append(row)
    return out


def list_families(path: Path = DEFAULT_ALIASES_PATH) -> list[str]:
    """family id 一覧 (UI の family dropdown 用)。"""
    fams = load_actors_raw(path).get("families")
    return sorted(str(k) for k in fams) if isinstance(fams, dict) else []


def _names_of(actor: dict[str, Any]) -> list[str]:
    names = [str(actor.get("canonical") or "")]
    aliases = actor.get("aliases")
    if isinstance(aliases, list):
        names.extend(str(x) for x in aliases)
    return [n for n in names if n.strip()]


def validate_actor_edit(
    data: dict[str, Any],
    actor_id: str,
    canonical: str,
    aliases: list[str],
    *,
    ambiguous: bool | None = None,
) -> str | None:
    """編集内容を検証。エラー文字列 or None。**alias 重複 (誤帰属) を防ぐのが主目的**。

    ambiguous: 編集後のゲート状態。None なら data 内の現在値に fallback (alias だけを
    足す承認経路用)。一般語の名前は ambiguous=true (文脈 cue ゲート) が必須 —
    guard test (test_actor_normalizer) と同じ SSoT (generic_alias_words) を保存時にも
    強制し、MITRE 同期が除去済み alias を書き戻した 2026-07-21 型の再発を UI 側でも塞ぐ。
    """
    from src.cti.generic_alias_words import is_generic_alias

    if not canonical.strip():
        return "主名は必須です"
    if ambiguous is None:
        cur = next(
            (a for a in data["actors"] if isinstance(a, dict) and str(a.get("id")) == actor_id),
            None,
        )
        ambiguous = bool((cur or {}).get("ambiguous"))
    if not ambiguous:
        for name in (canonical, *aliases):
            if is_generic_alias(name):
                return (
                    f"名前『{name}』は一般語と衝突します — 文脈ゲート (ambiguous) を"
                    "有効にするか、この名前を除去してください (誤帰属の原因になります)"
                )
    # 自身の new names (canonical + aliases、小文字化)
    my_names = {canonical.strip().lower()}
    for a in aliases:
        s = a.strip().lower()
        if s:
            my_names.add(s)
    # 他 actor の name と衝突しないか (= 誤帰属の原因)。merged 墓標は照合に参加しない
    # (canonical は歴史表示用に残るが継承先の alias と同名で正) ため検証対象外。
    for other in data["actors"]:
        if not isinstance(other, dict) or str(other.get("id")) == actor_id:
            continue
        if str(other.get("status") or "") == "merged":
            continue
        for name in _names_of(other):
            if name.lower() in my_names:
                return (
                    f"名前『{name}』が actor『{other.get('canonical')}』"
                    f"(ID: {other.get('id')}) と重複しています — 誤帰属の原因になります"
                )
    # 正規化キー衝突 (2026-08-01 thegentlemen 事故の再発防止): id/canonical を
    # _norm_slug (記号除去 + casefold) で畳んだキーが他 active actor の id/canonical と
    # 一致するのは同一実体の綴り違い (The Gentlemen vs thegentlemen)。読取層
    # (resolve_source_slug) は正規化解決するのに書込層が生文字列比較だったのが真因。
    # alias の共有 (APT38 = lazarus/bluenoroff 両属) は意図的に存在するため、
    # 検査は id/canonical 由来のキー同士に限定する。
    from src.cti.actor_normalizer import _norm_slug

    my_keys = {_norm_slug(actor_id), _norm_slug(canonical)} - {""}
    for other in data["actors"]:
        if not isinstance(other, dict) or str(other.get("id")) == actor_id:
            continue
        if str(other.get("status") or "") == "merged":
            continue
        other_keys = {
            _norm_slug(str(other.get("id") or "")),
            _norm_slug(str(other.get("canonical") or "")),
        } - {""}
        hit = my_keys & other_keys
        if hit:
            return (
                f"ID/主名の正規化キー『{sorted(hit)[0]}』が actor"
                f"『{other.get('canonical')}』(ID: {other.get('id')}) と衝突します — "
                "同一実体の綴り違いの疑い (登録せず merge を検討してください)"
            )
    return None


def apply_actor_edit(data: dict[str, Any], actor_id: str, edit: dict[str, Any]) -> dict[str, Any]:
    """data 内の対象 actor に edit を適用した新 data を返す (該当なしは KeyError)。"""
    actors = data["actors"]
    idx = next(
        (i for i, a in enumerate(actors) if isinstance(a, dict) and str(a.get("id")) == actor_id),
        None,
    )
    if idx is None:
        raise KeyError(f"actor not found: {actor_id}")
    actor = dict(actors[idx])
    for k in EDITABLE_FIELDS:
        if k not in edit:
            continue
        v = edit[k]
        if k in LIST_FIELDS:
            actor[k] = [str(x).strip() for x in (v or []) if str(x).strip()]
        elif k in BOOL_FIELDS:
            actor[k] = bool(v)
        elif k == "canonical":
            actor[k] = str(v).strip()
        elif k in ("description", "summary"):
            actor[k] = str(v).strip() if v is not None else ""  # 空文字許容 (default "")
        else:
            sv = str(v).strip() if v is not None else ""
            actor[k] = sv or None  # 空は None (yaml で未指定相当)
    new_actors = [*actors]
    new_actors[idx] = actor
    return {**data, "actors": new_actors}


def append_new_actor(data: dict[str, Any], actor: dict[str, Any]) -> dict[str, Any]:
    """新規 actor を追加した新 data を返す (MITRE 同期提案の承認用)。

    呼び出し側で ``validate_actor_edit`` による alias 衝突検証を済ませること。
    id 重複は ValueError。
    """
    actor_id = str(actor.get("id") or "").strip()
    if not actor_id:
        raise ValueError("actor id は必須です")
    from src.cti.actor_normalizer import _norm_slug

    nid = _norm_slug(actor_id)
    for a in data["actors"]:
        if not isinstance(a, dict):
            continue
        existing_id = str(a.get("id"))
        if existing_id == actor_id:
            raise ValueError(f"actor id が重複しています: {actor_id}")
        # 正規化キー一致は綴り違いの同一実体 (thegentlemen 事故の最終防衛線)。
        # merged 墓標は継承先 (active) 側が同キーを持つため active のみ照合する。
        if str(a.get("status") or "") != "merged" and _norm_slug(existing_id) == nid:
            raise ValueError(
                f"actor id が正規化キーで重複しています: {actor_id} (既存: {existing_id})"
            )
    return {**data, "actors": [*data["actors"], dict(actor)]}


def move_alias(data: dict[str, Any], alias: str, from_id: str, to_id: str) -> dict[str, Any]:
    """alias を from_id の actor から to_id の actor に付け替えた新 data を返す。

    MITRE 同期の alias 衝突提案を承認したときに使う。canonical (主名) は
    付け替え不可 (ValueError) — 主名の整理は手動編集で行う。
    """
    low = alias.strip().lower()
    if not low:
        raise ValueError("alias が空です")
    by_id = {str(a.get("id")): a for a in data["actors"] if isinstance(a, dict) and a.get("id")}
    src = by_id.get(from_id)
    dst = by_id.get(to_id)
    if src is None or dst is None:
        raise KeyError(f"actor not found: {from_id if src is None else to_id}")
    if str(src.get("canonical", "")).strip().lower() == low:
        raise ValueError(
            f"『{alias}』は actor『{src.get('canonical')}』の canonical (主名) のため"
            "自動付け替えできません — 手動で整理してください"
        )
    src_aliases = [x for x in (src.get("aliases") or []) if str(x).strip().lower() != low]
    if len(src_aliases) == len(src.get("aliases") or []):
        raise ValueError(f"『{alias}』は actor『{src.get('canonical')}』の alias にありません")
    dst_aliases = list(dst.get("aliases") or [])
    if low not in {str(x).strip().lower() for x in dst_aliases}:
        dst_aliases.append(alias)

    new_actors: list[Any] = []
    for a in data["actors"]:
        if not isinstance(a, dict):
            new_actors.append(a)
        elif str(a.get("id")) == from_id:
            new_actors.append({**a, "aliases": src_aliases})
        elif str(a.get("id")) == to_id:
            new_actors.append({**a, "aliases": dst_aliases})
        else:
            new_actors.append(a)
    return {**data, "actors": new_actors}


def render_actors_yaml(data: dict[str, Any]) -> str:
    """data を doc header 付き yaml に整形 (UI 保存用)。families + 全 field を保持。"""
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return f"{_ACTOR_HEADER}\n{body}"
