"""taxonomy 提案の承認時適用 (2026-07-31 運用レビュー完全調査の本質修正)。

背景: taxonomy_review_proposals の「採用」は status 変更のみで、提案内容をどこにも
適用していなかった (UI は「ワンクリック承認」と適用を約束)。承認済み提案 (UAT-11795)
が辞書未登録のまま残った実害で確定。actor 提案 (actors/sync 承認) は yaml 適用 +
再帰属まで実装済みだったため、同じ終端保証を taxonomy 承認にも与える。

3 kind を適用する:
- add_alias     → config/cti/victim_sectors.yaml の canonical に alias 追記
- new_canonical → 同 yaml に新カテゴリ block 追記
- new_actor     → config/cti/actor_aliases.yaml へ辞書化 (actor_editor 再利用 + 一般語ゲート)

victim_sectors.yaml はコメント保持のためテキスト外科編集とし、適用後に round-trip
検証 (yaml parse → alias が canonical に解決) を通す fail-closed。書込は呼出側
(pages.py) が FileEditor (atomic write + .bak) を注入する — src/ui への逆依存を作らない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import yaml

from src.logging_config import get_logger
from src.storage.records import TaxonomyProposalRecord

_log = get_logger(__name__)

SECTORS_YAML_RELPATH = "config/cti/victim_sectors.yaml"
ACTORS_YAML_RELPATH = "config/cti/actor_aliases.yaml"

# 新カテゴリ id の形式 (yaml key / DB canonical 値になるため厳格に)
_CANONICAL_ID_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# 書込 callback: (repo 相対パス, 新しい全文) を受けて atomic write する
WriteYaml = Callable[[str, str], None]


class TaxonomyApplyError(ValueError):
    """提案を適用できない (衝突 / 対象不在 / 検証失敗)。承認は成立させない。"""


def _quote(value: str) -> str:
    """挿入する scalar は常に double-quote (yaml 特殊文字の考慮を一律で不要にする)。"""
    return json.dumps(value, ensure_ascii=False)


def _sector_lookup_of(text: str) -> dict[str, str]:
    """yaml 全文 → {alias_lower: canonical_id} (canonical id 自身も含む)。"""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("canonical"), dict):
        raise TaxonomyApplyError("victim_sectors.yaml の構造が不正です (canonical: が必要)")
    lookup: dict[str, str] = {}
    for cid, body in data["canonical"].items():
        if not isinstance(cid, str):
            continue
        lookup[cid.lower()] = cid
        if isinstance(body, dict):
            for alias in body.get("aliases") or []:
                if isinstance(alias, str) and alias.strip():
                    lookup[alias.strip().lower()] = cid
    return lookup


def _verify_sector_resolution(text: str, *, alias: str, canonical: str) -> None:
    """編集後の全文で alias が canonical に解決することを検証 (fail-closed の要)。"""
    lookup = _sector_lookup_of(text)
    resolved = lookup.get(alias.strip().lower())
    if resolved != canonical:
        raise TaxonomyApplyError(
            f"適用後検証に失敗: 「{alias}」が '{resolved}' に解決 (期待: '{canonical}')"
        )


def add_sector_alias_text(text: str, *, alias: str, canonical: str) -> str:
    """victim_sectors.yaml 全文に alias を外科的に追記した新全文を返す。

    既に同じ canonical へ解決するなら冪等 (原文のまま返す)。別 canonical に解決する
    場合は衝突エラー。挿入位置は対象 canonical の ``aliases:`` 直下 (list 内の順序は
    正規化に影響しない)。
    """
    alias = alias.strip()
    if not alias:
        raise TaxonomyApplyError("alias が空です")
    lookup = _sector_lookup_of(text)
    if canonical not in set(lookup.values()):
        raise TaxonomyApplyError(f"canonical '{canonical}' が victim_sectors.yaml にありません")
    current = lookup.get(alias.lower())
    if current == canonical:
        return text  # 冪等: 既に解決済み
    if current is not None:
        raise TaxonomyApplyError(
            f"「{alias}」は既に canonical '{current}' の別名です (提案先: '{canonical}')"
        )

    lines = text.splitlines()
    canon_re = re.compile(rf"^  {re.escape(canonical)}:\s*$")
    idx = next((i for i, ln in enumerate(lines) if canon_re.match(ln)), None)
    if idx is None:
        raise TaxonomyApplyError(f"canonical '{canonical}' の block が見つかりません")
    # canonical block 内 (次の 2-space key まで) の aliases: 行を探す
    inserted = False
    for j in range(idx + 1, len(lines)):
        ln = lines[j]
        if re.match(r"^  \S", ln):  # 次の canonical に到達
            break
        if re.match(r"^    aliases:\s*\[\]\s*$", ln):
            lines[j] = "    aliases:"
            lines.insert(j + 1, f"      - {_quote(alias)}")
            inserted = True
            break
        if re.match(r"^    aliases:\s*$", ln):
            lines.insert(j + 1, f"      - {_quote(alias)}")
            inserted = True
            break
    if not inserted:
        raise TaxonomyApplyError(f"canonical '{canonical}' に aliases: 行が見つかりません")
    new_text = "\n".join(lines) + "\n"
    _verify_sector_resolution(new_text, alias=alias, canonical=canonical)
    return new_text


def add_sector_canonical_text(
    text: str, *, canonical_id: str, display: str, aliases: list[str]
) -> str:
    """victim_sectors.yaml 全文に新カテゴリ block を追記した新全文を返す。"""
    canonical_id = canonical_id.strip()
    display = display.strip()
    if not _CANONICAL_ID_RE.match(canonical_id):
        raise TaxonomyApplyError(f"canonical id が不正です: '{canonical_id}' (小文字英数と _ のみ)")
    if not display:
        raise TaxonomyApplyError("display が空です")
    clean_aliases = [a.strip() for a in aliases if a.strip()]
    lookup = _sector_lookup_of(text)
    if canonical_id.lower() in lookup:
        raise TaxonomyApplyError(f"'{canonical_id}' は既に存在します")
    for a in clean_aliases:
        owner = lookup.get(a.lower())
        if owner is not None:
            raise TaxonomyApplyError(f"別名「{a}」は既に canonical '{owner}' に属しています")

    block_lines = [f"  {canonical_id}:", f"    display: {_quote(display)}"]
    if clean_aliases:
        block_lines.append("    aliases:")
        block_lines.extend(f"      - {_quote(a)}" for a in clean_aliases)
    else:
        block_lines.append("    aliases: []")
    new_text = text.rstrip("\n") + "\n\n" + "\n".join(block_lines) + "\n"
    for a in clean_aliases:
        _verify_sector_resolution(new_text, alias=a, canonical=canonical_id)
    if canonical_id.lower() not in _sector_lookup_of(new_text):
        raise TaxonomyApplyError(f"適用後検証に失敗: '{canonical_id}' が解決しません")
    return new_text


def _apply_new_actor(
    change: dict[str, Any],
    *,
    write_yaml: WriteYaml,
    actors_data_loader: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """pattern_5 (new_actor) を actor_aliases.yaml に辞書化する。

    actors/sync 承認 (corpus_emerging_actor) と同じ primitives (validate → append →
    render) を使う。一般語衝突は起票側ゲートと同思想で **適用時にも** ambiguous=true を
    強制する (承認経路が増えても最終防衛線が残る)。既知名なら冪等成功 (別キューで
    先に承認済みのケース — 二重適用エラーにしない)。
    """
    from src.cti.actor_editor import append_new_actor, render_actors_yaml, validate_actor_edit
    from src.cti.generic_alias_words import is_generic_alias

    canonical = str(change.get("canonical", "")).strip()
    if not canonical:
        raise TaxonomyApplyError("canonical (アクター名) が空です")
    sid = str(change.get("suggested_id", "")).strip() or re.sub(
        r"[^a-z0-9]+", "_", canonical.lower()
    ).strip("_")
    aliases = [
        str(a).strip()
        for a in change.get("aliases") or []
        if str(a).strip() and str(a).strip().lower() != canonical.lower()
    ]

    data = actors_data_loader()
    known = {
        str(n).lower()
        for a in data.get("actors", [])
        if isinstance(a, dict)
        for n in [a.get("id"), a.get("canonical"), *(a.get("aliases") or [])]
        if n
    }
    if canonical.lower() in known:
        return {"applied": "already_known", "actor_id": sid, "target_yaml": "actor_aliases"}

    actor: dict[str, Any] = {
        "id": sid,
        "canonical": canonical,
        "aliases": aliases,
        "kind": "group",
        "family": "",
    }
    if is_generic_alias(canonical) or is_generic_alias(sid):
        actor["ambiguous"] = True
    err = validate_actor_edit(
        data, sid, canonical, aliases, ambiguous=bool(actor.get("ambiguous", False))
    )
    if err:
        raise TaxonomyApplyError(f"衝突のため適用できません: {err}")
    try:
        new_data = append_new_actor(data, actor)
    except ValueError as e:
        raise TaxonomyApplyError(str(e)) from e
    write_yaml(ACTORS_YAML_RELPATH, render_actors_yaml(new_data))
    return {"applied": "yaml_updated", "actor_id": sid, "target_yaml": "actor_aliases"}


def apply_taxonomy_proposal(
    proposal: TaxonomyProposalRecord,
    *,
    write_yaml: WriteYaml,
    sectors_text_loader: Callable[[], str],
    actors_data_loader: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """承認された提案を対象 yaml に適用する。失敗時は TaxonomyApplyError (status は据置)。

    Returns:
        {"applied": "yaml_updated" | "already_known" | "noop",
         "target_yaml": ..., "actor_id"?: ...}
    """
    try:
        change = json.loads(proposal.proposed_change)
    except (TypeError, ValueError) as e:
        raise TaxonomyApplyError(f"提案データが壊れています: {e}") from e
    if not isinstance(change, dict):
        raise TaxonomyApplyError("提案データが壊れています (dict ではない)")
    kind = str(change.get("kind", ""))

    if kind == "add_alias":
        alias = str(change.get("alias", ""))
        canonical = str(change.get("canonical", ""))
        text = sectors_text_loader()
        new_text = add_sector_alias_text(text, alias=alias, canonical=canonical)
        if new_text == text:
            return {"applied": "already_known", "target_yaml": "victim_sectors"}
        write_yaml(SECTORS_YAML_RELPATH, new_text)
        return {"applied": "yaml_updated", "target_yaml": "victim_sectors"}

    if kind == "new_canonical":
        sid = str(change.get("suggested_id", ""))
        display = str(change.get("display", "") or sid)
        aliases = [str(a) for a in change.get("aliases") or []]
        text = sectors_text_loader()
        new_text = add_sector_canonical_text(
            text, canonical_id=sid, display=display, aliases=aliases
        )
        write_yaml(SECTORS_YAML_RELPATH, new_text)
        return {"applied": "yaml_updated", "target_yaml": "victim_sectors"}

    if kind == "new_actor":
        return _apply_new_actor(
            change, write_yaml=write_yaml, actors_data_loader=actors_data_loader
        )

    raise TaxonomyApplyError(f"未知の変更種別のため適用できません: '{kind}'")
