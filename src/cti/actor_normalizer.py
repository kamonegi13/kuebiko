"""脅威アクター名のエイリアス正規化 (Phase 4)。

CTI 報道では同じアクターが複数の名前で呼ばれる:
    - Volt Typhoon = Vanguard Panda = BRONZE SILHOUETTE
    - APT41 = Wicked Panda = Barium = Winnti
    - APT29 = Cozy Bear = Midnight Blizzard

このモジュールは ``config/cti/actor_aliases.yaml`` をロードし、与えられた
テキストから既知アクターを検出して正規名 + エイリアス + MITRE Group ID
を返す。長い別名から優先的に試すことで誤検出を抑える。

主用途:
    1. Grok 経路: ``GrokIncident.related_actor`` を正規化
    2. RSS 経路: LLM 要約結果のアクター名を正規化
    3. Discord 投稿で「Volt Typhoon (別名: Vanguard Panda; MITRE: G1017)」と表示
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.logging_config import get_logger

_log = get_logger(__name__)


# Phase 1 Q3: actor 名の word-boundary 一致。
# 旧実装は substring 一致 ("APT1" が "APT10" に誤爆、"APT3"⊂"APT30" 等) で誤帰属していた。
# 日本語混在テキストでは ``\b`` が ASCII/CJK 境界で機能せず "Lazarusが" を取りこぼすため、
# **ASCII 英数の直前後のみ** を境界とする lookaround を使う。これで:
#   - "APT1" は "APT10" に誤爆しない (後続 "0" が ASCII 英数)
#   - "APT1が" / "Lazarus型" / "Salt Typhoon の活動" は正しく拾う (後続が非 ASCII)
@lru_cache(maxsize=4096)
def _alias_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])", re.IGNORECASE)


def _name_in_text(name: str, text: str) -> bool:
    """actor 名 (canonical/alias) が text 中に word-boundary 付きで出現するか。"""
    return bool(name) and _alias_pattern(name).search(text) is not None


def _norm_slug(s: str) -> str:
    """名前空間キーの正規化: 英数字以外を除去し casefold (source slug 写像用)。"""
    return re.sub(r"[^a-z0-9]", "", (s or "").casefold())


# Part B: 曖昧アクター (Anonymous 等) を確定させるハクティビズム文脈 cue (既定セット)。
# substring・小文字比較。匿名情報源を含む一般記事には現れず、ハクティビスト作戦記事には
# 必ず 1 つ以上現れる、を狙った高精度セット (誤検出を構造的に抑える)。
DEFAULT_AMBIGUOUS_CUES: tuple[str, ...] = (
    # English
    "hacktivist",
    "hacktivism",
    "ddos",
    "defac",  # defacement / defaced
    "claimed responsibility",
    "claimed credit",
    "claimed the attack",
    "knocked offline",
    "took offline",
    "took down",
    "#op",  # #OpIsrael 等の作戦タグ
    # 日本語
    "ハクティビスト",
    "ハクティビズム",
    "犯行声明",
    "犯行を主張",
    "攻撃を主張",
    "改ざん",
    "サービス妨害",
    "サービス拒否",
)

# サイバー犯罪/APT グループ名が一般語衝突する ambiguous actor の既定 cue
# (entity パイプライン棚卸し 2026-07-29, docs/entity_pipeline_inventory.md §6)。
# DEFAULT_AMBIGUOUS_CUES はハクティビズム文脈用でランサム/諜報グループには合わず、legit な
# 記事を gate で落とす under-attribution を招く。cue は substring 照合のため "apt"/"c2" のような
# 短語 (adapt/chapter に誤ヒット) は避け、識別性の高い語のみ列挙する。
CYBERCRIME_CONTEXT_CUES: tuple[str, ...] = (
    # ransomware
    "ransomware",
    "ransom",
    "leak site",
    "encrypt",
    "extortion",
    "affiliate",
    "data breach",
    "victim",
    # espionage / APT
    "espionage",
    "cyberespionage",
    "cyber-espionage",
    "state-sponsored",
    "state sponsored",
    "advanced persistent",
    "nation-state",
    # generic cyber intrusion
    "malware",
    "backdoor",
    "exfiltrat",
    "threat actor",
    "threat group",
    "hacking group",
    "cyberattack",
    "cyber attack",
    "intrusion",
    "phishing",
    "credential",
    "command-and-control",
    "command and control",
    "web shell",
    # 日本語
    "ランサムウェア",
    "身代金",
    "リークサイト",
    "暗号化",
    "マルウェア",
    "攻撃グループ",
    "脅威アクター",
    "標的型",
    "サイバー攻撃",
    "諜報",
    "スパイ活動",
    "バックドア",
    "不正アクセス",
    "情報窃取",
)


def _text_has_cue(text_lower: str, cues: tuple[str, ...]) -> bool:
    """ハクティビズム文脈 cue が (小文字化済み) text に 1 つ以上含まれるか。"""
    return any(cue.lower() in text_lower for cue in cues)


DEFAULT_ALIASES_PATH = Path("config/cti/actor_aliases.yaml")


class ActorAlias(BaseModel):
    """1 つのアクター定義 (frozen)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    canonical: str
    aliases: tuple[str, ...] = ()
    mitre_group: str | None = None
    nation: str | None = None
    sponsor: str | None = None
    description: str = ""
    # Phase H Batch 12: ISO 3166-1 alpha-2 of suspected origin (e.g. "CN", "RU", "KP", "IR").
    # 空欄許容 (yaml で未指定なら None)。V5 Geo-Cyber Map で attack flow 起点として使用。
    origin: str | None = None
    # Phase Threats: actor family (例: typhoon / lazarus / bear / kitten / spider)。
    # actor 群を grouping して Threat Operations 画面で family 単位 filter を可能にする。
    family: str | None = None
    # Actors Stage 5: 実体種別。group=攻撃グループ (intrusion-set 相当、APT 認定) /
    # organization=国家情報機関そのもの (GRU/FSB/MSS 等、配下グループの sponsor) /
    # contractor=民間請負 (i-Soon/NTC Vulkan)。脅威アクター画面で「活動」(group) と
    # 「言及」(organization/contractor) を区別し、帰属の二重計上を防ぐために使う。
    kind: str = "group"
    # 親 organization の actor id (kind=group のみ)。sponsor (フリーテキスト) を機関
    # エントリへ正規化したもの。例: APT28→russia_gru。rollup と二重計上抑止に使う。
    sponsor_org: str | None = None
    # source slug 名前空間キー (R2、2026-07-26)。ransomware.live 等の構造化ソースが使う
    # グループ slug で、prose 照合の canonical/alias とは別レイヤ (mitre_group と同じ思想)。
    # 一般語衝突で prose alias に持てない名前 (canonical="Akira ransomware" に対する "akira"
    # 等) を **構造化ソースからの完全キー照合でのみ** 解決するために持つ。find/find_all は
    # 参照しない (記事本文を走査しないため一般語誤爆の危険がない)。resolve_source_slug 専用。
    source_slugs: tuple[str, ...] = ()
    # Part B (Actor Recall Layer): 名前が一般語と衝突する曖昧アクター (例: "Anonymous" ≒
    # 匿名情報源)。true のとき **名前一致 + ハクティビズム文脈 cue の共起** で初めてマッチさせ、
    # 偽陽性 (匿名情報源) も偽陰性 (本物の集団取りこぼし) も同時に減らす。
    ambiguous: bool = False
    # 曖昧アクターの判定に使う共起 cue (空なら DEFAULT_AMBIGUOUS_CUES)。値は yaml で上書き可。
    context_cues: tuple[str, ...] = ()
    # ── reference 用詳細 (Actor 辞書、MITRE/vendor 由来。matching には不使用) ──
    summary: str = ""  # 概説 (description より長い overview)
    motivation: str | None = None  # 諜報 / 金銭 / 破壊 / 影響工作 等
    first_seen: str | None = None  # 活動開始 (年 or 日付)
    target_sectors: tuple[str, ...] = ()  # 標的業種
    target_regions: tuple[str, ...] = ()  # 標的地域
    associated_malware: tuple[str, ...] = ()  # 使用マルウェア・ツール
    notable_campaigns: tuple[str, ...] = ()  # 主要作戦
    references: tuple[str, ...] = ()  # 出典 URL
    # 既知 TTP ("T1566 Phishing" 形式、mitre_sync 所有)。脅威アクターページで
    # 観測 TTP と突き合わせ「既知外 TTP」をハイライトするための knowledge 側データ
    mitre_ttps: tuple[str, ...] = ()
    # ── identity ライフサイクル (アクター辞書 Phase1、id は不透明な永久キーで rename 禁止) ──
    # status="merged" は redirect 墓標: 本 entry は照合に参加せず (aliases は継承先へ物理移動
    # 済み・0 件が不変条件)、canonical は歴史表示用に残す。行動史 (actor_observed_profile) や
    # 記事行の旧 id は不改変のまま、resolve_actor_id() が表示時に継承先へ解決する。
    # split (分割) は後ろ向き非対応 — 同一性が不確かなアクターは別 id で開始し確証後に merge。
    status: str = "active"  # "active" | "merged"
    merged_into: str | None = None  # status="merged" のとき必須: 継承先 actor id
    merged_at: str | None = None  # merge 決定日 (ISO date)
    merge_note: str = ""  # merge の根拠メモ (人承認の記録)
    moved_aliases: tuple[str, ...] = ()  # 継承先へ移した alias (merge undo 用の来歴)

    @field_validator("nation")
    @classmethod
    def _normalize_nation(cls, v: str | None) -> str | None:
        """nation (ISO-2) は小文字が規約 — parse 時に正規化する。

        承認キュー payload 経由で大文字 (CN/KP) が yaml に混入した実績 (2026-08-21)。
        消費側は集計 key・敵対国判定 (小文字集合) に使うため、非正規化のまま通すと
        国が黙って割れる (概況で cn/CN が別行になり、敵対国判定から漏れた)。
        全読者がここを通るので、正規化はこの 1 点に置く。
        """
        return v.lower() if v else v

    @property
    def is_merged(self) -> bool:
        return self.status == "merged"

    @property
    def all_names(self) -> tuple[str, ...]:
        """canonical + aliases を 1 つのリストに統合 (重複なし)。"""
        seen: set[str] = set()
        out: list[str] = []
        for name in (self.canonical, *self.aliases):
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return tuple(out)

    def display_with_aliases(self) -> str:
        """Discord 表示用文字列を返す。

        例: ``Volt Typhoon (別名: Vanguard Panda, BRONZE SILHOUETTE; MITRE: G1017)``
        """
        parts = [self.canonical]
        annotations: list[str] = []
        if self.aliases:
            annotations.append(f"別名: {', '.join(self.aliases[:3])}")
        if self.mitre_group:
            annotations.append(f"MITRE: {self.mitre_group}")
        if annotations:
            parts.append(f"({'; '.join(annotations)})")
        return " ".join(parts)


