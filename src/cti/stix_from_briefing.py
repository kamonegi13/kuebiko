"""``BriefingMessage`` から STIX 2.1 Bundle (bytes) を作る (Phase 4.5)。

Discord 投稿に STIX bundle を **添付** するための薄いラッパ。
``src/main.py`` の publish ループから呼ばれ、IOC / actor / technique が
1 件以上ある briefing にのみ bundle を作って返す (空 briefing は None)。

設計:
- ``BriefingMessage`` は ``iocs`` / ``mitre_techniques`` を **flat な文字列**
  で保持しているため、ここで再分類 (refang → 種別判定) して
  ``ExtractedIocs`` を再構築する。Inoreader 経路で既に extractor を通って
  いるが、Grok 経路では生 string しかないため統一的に再構築するのが楽。
- 既知アクターは ``BriefingMessage.metadata["detected_actor_ids"]`` (新規)
  に list[str] として保存する。``ActorAliasRegistry.by_id()`` で
  ``ActorAlias`` を引き戻す。

ファイル名は呼び出し側で組む (run_id / article_id / 重要度を入れたいため)。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from src.cti.actor_normalizer import ActorAlias, ActorAliasRegistry
from src.cti.ioc_extractor import ExtractedIocs, extract_iocs
from src.cti.stix_exporter import to_bundle
from src.logging_config import get_logger

if TYPE_CHECKING:
    from src.tools.discord_publisher import BriefingMessage

_log = get_logger(__name__)

# Phase 5P: Discord 添付の安全マージン込みサイズ上限。
# Discord は 8MB / message が上限だが、multipart 境界・filename・JSON 文字列
# エスケープでサイズが膨らむため 6MB をハード上限とする。これを超える
# bundle は添付スキップし本文投稿だけ通す (graceful degradation)。
STIX_MAX_BYTES = 6 * 1024 * 1024


def briefing_to_stix_bytes(
    msg: BriefingMessage,
    *,
    registry: ActorAliasRegistry | None = None,
    description: str | None = None,
    policy: object | None = None,  # StixAttachPolicy (循環 import 回避で untyped)
) -> bytes | None:
    """``BriefingMessage`` から STIX 2.1 Bundle JSON を bytes で返す。

    Phase 5F: ``policy`` で添付条件を制御:
      - ``off``: 常に None
      - ``lenient``: 旧挙動 (IOC/actor/technique のいずれかがあれば添付)
      - ``strict`` (既定): 具体的 IOC ≥ 1 OR (actor ≥ 1 AND technique ≥ 1)。
        ``msg.metadata['grok_section_empty']=True`` の briefing は無条件 skip。
    """
    # 循環 import 回避のため遅延 import
    from src.config_loader import StixAttachPolicy

    p: StixAttachPolicy = policy if isinstance(policy, StixAttachPolicy) else StixAttachPolicy()
    if p.mode == "off":
        return None
    # 「該当なし」briefing は strict モードで無条件 skip (lenient では従来通り判定)
    if p.mode == "strict" and msg.metadata.get("grok_section_empty"):
        return None
    # Phase B-cal: 非サイバーカテゴリは cyber observable を持たないため STIX 添付対象外。
    # (strict の content 判定でも実質 None になるが、明示的に早期 skip して無駄な再分類を避ける)
    if (msg.category or "").lower() in {"geopolitical", "research"}:
        return None

    iocs, techs = _collect_flat_iocs_and_techs(msg)
    extracted = _reclassify(iocs, techs)
    actors = _resolve_actors(msg, registry)

    concrete_iocs = (
        len(extracted.cves)
        + len(extracted.ipv4)
        + len(extracted.ipv6)
        + len(extracted.domains)
        + len(extracted.urls)
        + len(extracted.md5)
        + len(extracted.sha1)
        + len(extracted.sha256)
    )
    techniques_count = len(extracted.mitre_techniques) + len(extracted.mitre_groups)

    if p.mode == "lenient":
        # 旧挙動: 何かしら 1 件あれば添付
        if concrete_iocs == 0 and techniques_count == 0 and not actors:
            return None
    else:
        # strict: 具体的 IOC が閾値以上 OR (actor かつ technique 両方)
        has_concrete = concrete_iocs >= p.min_concrete_iocs
        has_actor_and_technique = (
            p.require_actor_and_technique and bool(actors) and techniques_count >= 1
        )
        if not has_concrete and not has_actor_and_technique:
            return None

    desc = description or _default_description(msg)
    # Phase Diamond-Axes: socio-political intent を STIX primary_motivation へ伝播。
    intent_raw = msg.metadata.get("socio_political_intent")
    intent = intent_raw if isinstance(intent_raw, str) else None
    bundle = to_bundle(extracted, actors, description=desc, socio_political_intent=intent)
    # ensure_ascii=False で日本語を可読に保持。コンパクト出力で添付サイズを抑える
    payload = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # Phase 5P: Discord 上限 (8MB) を超える前にハード上限 6MB で打ち切る。
    # 超過時は添付スキップ (本文投稿は止めない graceful degradation)。
    if len(payload) > STIX_MAX_BYTES:
        _log.warning(
            "stix_bundle_too_large",
            size=len(payload),
            limit=STIX_MAX_BYTES,
            iocs_total=concrete_iocs,
            actors=len(actors),
            techniques=techniques_count,
        )
        return None
    return payload


def _collect_flat_iocs_and_techs(
    msg: BriefingMessage,
) -> tuple[list[str], list[str]]:
    """``BriefingMessage`` から (iocs, techniques) を集める。multi-embed の
    ``incidents[*].iocs`` / ``incidents[*].ttps`` も合算する。
    """
    iocs: list[str] = list(msg.iocs)
    techs: list[str] = list(msg.mitre_techniques)
    for inc in msg.incidents:
        iocs.extend(inc.iocs)
        techs.extend(inc.ttps)
    return _dedup_preserve_order(iocs), _dedup_preserve_order(techs)


def _dedup_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in items:
        key = s.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _reclassify(iocs: list[str], techs: list[str]) -> ExtractedIocs:
    """flat string list を ``ExtractedIocs`` に再分類する。

    各 string を ``extract_iocs`` に通して種別を判定し、結果を集約する。
    techniques は ``MITRE T1234`` 形式なので extract_iocs 経由で判定する。
    """
    # 全 IOC + techniques を 1 つのコーパスに連結して extract_iocs に渡す。
    # extract_iocs は refang + 全種別判定を 1 パスでやってくれる。
    corpus = "\n".join([*iocs, *techs])
    if not corpus.strip():
        return ExtractedIocs()
    return extract_iocs(corpus)


def _resolve_actors(
    msg: BriefingMessage,
    registry: ActorAliasRegistry | None,
) -> list[ActorAlias]:
    """``metadata["detected_actor_ids"]`` を ``ActorAlias`` のリストに解決。

    保存された ID が無い場合は ``msg`` のテキスト要素 (title + bluf + summary
    + incidents[*].body) を走査してフォールバック検出する (Grok 経路など、
    ``_process_article`` を通っていない briefing でも actor を拾うため)。
    レジストリ未指定の場合は空リスト。
    """
    if registry is None:
        return []
    # subject-gate (2026-07-29): STIX は外部連携 (TIP 等) に流れるため誤帰属流出が高コスト。
    # post 時点では subject_actor_ids が未計算のため、judgment の主題 actor
    # (routing_flags["primary_actor_id"]、mention 集合内に gate 済み) を主題 proxy に使い、
    # 特定できれば threat-actor をそれのみに絞る。primary 不明 (legacy/未評価/主題なし) の
    # ときだけ従来の mention/本文走査 fallback に委ねる。
    rf = msg.metadata.get("routing_flags")
    if isinstance(rf, dict):
        primary = str(rf.get("primary_actor_id") or "").strip()
        if primary:
            actor = registry.by_id(primary)
            return [actor] if actor is not None else []
    raw = msg.metadata.get("detected_actor_ids")
    if isinstance(raw, list) and raw:
        out: list[ActorAlias] = []
        for actor_id in raw:
            if not isinstance(actor_id, str):
                continue
            actor = registry.by_id(actor_id)
            if actor is not None:
                out.append(actor)
        if out:
            return out

    # フォールバック: BriefingMessage 内のテキストを scan
    parts: list[str] = [msg.title, msg.bluf, msg.summary]
    for inc in msg.incidents:
        parts.append(inc.heading)
        parts.append(inc.body)
        if inc.related_actor:
            parts.append(inc.related_actor)
    return registry.find_all("\n".join(p for p in parts if p))


def _default_description(msg: BriefingMessage) -> str:
    """Bundle の note に入れる説明文 (元記事 URL を含む)。"""
    src_url = ""
    if msg.sources:
        src_url = msg.sources[0].url
    title = msg.title or "(no title)"
    if src_url:
        return f"{title} ({src_url})"
    return title


def make_attachment_filename(
    *,
    importance: str,
    article_id: str,
) -> str:
    """添付ファイル名を組む。

    例: ``stix-high-a3b1c2d4.json``
    article_id は短いハッシュにして filesystem-friendly にする。
    """
    import hashlib

    digest = hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:8]
    return f"stix-{importance}-{digest}.json"
