"""MITRE ATT&CK → Actor 辞書の逐次同期 (Actors Stage 4)。

weekly cron (pipelines.yaml の ``mitre-actor-sync``) で MITRE ATT&CK enterprise STIX を
取得し、``config/cti/actor_aliases.yaml`` との差分を検出する。

適用ポリシー (ハイブリッド):
- **自動適用**: MITRE 一次データ由来の追加系差分 — alias 追加 / 使用マルウェア追加 /
  references 追加 / summary 更新 (ローカル LLM で和訳)。atomic write + ``.bak`` で追跡可能。
  ただし一般語 alias (旧 Microsoft 元素名等) は取り込まない — 詳細は
  ``src/cti/generic_alias_words.py``。
- **レビュー提案** (``actor_update_proposals`` テーブル): 判断が必要なもののみ —
  新規 actor 追加 (nation がヒューリスティック推定のため誤帰属リスク) と
  alias 衝突 (MITRE の別名が既存の別 actor に帰属済)。

状態管理: DB でなく yaml 内の bookkeeping field が SSoT。
- ``mitre_summary_sha1``: 最後に取り込んだ MITRE 英語 summary の sha1。
  保存 summary は和訳済で原文比較できないため、MITRE 側変更検知に使う。
- alias / malware / references は集合差分で冪等に検出するため状態不要。

翻訳ルール: 固有名詞 (アクター名・マルウェア/ツール名・組織名・作戦名) と
MITRE ID / CVE は原文のまま維持する (prompt で強制)。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from src.cti.actor_editor import load_actors_raw, render_actors_yaml
from src.cti.generic_alias_words import is_generic_alias
from src.logging_config import get_logger
from src.tools.llm_client import LLMClient, LLMError

_log = get_logger(__name__)

MITRE_ENTERPRISE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    "enterprise-attack/enterprise-attack.json"
)
_FETCH_TIMEOUT_SECONDS = 120.0
_MAX_REFERENCES = 8
_TRANSLATE_MAX_TOKENS = 2000

# 攻撃主体名 → ISO alpha-2 (unambiguous な attribution marker のみ)。
# 国名のみの弱い一致 (bare "chinese"/"russian") は誤帰属を生む
# (Gorgon→ru / Machete→ru / Sidewinder→cn 等の実害を確認) ため使わない。
AGENCY_NATION: tuple[tuple[str, str], ...] = (
    ("ministry of state security", "cn"),
    ("mss", "cn"),
    ("people's liberation army", "cn"),
    ("pla unit", "cn"),
    ("chinese state", "cn"),
    ("china-based", "cn"),
    ("china-nexus", "cn"),
    ("reconnaissance general bureau", "kp"),
    ("north korea", "kp"),
    ("dprk", "kp"),
    ("north korean", "kp"),
    ("gru", "ru"),
    ("svr", "ru"),
    ("fsb", "ru"),
    ("russian military", "ru"),
    ("russian state", "ru"),
    ("russia-based", "ru"),
    ("russian government", "ru"),
    ("mois", "ir"),
    ("irgc", "ir"),
    ("islamic revolutionary guard", "ir"),
    ("iranian government", "ir"),
    ("iran-based", "ir"),
    ("iranian state", "ir"),
)

# well-known mission group の nation override (MITRE ID → ISO alpha-2)。CTI 帰属で検証済。
NATION_OVERRIDE: dict[str, str] = {
    # 中国
    "G1054": "cn",
    "G0093": "cn",
    "G0044": "cn",
    "G0019": "cn",
    "G0027": "cn",
    "G0131": "cn",
    "G0013": "cn",
    "G0009": "cn",
    "G0005": "cn",
    "G0022": "cn",
    "G0024": "cn",
    "G0025": "cn",
    "G0001": "cn",
    "G1042": "cn",
    # 北朝鮮
    "G0067": "kp",
    "G1036": "kp",
    "G1052": "kp",
    # イラン
    "G0064": "ir",
    "G1030": "ir",
    "G0052": "ir",
    "G0003": "ir",
    "G0137": "ir",
    "G0122": "ir",
    "G1012": "ir",
    "G1055": "ir",
    # ロシア
    "G0088": "ru",
}

# 主要なランサム/e-crime 群のみ追加対象 (broad keyword だと minor 群まで拾い辞書が薄まる)。
CRIMINAL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "G0008",  # Carbanak
        "G0115",  # GOLD SOUTHFIELD (REvil/Sodinokibi)
        "G1004",  # LAPSUS$
        "G1032",  # INC Ransom
        "G1043",  # BlackByte
        "G0061",  # FIN8
        "G0046",  # FIN7
        "G0037",  # FIN6
        "G0080",  # Cobalt Group
        "G1053",  # Storm-0501
        "G1046",  # Storm-1811
    }
)

_CITATION_RE = re.compile(r"\(Citation:.*?\)", re.DOTALL)
_MDLINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


# ---------- MITRE STIX 取得・パース ----------


@dataclass(frozen=True)
class MitreGroup:
    """MITRE ATT&CK intrusion-set 1 件の factual data。"""

    mitre_id: str  # G番号
    canonical: str
    aliases: tuple[str, ...]
    summary: str
    associated_malware: tuple[str, ...]
    references: tuple[str, ...]
    modified: str  # STIX modified timestamp (情報用)
    nation: str | None  # 検証済 override / agency marker で確定できた場合のみ
    nation_reason: str  # 推定根拠 (レビュー提案の rationale 用)
    is_criminal: bool
    # 既知 TTP ("T1566 Phishing" 形式、uses→attack-pattern 由来)。観測 TTP との
    # 突き合わせ (脅威アクターページの既知外ハイライト) に使う knowledge 側データ。
    ttps: tuple[str, ...] = ()


def clean_mitre_text(text: str) -> str:
    """MITRE description から citation marker / markdown link を除去。"""
    text = _CITATION_RE.sub("", text)
    text = _MDLINK_RE.sub(r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def derive_nation(mitre_id: str, desc: str) -> tuple[str | None, str]:
    """nation と推定根拠を返す: 検証済 override 優先 → agency marker。

    国名のみの弱い一致は使わない (誤帰属防止)。確定できなければ (None, "")。
    """
    if mitre_id in NATION_OVERRIDE:
        return NATION_OVERRIDE[mitre_id], f"検証済 override ({mitre_id})"
    low = desc.lower()
    for marker, nation in AGENCY_NATION:
        if marker in low:
            return nation, f"description 中の attribution marker『{marker}』"
    return None, ""


def slugify_actor_id(name: str) -> str:
    """canonical 名 → actor id スラグ。"""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "actor"


def parse_mitre_bundle(objects: list[dict[str, Any]]) -> list[MitreGroup]:
    """STIX bundle の objects から group ごとの factual data を組み立てる (pure)。"""
    id_to_name: dict[str, str] = {}
    technique_label: dict[str, str] = {}  # attack-pattern stix id → "T1566 Phishing"
    intrusion_sets: dict[str, dict[str, Any]] = {}
    uses: dict[str, set[str]] = {}
    uses_technique: dict[str, set[str]] = {}
    for o in objects:
        t = o.get("type")
        if t in ("malware", "tool"):
            id_to_name[o["id"]] = o.get("name", "")
        elif t == "attack-pattern" and not o.get("revoked") and not o.get("x_mitre_deprecated"):
            tid = next(
                (
                    r.get("external_id")
                    for r in o.get("external_references", [])
                    if r.get("source_name") == "mitre-attack"
                ),
                None,
            )
            if tid:
                technique_label[o["id"]] = f"{tid} {o.get('name', '')}".strip()
        elif t == "intrusion-set" and not o.get("revoked") and not o.get("x_mitre_deprecated"):
            intrusion_sets[o["id"]] = o
    for o in objects:
        if o.get("type") == "relationship" and o.get("relationship_type") == "uses":
            src = o.get("source_ref", "")
            tgt = o.get("target_ref", "")
            tgt_type = tgt.split("--")[0]
            if src not in intrusion_sets:
                continue
            if tgt_type in ("malware", "tool"):
                uses.setdefault(src, set()).add(tgt)
            elif tgt_type == "attack-pattern" and tgt in technique_label:
                uses_technique.setdefault(src, set()).add(tgt)

    out: list[MitreGroup] = []
    for sid, o in intrusion_sets.items():
        ext = o.get("external_references", [])
        mitre_id = next(
            (r.get("external_id") for r in ext if r.get("source_name") == "mitre-attack"), None
        )
        if not mitre_id:
            continue
        name = str(o.get("name", ""))
        aliases = tuple(a for a in o.get("aliases", []) if a and a != name)
        malware = tuple(sorted({id_to_name.get(m, "") for m in uses.get(sid, set())} - {""}))
        refs = tuple(
            sorted(
                {r["url"] for r in ext if r.get("source_name") != "mitre-attack" and r.get("url")}
            )[:_MAX_REFERENCES]
        )
        desc = clean_mitre_text(str(o.get("description", "")))
        nation, reason = derive_nation(str(mitre_id), desc)
        ttps = tuple(sorted(technique_label[t] for t in uses_technique.get(sid, set())))
        out.append(
            MitreGroup(
                mitre_id=str(mitre_id),
                canonical=name,
                aliases=aliases,
                summary=desc,
                associated_malware=malware,
                references=refs,
                modified=str(o.get("modified", "")),
                nation=nation,
                nation_reason=reason,
                is_criminal=str(mitre_id) in CRIMINAL_ALLOWLIST,
                ttps=ttps,
            )
        )
    return out


async def fetch_mitre_groups(url: str = MITRE_ENTERPRISE_URL) -> list[MitreGroup]:
    """MITRE ATT&CK enterprise STIX を取得してパースする。"""
    _log.info("mitre_sync_fetch_start", url=url)
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    groups = parse_mitre_bundle(resp.json().get("objects", []))
    _log.info("mitre_sync_fetch_done", groups=len(groups))
    return groups


# ---------- 差分計算 (pure) ----------


@dataclass(frozen=True)
class ActorAutoUpdate:
    """既存 actor への自動適用差分 (追加系のみ、curated field は触らない)。"""

    actor_id: str
    add_aliases: tuple[str, ...] = ()
    add_malware: tuple[str, ...] = ()
    add_references: tuple[str, ...] = ()
    add_ttps: tuple[str, ...] = ()  # 既知 TTP の追加分 ("T1566 Phishing" 形式)
    set_mitre_group: str | None = None
    new_summary_en: str | None = None  # MITRE 側で summary 変更があった場合のみ (要和訳)
    summary_sha1: str | None = None  # new_summary_en の sha1 (bookkeeping 用)
    # 一般語のため取り込まなかった MITRE alias (可視化専用、辞書には反映しない)。
    skipped_generic_aliases: tuple[str, ...] = ()

    @property
    def has_changes(self) -> bool:
        return bool(
            self.add_aliases
            or self.add_malware
            or self.add_references
            or self.add_ttps
            or self.set_mitre_group
            or self.new_summary_en
        )


@dataclass(frozen=True)
class SyncProposal:
    """レビューが必要な差分 (新規 actor / alias 衝突)。"""

    proposal_type: str  # "mitre_new_actor" | "mitre_alias_conflict"
    mitre_group: str
    dedup_key: str  # 再提案防止キー (rejected も含めて同一提案を抑止)
    actor_id: str | None
    payload: dict[str, Any]
    rationale: str


@dataclass(frozen=True)
class SyncPlan:
    """1 回の同期で検出した差分の全体。"""

    auto_updates: list[ActorAutoUpdate] = field(default_factory=list)
    proposals: list[SyncProposal] = field(default_factory=list)
    matched: int = 0
    skipped_out_of_mission: int = 0


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _actor_names_lower(actor: dict[str, Any]) -> set[str]:
    names = {str(actor.get("canonical", "")).lower()}
    for a in actor.get("aliases") or []:
        names.add(str(a).lower())
    names.discard("")
    return names


def _build_name_owner_index(actors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """name (lower) → actor の逆引き (alias 衝突検出用)。"""
    idx: dict[str, dict[str, Any]] = {}
    for a in actors:
        if not isinstance(a, dict):
            continue
        for name in _actor_names_lower(a):
            idx.setdefault(name, a)
    return idx


def _match_actor(
    group: MitreGroup,
    by_mitre: dict[str, dict[str, Any]],
    by_name: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    # MITRE ID 一致は最も信頼できる (正しい紐付け)。
    hit = by_mitre.get(group.mitre_id.upper())
    if hit is not None:
        return hit
    # name/alias フォールバック: 既存 actor が別 MITRE group の名前を alias 混載していると
    # 誤マッチする (silk_typhoon(G0125) が ZIRCONIUM を、ember_bear(G1003) が Saint Bear の
    # designation を抱える等)。別グループの summary/属性で上書きすると取り違えるため、
    # 既に別 MITRE group に紐付く actor へのフォールバックマッチは拒否し、未収載グループとして
    # 上位で new_actor 提案へ回す。mitre_group 未設定の actor は初回紐付けとして許可する。
    cand = by_name.get(group.canonical.lower()) or next(
        (by_name[a.lower()] for a in group.aliases if a.lower() in by_name), None
    )
    if cand is None:
        return None
    cand_mitre = str(cand.get("mitre_group") or "").upper()
    if cand_mitre and cand_mitre != group.mitre_id.upper():
        return None
    return cand


def _diff_existing(
    actor: dict[str, Any],
    group: MitreGroup,
    name_owner: dict[str, dict[str, Any]],
) -> tuple[ActorAutoUpdate, list[SyncProposal]]:
    """既存 actor と MITRE group の差分 → 自動適用分と衝突提案に振り分ける。"""
    actor_id = str(actor["id"])
    my_names = _actor_names_lower(actor) | {group.canonical.lower()}

    # ambiguous=true の actor は文脈 cue ゲートがあるため一般語 alias を持ってよい。
    is_gated = bool(actor.get("ambiguous"))

    add_aliases: list[str] = []
    skipped_generic: list[str] = []
    proposals: list[SyncProposal] = []
    for alias in group.aliases:
        low = alias.lower()
        if low in my_names:
            continue
        # 一般語 (旧 Microsoft 元素名等) は単独で識別できず誤帰属を生むため取り込まない。
        # MITRE 側は保持し続けるので、filter しないと毎週の同期が書き戻す (2026-07-21)。
        if not is_gated and is_generic_alias(alias):
            skipped_generic.append(alias)
            continue
        owner = name_owner.get(low)
        if owner is not None and str(owner.get("id")) != actor_id:
            proposals.append(
                SyncProposal(
                    proposal_type="mitre_alias_conflict",
                    mitre_group=group.mitre_id,
                    dedup_key=f"{group.mitre_id}:{low}",
                    actor_id=actor_id,
                    payload={
                        "alias": alias,
                        "mitre_actor_id": actor_id,
                        "mitre_actor_canonical": str(actor.get("canonical", "")),
                        "current_owner_id": str(owner.get("id", "")),
                        "current_owner_canonical": str(owner.get("canonical", "")),
                    },
                    rationale=(
                        f"MITRE ({group.mitre_id}) は『{alias}』を"
                        f"『{actor.get('canonical')}』の別名としているが、"
                        f"辞書では『{owner.get('canonical')}』に帰属済。"
                        "承認すると alias を MITRE 側の actor に付け替える"
                    ),
                )
            )
        else:
            add_aliases.append(alias)
            my_names.add(low)

    have_malware = {str(m).lower() for m in actor.get("associated_malware") or []}
    add_malware = tuple(m for m in group.associated_malware if m.lower() not in have_malware)

    have_refs = set(actor.get("references") or [])
    add_refs = tuple(r for r in group.references if r not in have_refs)

    # 既知 TTP: T 番号 (先頭 token) で比較する additive 差分
    have_ttps = {str(t).split(" ")[0].upper() for t in actor.get("mitre_ttps") or []}
    add_ttps = tuple(t for t in group.ttps if t.split(" ")[0].upper() not in have_ttps)

    new_summary_en: str | None = None
    summary_sha1: str | None = None
    if group.summary:
        sha = _sha1(group.summary)
        if sha != actor.get("mitre_summary_sha1"):
            new_summary_en = group.summary
            summary_sha1 = sha

    update = ActorAutoUpdate(
        actor_id=actor_id,
        add_aliases=tuple(add_aliases),
        skipped_generic_aliases=tuple(skipped_generic),
        add_malware=add_malware,
        add_references=add_refs,
        add_ttps=add_ttps,
        set_mitre_group=group.mitre_id if not actor.get("mitre_group") else None,
        new_summary_en=new_summary_en,
        summary_sha1=summary_sha1,
    )
    return update, proposals


def _new_actor_proposal(group: MitreGroup, used_ids: set[str]) -> SyncProposal:
    """ミッション該当の未収載 group → 新規 actor 追加のレビュー提案。"""
    base = slugify_actor_id(group.canonical)
    actor_id = base
    n = 2
    while actor_id in used_ids:
        actor_id = f"{base}_{n}"
        n += 1
    payload: dict[str, Any] = {
        "id": actor_id,
        "canonical": group.canonical,
        # 新規 actor に ambiguous ゲートは付かないため一般語 alias は最初から載せない
        # (承認時にそのまま辞書へ入り誤帰属源になる)。
        "aliases": [a for a in group.aliases if not is_generic_alias(a)],
        "mitre_group": group.mitre_id,
        "summary": group.summary,  # runner が和訳で置き換える
        "summary_en": group.summary,
        "associated_malware": list(group.associated_malware),
        "references": list(group.references),
        "mitre_ttps": list(group.ttps),
    }
    if group.nation:
        payload["nation"] = group.nation
        rationale = f"nation={group.nation} の推定根拠: {group.nation_reason}"
    else:
        payload["motivation"] = "financial"
        rationale = "主要犯罪グループ allowlist 該当 (nation なし、motivation=financial)"
    return SyncProposal(
        proposal_type="mitre_new_actor",
        mitre_group=group.mitre_id,
        dedup_key=group.mitre_id,
        actor_id=None,
        payload=payload,
        rationale=rationale,
    )


def compute_sync_plan(data: dict[str, Any], groups: list[MitreGroup]) -> SyncPlan:
    """Actor 辞書と MITRE groups の差分を計算する (pure、副作用なし)。"""
    actors = [a for a in data.get("actors", []) if isinstance(a, dict) and a.get("id")]
    by_mitre = {str(a["mitre_group"]).upper(): a for a in actors if a.get("mitre_group")}
    by_name = _build_name_owner_index(actors)
    used_ids = {str(a["id"]) for a in actors}

    auto_updates: list[ActorAutoUpdate] = []
    proposals: list[SyncProposal] = []
    matched = 0
    skipped = 0
    for group in groups:
        actor = _match_actor(group, by_mitre, by_name)
        if actor is not None:
            matched += 1
            update, conflicts = _diff_existing(actor, group, by_name)
            if update.has_changes:
                auto_updates.append(update)
            proposals.extend(conflicts)
            continue
        if group.nation is None and not group.is_criminal:
            skipped += 1
            continue
        proposals.append(_new_actor_proposal(group, used_ids))

    return SyncPlan(
        auto_updates=auto_updates,
        proposals=proposals,
        matched=matched,
        skipped_out_of_mission=skipped,
    )


def apply_auto_update(
    data: dict[str, Any], update: ActorAutoUpdate, summary_ja: str | None
) -> dict[str, Any]:
    """auto update を適用した新 data を返す (元 data は変更しない)。

    summary_ja: ``update.new_summary_en`` の和訳。翻訳失敗時は None を渡すと
    summary は据え置き (次回 run で再試行される)。
    """
    new_actors: list[Any] = []
    for a in data.get("actors", []):
        if not isinstance(a, dict) or str(a.get("id")) != update.actor_id:
            new_actors.append(a)
            continue
        actor = dict(a)
        if update.add_aliases:
            actor["aliases"] = [*(actor.get("aliases") or []), *update.add_aliases]
        if update.add_malware:
            actor["associated_malware"] = [
                *(actor.get("associated_malware") or []),
                *update.add_malware,
            ]
        if update.add_references:
            actor["references"] = [*(actor.get("references") or []), *update.add_references]
        if update.add_ttps:
            actor["mitre_ttps"] = sorted([*(actor.get("mitre_ttps") or []), *update.add_ttps])
        if update.set_mitre_group:
            actor["mitre_group"] = update.set_mitre_group
        if update.new_summary_en and summary_ja:
            actor["summary"] = summary_ja
            actor["mitre_summary_sha1"] = update.summary_sha1
        new_actors.append(actor)
    return {**data, "actors": new_actors}


# ---------- LLM 翻訳 ----------

TRANSLATE_SYSTEM_PROMPT = (
    "あなたは CTI (サイバー脅威インテリジェンス) アナリスト向けの英日翻訳者です。"
    "以下のルールを厳守してください:\n"
    "- 脅威アクター名・マルウェア名・ツール名・作戦名・組織名・製品名・サービス名などの"
    "固有名詞は翻訳せず原文表記のまま残す\n"
    "- MITRE ATT&CK ID (G/T/S 番号)・CVE 番号はそのまま維持する\n"
    "- 技術用語は定訳があれば日本語 (例: lateral movement → 横展開)、なければ原文のまま\n"
    "- 簡潔で自然な日本語にする\n"
    "- 出力は翻訳文のみとし、前置き・注釈・修飾を一切付けない"
)


async def translate_summary(llm: LLMClient, text: str) -> str:
    """MITRE 英語 summary をローカル LLM で和訳する。

    固有名詞 (アクター名/マルウェア/ツール等) は prompt で原文維持を強制。
    失敗時は原文をそのまま返す (同期全体を止めない。次回 run で再試行)。
    """
    if not text.strip():
        return text
    try:
        resp = await llm.generate(
            prompt=f"次の脅威アクター解説を日本語に翻訳してください:\n\n{text}",
            system=TRANSLATE_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=_TRANSLATE_MAX_TOKENS,
            think=False,  # 翻訳は単純タスク。thinking ブロックで本文圧迫されるのを防ぐ
        )
    except LLMError as e:
        _log.warning("mitre_sync_translate_failed", error=str(e))
        return text
    out = resp.text.strip()
    return out or text


# ---------- runner (pipeline から呼ばれる) ----------


def _harvest_alias_labels(repo: Any, update: Any) -> None:
    """MITRE 自動適用の alias 追加を tuning_labels (E1) へ記録する (較正格子 P1 収穫②)。

    repo が record_tuning_label を持たない (テストの fake 等) 場合と書込失敗は
    warning に落として同期を続行する。
    """
    record = getattr(repo, "record_tuning_label", None)
    if record is None:
        return
    for alias in update.add_aliases:
        try:
            record(
                dedup_key=f"mitre_alias:{update.actor_id}:{str(alias).lower()}",
                field="actor_alias",
                label_value=update.actor_id,
                source="E1",
                strength="strong",
                provenance=json.dumps(
                    {"producer": "mitre_sync", "alias": str(alias)}, ensure_ascii=False
                ),
            )
        except Exception as e:  # noqa: BLE001 — ラベル記録の失敗で同期を止めない
            _log.warning(
                "mitre_sync_label_record_failed",
                actor_id=update.actor_id,
                error=str(e),
            )


@dataclass(frozen=True)
class MitreSyncResult:
    """1 回の MITRE 同期の実行結果。"""

    groups_total: int
    matched: int
    auto_applied: int
    summaries_translated: int
    proposals_new: int
    proposals_skipped_duplicate: int
    errors: list[str]
    # 一般語のため取り込まなかった alias の総数 (filter が効いていることの可視化)。
    generic_aliases_skipped: int = 0


async def run_mitre_actor_sync(
    *,
    llm: LLMClient,
    repo: Any | None,
    run_id: int | None,
    dry_run: bool,
    write_yaml: Any | None,
) -> MitreSyncResult:
    """MITRE 同期の本体。

    Args:
        llm: summary 和訳用 (main model)。
        repo: ``RunHistoryRepository`` (提案の永続化先)。None なら提案は記録しない。
        run_id: 提案に紐付ける run_id。
        dry_run: True なら yaml 書込・提案永続化を行わずログのみ。
        write_yaml: ``Callable[[str, str], None]`` — (yaml content, commit message) を
            受けて書込+git commit する callback (FileEditor を pipeline 側で束縛)。
    """
    groups = await fetch_mitre_groups()
    data = load_actors_raw()
    plan = compute_sync_plan(data, groups)
    errors: list[str] = []

    # 1. 自動適用 (summary は和訳してから)
    translated = 0
    applied = 0
    generic_skipped = 0
    for update in plan.auto_updates:
        if update.skipped_generic_aliases:
            generic_skipped += len(update.skipped_generic_aliases)
            _log.info(
                "mitre_sync_generic_alias_skipped",
                actor_id=update.actor_id,
                aliases=list(update.skipped_generic_aliases),
            )
        summary_ja: str | None = None
        if update.new_summary_en:
            if dry_run:
                # dry-run は件数把握のみ (バックフィル前に全 94 件の LLM 呼出が
                # 走るのを防ぐ)。実適用時のみ翻訳する。
                translated += 1
            else:
                summary_ja = await translate_summary(llm, update.new_summary_en)
                if summary_ja != update.new_summary_en:
                    translated += 1
                else:
                    # 翻訳できなかった (LLM エラー等) → summary は据え置き次回再試行
                    summary_ja = None
                    errors.append(f"translate failed: {update.actor_id}")
        data = apply_auto_update(data, update, summary_ja)
        applied += 1
        _log.info(
            "mitre_sync_auto_update",
            actor_id=update.actor_id,
            aliases=len(update.add_aliases),
            malware=len(update.add_malware),
            references=len(update.add_references),
            summary_updated=bool(update.new_summary_en and summary_ja),
        )
        # 較正格子 P1 (収穫②): MITRE 帰属確定を E1 遅延正解ラベルとして残す。
        # push 型 hook なのは、適用済み変更の一覧を DB に残す他の記録が無く
        # sweep で後から辿れないため。dedup_key で再同期は冪等。失敗しても同期は止めない。
        if not dry_run and repo is not None:
            _harvest_alias_labels(repo, update)

    if applied and not dry_run and write_yaml is not None:
        write_yaml(
            render_actors_yaml(data),
            f"chore(sync): MITRE actor sync — {applied} 件自動適用",
        )

    # 2. レビュー提案 (新規 actor は summary を和訳して payload に格納)
    new_proposals = 0
    skipped_dup = 0
    for proposal in plan.proposals:
        if repo is None or dry_run:
            new_proposals += 1  # dry-run: 永続化せず「提案される件数」のみ報告
            continue
        try:
            if repo.find_actor_update_proposal(
                proposal_type=proposal.proposal_type, dedup_key=proposal.dedup_key
            ):
                skipped_dup += 1
                continue
            payload = dict(proposal.payload)
            if proposal.proposal_type == "mitre_new_actor" and payload.get("summary"):
                payload["summary"] = await translate_summary(llm, str(payload["summary"]))
            repo.insert_actor_update_proposal(
                run_id=run_id,
                proposal_type=proposal.proposal_type,
                mitre_group=proposal.mitre_group,
                dedup_key=proposal.dedup_key,
                actor_id=proposal.actor_id,
                payload=json.dumps(payload, ensure_ascii=False),
                rationale=proposal.rationale,
            )
            new_proposals += 1
        except Exception as e:  # noqa: BLE001 — 1 件の失敗で同期全体を止めない
            _log.warning(
                "mitre_sync_proposal_persist_failed",
                dedup_key=proposal.dedup_key,
                error=str(e),
            )
            errors.append(f"proposal {proposal.dedup_key}: {type(e).__name__}: {e}")

    _log.info(
        "mitre_sync_complete",
        groups=len(groups),
        matched=plan.matched,
        auto_applied=applied,
        translated=translated,
        proposals_new=new_proposals,
        proposals_duplicate=skipped_dup,
        out_of_mission=plan.skipped_out_of_mission,
        dry_run=dry_run,
    )
    return MitreSyncResult(
        groups_total=len(groups),
        matched=plan.matched,
        auto_applied=applied,
        summaries_translated=translated,
        proposals_new=new_proposals,
        proposals_skipped_duplicate=skipped_dup,
        errors=errors,
        generic_aliases_skipped=generic_skipped,
    )