def resolve_ambiguous_cues(actor: ActorAlias) -> tuple[str, ...]:
    """ambiguous actor の文脈 cue を解決する (cue 選択の SSoT、2026-07-29)。

    - 明示 ``context_cues`` があればそれを使う
    - ``hacktivist`` family は :data:`DEFAULT_AMBIGUOUS_CUES` (ハクティビズム文脈)
    - それ以外 (ランサム/APT/'') は :data:`CYBERCRIME_CONTEXT_CUES`

    従来 3 箇所 (find / matched_names_for / threat_operations の抽出) が個別に
    ``context_cues or DEFAULT_AMBIGUOUS_CUES`` を持ち、ランサム系 ambiguous actor に
    ハクティビズム cue を当てて legit 記事を落とす穴があった。本関数に集約し drift を防ぐ。
    """
    if actor.context_cues:
        return actor.context_cues
    if actor.family == "hacktivist":
        return DEFAULT_AMBIGUOUS_CUES
    return CYBERCRIME_CONTEXT_CUES


# ---------- 同一性 cue (docs/actor_identity_cue_design.md, 2026-07-31) ----------
# 旧ジャンル cue (CYBERCRIME_CONTEXT_CUES 等) は「サイバー記事か」しか検査せず、CTI 専用
# コーパスでは常に成立するため、一般語衝突アクター (play/deadlock/tick 等) の言及 entity の
# 59% が誤検出だった (90日実測)。曖昧解消は「この記事は**このアクター**の話か」= 同一性証拠
# (E0-E7) で行う。rollback: env ACTOR_IDENTITY_CUES=0 で旧ジャンル cue 挙動へ即時復帰。

# E4/E5: 正当な言及は必ず修飾付きで書かれる ("Play ransomware" / 「ランサムウェア Play」)。
# 裸の一般語 (動詞 play / 技術用語 deadlock) はこの隣接が成立しない。取りこぼし発見時は追加可。
_ADJ_QUALIFIERS_AFTER = (
    r"(?:ransomwares?|ransom|groups?|gangs?|actors?|apt|crew|collective|hacktivists?|operations?"
    r"|cyber|extortion|claims?|claimed"
    r"|ランサムウェア|ランサム|グループ|攻撃グループ|集団|一味|犯行声明)"
)
_ADJ_QUALIFIERS_BEFORE = (
    r"(?:ransomware|group|gang|actor|apt|hacktivist collective|collective"
    r"|ランサムウェア|ランサム|グループ|攻撃グループ|脅威グループ|集団)"
)
# 名前と修飾語の間に許す区切り (空白/引用符/括弧/中黒)
_ADJ_SEP = r"[\s　\"'「」『』()（）・-]{0,3}"


@lru_cache(maxsize=1024)
def _adjacency_patterns(
    name_lower: str,
) -> tuple[re.Pattern[str], re.Pattern[str], re.Pattern[str]]:
    """名前の隣接パターン (E4 後置修飾 / E5 前置修飾 / E6 レコード形式) をコンパイル。

    E6 は 3 形式 (FN サンプル実測 2026-07-31 で追加):
    - ransomware.live 被害者レコード: 行頭 ``name: 被害者``
    - 被害者公表ニュースタイトル: ``name、新たな被害(組織|者)として…``
    - ランサムダイジェスト集計行: ``name - 3 victims``
    """
    esc = re.escape(name_lower)
    # 直接隣接 ("Play ransomware") に加え、copula 構文 ("Axiom is a Chinese cyberespionage
    # group" / 「X は中国系の諜報グループ」= 語間 3 語/30 字まで) も正当な言及の定型として認める。
    after = re.compile(
        rf"{esc}(?:{_ADJ_SEP}{_ADJ_QUALIFIERS_AFTER}"
        rf"|\s+(?:is|was|remains|,)\s+(?:an?\s+|the\s+)?(?:[\w-]+\s+){{0,3}}{_ADJ_QUALIFIERS_AFTER}"
        rf"|\s*(?:は|とは)[^\n。]{{0,30}}(?:グループ|集団|アクター|攻撃者))",
        re.IGNORECASE,
    )
    before = re.compile(rf"{_ADJ_QUALIFIERS_BEFORE}{_ADJ_SEP}{esc}", re.IGNORECASE)
    victim = re.compile(
        rf"(?:^\s*{esc}\s*:|{esc}\s*[、,]\s*新たな被害|{esc}\s*[-–—]\s*\d+\s*victims?)",
        re.IGNORECASE | re.MULTILINE,
    )
    return after, before, victim


# ジャンル語 denylist: context_cues に入れてはならない語 (guard test が全アクターに強制)。
# これらは「サイバー記事である」ことしか意味せず、同一性の曖昧解消に使えない。
# Tick 汚染 (2026-07-30) の再発を CI で構造的に遮断する (mitre_sync 再発ループと同じ思想:
# SSoT を実装と test の双方が参照)。
GENRE_CUE_WORDS: frozenset[str] = frozenset(
    {
        # EN
        "ransomware",
        "ransom",
        "malware",
        "backdoor",
        "espionage",
        "cyberespionage",
        "cyber-espionage",
        "cyber espionage",
        "state-sponsored",
        "state sponsored",
        "threat actor",
        "threat group",
        "hacking group",
        "cyberattack",
        "cyber attack",
        "intrusion",
        "phishing",
        "credential",
        "data breach",
        "extortion",
        "exfiltration",
        "advanced persistent",
        "nation-state",
        "apt",
        "victim",
        "leak site",
        "encrypt",
        "hacktivism",
        "ddos",
        "defacement",
        # JA
        "ランサムウェア",
        "ランサム",
        "身代金",
        "マルウェア",
        "バックドア",
        "諜報",
        "スパイ",
        "スパイ活動",
        "サイバー攻撃",
        "標的型",
        "標的型攻撃",
        "攻撃グループ",
        "脅威アクター",
        "脅威グループ",
        "不正アクセス",
        "情報窃取",
        "暗号化",
        "リークサイト",
        "サイバースパイ",
    }
)


def _identity_cues_enabled() -> bool:
    """同一性 cue ゲートの on/off (既定 on)。0 で旧ジャンル cue に fallback (rollback 用)。"""
    return os.environ.get("ACTOR_IDENTITY_CUES", "1") != "0"


def has_identity_evidence(actor: ActorAlias, matched_name: str, text: str) -> bool:
    """text に「このアクターの話である」同一性証拠が 1 つ以上あるか (E0-E7)。

    ambiguous actor の曖昧解消に使う。ジャンル語 (ransomware 等) は証拠にならない —
    証拠は辞書エントリ自身 (別名/マルウェア/MITRE id/固有 cue) と、名前への修飾の
    隣接 (E4/E5)・被害者レコード形式 (E6) から導出する。
    """
    text_lower = text.lower()
    name_lower = matched_name.lower()
    # E0: マッチした名前自体が複数語 (Bronze Butler 等) = 固有名でそれ自体が証拠
    if " " in matched_name.strip() or "-" in matched_name.strip():
        return True
    # E1: 他の別名の共起 (マッチ名以外)
    for other in actor.all_names:
        if other.lower() != name_lower and _name_in_text(other, text):
            return True
    # E2: 関連マルウェア名の共起 (マッチ名と同名の malware は自己証明になるため除外 —
    # Akira のように actor 名 = malware 名のエントリで裸名が常時素通りする穴の防止、2026-08-01)
    for mal in actor.associated_malware:
        if mal and mal.lower() != name_lower and _name_in_text(mal, text):
            return True
    # E3: MITRE Group ID の共起 (G0060 等)
    if actor.mitre_group and actor.mitre_group.lower() in text_lower:
        return True
    # E4/E5/E6: 隣接修飾・被害者レコード
    after, before, victim = _adjacency_patterns(name_lower)
    if after.search(text_lower) or before.search(text_lower) or victim.search(text_lower):
        return True
    # E7: 手書き固有 cue (guard test がジャンル語混入を遮断する前提)
    return bool(actor.context_cues) and _text_has_cue(text_lower, actor.context_cues)


class ActorAliasRegistry(BaseModel):
    """全アクター定義のレジストリ (frozen)。"""

    model_config = ConfigDict(frozen=True, extra="ignore")

    actors: tuple[ActorAlias, ...] = Field(default_factory=tuple)

    @staticmethod
    def _matched_name(actor: ActorAlias, text: str, text_lower: str) -> str | None:
        """actor が text にマッチすればマッチした名前を返す (なければ None)。

        曖昧アクター (ambiguous=true) は **名前一致 + ハクティビズム文脈 cue の共起** を要求し、
        一般語 (匿名情報源 等) の誤検出を排除する (Part B)。非曖昧は従来の word-boundary 一致。
        """
        hit = next((name for name in actor.all_names if _name_in_text(name, text)), None)
        if hit is None:
            return None
        if actor.ambiguous:
            if _identity_cues_enabled():
                # 同一性 cue (E0-E7): 「このアクターの話」の証拠を要求 (fail-closed)。
                if not has_identity_evidence(actor, hit, text):
                    _log.debug(
                        "ambiguous_actor_no_identity_evidence",
                        actor_id=actor.id,
                        matched=hit,
                    )
                    return None
            else:
                # rollback 経路 (ACTOR_IDENTITY_CUES=0): 旧ジャンル cue 挙動
                cues = resolve_ambiguous_cues(actor)
                if not _text_has_cue(text_lower, cues):
                    return None
        return hit

    def find(self, text: str) -> ActorAlias | None:
        """テキスト中に既知アクターの名前 (canonical or alias) があれば返す。

        長い名前から優先的に試す (例: ``BRONZE SILHOUETTE`` → ``BRONZE``)。
        大文字小文字非区別、word boundary (ASCII 英数境界) を考慮する。完全一致は不要
        (例: 「中国系 Volt Typhoon の事前配置」でも検出)。Phase 1 Q3 で substring から
        word-boundary 一致に変更 ("APT1"⊂"APT10" 等の誤帰属を排除)。
        曖昧アクターは文脈 cue 共起時のみ候補化する (Part B)。
        """
        if not text:
            return None
        text_lower = text.lower()
        # 長い名前から評価 (より具体的なものを優先)。merged 墓標は照合に参加しない
        # (alias は継承先へ移動済みで、canonical も継承先の alias として照合される)。
        candidates: list[tuple[int, ActorAlias]] = []
        for actor in self.actors:
            if actor.is_merged:
                continue
            hit = self._matched_name(actor, text, text_lower)
            if hit is not None:
                candidates.append((len(hit), actor))
        if not candidates:
            return None
        # 最も長い名前マッチを返す
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    def find_all(self, text: str) -> list[ActorAlias]:
        """テキスト中の **全** マッチアクターを返す (重複除去、登場順)。"""
        if not text:
            return []
        text_lower = text.lower()
        out: list[ActorAlias] = []
        seen_ids: set[str] = set()
        for actor in self.actors:
            if actor.id in seen_ids or actor.is_merged:
                continue
            if self._matched_name(actor, text, text_lower) is not None:
                seen_ids.add(actor.id)
                out.append(actor)
        return out

    def matched_names_for(self, actor: ActorAlias, text: str) -> tuple[str, ...]:
        """actor の all_names のうち text に出現する**全て**の名前 (F5 alias 使用統計用)。

        find/find_all は最初のヒット名で打ち切るため、どの別名が実際に発火しているかの
        統計には使えない。ambiguous ゲートは find と同一条件を適用する。
        """
        if not text:
            return ()
        hits = tuple(name for name in actor.all_names if _name_in_text(name, text))
        if not hits:
            return ()
        if actor.ambiguous:
            if _identity_cues_enabled():
                if not has_identity_evidence(actor, hits[0], text):
                    return ()
            else:
                cues = resolve_ambiguous_cues(actor)
                if not _text_has_cue(text.lower(), cues):
                    return ()
        return hits

    def knows_name(self, text: str) -> bool:
        """text に既知アクター名が (ambiguous gate 抜きで) 含まれるか。

        新興候補採取 (Part C1) で「この文字列は既に辞書にある actor か」を判定するため、
        find/find_all と違い曖昧 cue を要求しない (登録済みなら候補化しない、が目的)。
        """
        if not text:
            return False
        return any(_name_in_text(name, text) for actor in self.actors for name in actor.all_names)

    def by_id(self, actor_id: str) -> ActorAlias | None:
        """id で entry を返す (merged 墓標も返す — 歴史表示用)。canonical が必要なら
        ``resolve_actor_id`` を先に通すこと。"""
        for a in self.actors:
            if a.id == actor_id:
                return a
        return None

    def resolve_source_slug(self, slug: str) -> ActorAlias | None:
        """source データの slug (ransomware.live の group 名等) を辞書エントリに解決する。

        R2 (2026-07-26): slug は**散文照合の名前ではなく名前空間キー**。英数字以外を除去+
        casefold の正規化写像で照合する — "thegentlemen"→the_gentlemen /
        "incransom"→inc_ransom / "Akira"→akira を吸収する。**照合 alias には足さない**
        (一般語 alias の ambiguous ゲート設計を壊さないため、slug 解決は独立層)。
        merged 墓標は継承先へ解決する。
        """
        key = _norm_slug(slug)
        if not key:
            return None
        for actor in self.actors:
            names = (actor.id, *actor.all_names, *actor.source_slugs)
            if any(_norm_slug(n) == key for n in names):
                resolved = self.by_id(self.resolve_actor_id(actor.id))
                return resolved if resolved is not None else actor
        return None

    def resolve_actor_id(self, actor_id: str) -> str:
        """保存データ中の actor id を現在の canonical id へ解決する (identity seam)。

        merged_into チェーンを追従する (循環ガード付き)。未知の id・active な id は
        そのまま返す (恒等)。**保存済みの記事行・月次行・anchors を読む全経路はこの
        seam を通す** — redirect が 0 件の間は恒等関数として振る舞う。
        """
        visited: set[str] = set()
        current = actor_id
        while current not in visited:
            visited.add(current)
            entry = self.by_id(current)
            if entry is None or not entry.is_merged or not entry.merged_into:
                return current
            current = entry.merged_into
        # 循環 (設定不正) — guard test で防ぐが、万一の場合は入力を返して暴走を避ける
        _log.warning("actor_redirect_cycle_detected", actor_id=actor_id)
        return actor_id

    def merged_sources(self, canonical_id: str) -> tuple[str, ...]:
        """canonical_id へ redirect している旧 id 一覧 (表示時合算用、チェーン込み)。"""
        return tuple(
            a.id
            for a in self.actors
            if a.is_merged and a.id != canonical_id and self.resolve_actor_id(a.id) == canonical_id
        )


# YAML parse は 156 actor 規模で ~0.35s かかり、snapshot / 検索 / 表示系が
# リクエストごとに呼ぶため (mtime_ns, size) キーの module cache で省く。
# UI 編集 / mitre-sync は atomic write で mtime が変わるので自動失効する。
_registry_cache: dict[Path, tuple[tuple[int, int], ActorAliasRegistry]] = {}
_families_cache: dict[Path, tuple[tuple[int, int], dict[str, dict[str, str]]]] = {}


def _file_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load_actor_aliases(path: Path = DEFAULT_ALIASES_PATH) -> ActorAliasRegistry:
    """yaml から actor 辞書をロード。失敗時は空 registry。"""
    if not path.exists():
        _log.info("actor_aliases_yaml_missing", path=str(path))
        return ActorAliasRegistry()
    key = _file_key(path)
    cached = _registry_cache.get(path)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except Exception as e:  # noqa: BLE001
        _log.warning("actor_aliases_yaml_parse_failed", path=str(path), error=str(e))
        return ActorAliasRegistry()
    raw_actors = data.get("actors", [])
    if not isinstance(raw_actors, list):
        return ActorAliasRegistry()
    actors: list[ActorAlias] = []
    for entry in raw_actors:
        if not isinstance(entry, dict):
            continue
        try:
            actors.append(ActorAlias.model_validate(entry))
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "actor_alias_entry_invalid",
                entry=entry,
                error=str(e),
            )
    _log.info("actor_aliases_loaded", count=len(actors), path=str(path))
    registry = ActorAliasRegistry(actors=tuple(actors))
    if key is not None:
        _registry_cache[path] = (key, registry)
    return registry


def load_actor_families(
    path: Path = DEFAULT_ALIASES_PATH,
) -> dict[str, dict[str, str]]:
    """yaml の families セクションを dict として返す (Phase Threats)。

    Returns:
        {family_id: {nation, label, description}} 形式の dict。
        families が無い yaml では空 dict。
    """
    if not path.exists():
        return {}
    key = _file_key(path)
    cached = _families_cache.get(path)
    if key is not None and cached is not None and cached[0] == key:
        # caller 側の書き換えで cache を汚さないよう浅い copy を返す
        return {fam_id: dict(info) for fam_id, info in cached[1].items()}
    try:
        text = path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    except Exception:  # noqa: BLE001
        return {}
    raw = data.get("families", {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for fam_id, info in raw.items():
        if not isinstance(info, dict):
            continue
        out[str(fam_id)] = {
            "nation": str(info.get("nation", "")),
            "label": str(info.get("label", fam_id)),
            "description": str(info.get("description", "")),
        }
    if key is not None:
        _families_cache[path] = (key, {fam_id: dict(info) for fam_id, info in out.items()})
    return out
