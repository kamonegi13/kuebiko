"""語彙レジストリの実体。

各 vocabulary は「value → 日本語ラベル」の順序付き集合。**backend が唯一の SSoT** で、
`/api/v1/vocabularies` 経由で frontend に配信される (frontend は静的マップを持たない)。

強制 (docs §4.6): `canonical` を持つ vocab は、その `values()` が backend の正規値集合
(named `Literal` / config / frozenset 等) と一致することを `tests/unit/test_vocabularies.py`
が assert する。名前付き正規定数がまだ無い vocab は `canonical=None` とし、定数を mint した
時点で突合対象に加える (定数無しのまま強制はできない — docs §4.1 の step 0)。

新しい vocab を足すときは (1) ここに `_vocab(...)` を 1 行、(2) 可能なら `canonical=` に
backend の正規値集合を渡す、(3) frontend 側の静的マップを撤去して `useVocab(name)` に置換。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from src.config_loader import IMPORTANCE_LEVELS, KNOWN_ARTICLE_CATEGORIES
from src.cti.geocoder import GeoPrecision
from src.cti.llm_routing_flags import EditorialStance
from src.cti.taxonomy_normalizer import PMESII_AXES
from src.prompts.rubric_guide import GROUPS
from src.storage.records import ArticleStatus, RunStatus, TriggerSource
from src.synthesis.grounded.hypotheses import HYPOTHESIS_MENU


@dataclass(frozen=True)
class VocabItem:
    """1 つの値とその表示ラベル。`order` は UI 上の並び (挿入順)。"""

    value: str
    label: str
    order: int
    description: str | None = None
    deprecated: bool = False


@dataclass(frozen=True)
class Vocabulary:
    """順序付きの value→label 集合。`canonical` があれば強制テストの突合対象になる。"""

    name: str
    items: tuple[VocabItem, ...]
    canonical: frozenset[str] | None = None

    def values(self) -> tuple[str, ...]:
        return tuple(item.value for item in self.items)

    def label_for(self, value: str) -> str:
        for item in self.items:
            if item.value == value:
                return item.label
        return value  # 未知値は原値へ (frontend humanizer と対称)

    def serialize(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for item in self.items:
            row: dict[str, object] = {
                "value": item.value,
                "label": item.label,
                "order": item.order,
            }
            if item.description is not None:
                row["description"] = item.description
            if item.deprecated:
                row["deprecated"] = True
            out.append(row)
        return out


def _vocab(
    name: str, labels: dict[str, str], *, canonical: frozenset[str] | None = None
) -> Vocabulary:
    """value→label の順序付き dict から Vocabulary を構築 (order=挿入順)。"""
    items = tuple(
        VocabItem(value=value, label=label, order=index)
        for index, (value, label) in enumerate(labels.items())
    )
    return Vocabulary(name=name, items=items, canonical=canonical)


# ---------------------------------------------------------------------------
# 登録済み語彙
# ---------------------------------------------------------------------------
# Phase 1 代表: run status。`RunStatus` は named Literal なので canonical を突合できる
# (docs §7 Phase 1)。ラベルは旧 frontend RUN_STATUS_LABELS と一致 (挙動不変)。
_RUN_STATUS_LABELS: dict[str, str] = {
    "succeeded": "成功",
    "failed": "失敗",
    "running": "実行中",
    "partial_failure": "一部失敗",
}


# importance / confidence / capability は CTI の重大度スケールなので **英語維持**
# (脅威ティア Critical/High/… や CVSS と整合。docs §2.6.1・memory ui-copy-policy 2026-07-21 改訂)。
# "unknown" は triage 未実施の sentinel (importance) / 能力の証拠不足 (capability)。
_IMPORTANCE_LABELS: dict[str, str] = {
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "unknown": "Unknown",
}

# capability_band の正規集合は actor_threat.CapabilityBand (heavy import 回避のため値は
# ここに直書きし、tests/unit/test_vocabularies.py が本物の Literal と一致を突合する)。
_CAPABILITY_VALUES: frozenset[str] = frozenset({"high", "medium", "low", "unknown"})


_REGISTRY: dict[str, Vocabulary] = {
    "run_status": _vocab(
        "run_status",
        _RUN_STATUS_LABELS,
        canonical=frozenset(get_args(RunStatus)),
    ),
    # importance の正規集合 = triage 出力 (IMPORTANCE_LEVELS) + 未 triage sentinel "unknown"。
    "importance": _vocab(
        "importance",
        _IMPORTANCE_LABELS,
        canonical=frozenset(IMPORTANCE_LEVELS) | {"unknown"},
    ),
    "capability_band": _vocab(
        "capability_band",
        _IMPORTANCE_LABELS,
        canonical=_CAPABILITY_VALUES,
    ),
    # ---- 運用・記述系 (日本語維持。docs §2.6.1) ----
    "stance": _vocab(
        "stance",
        {
            "factual_report": "事実報道",
            "analytical": "分析",
            "opinion": "論説",
            "propaganda": "プロパガンダ",
            "unknown": "不明",
        },
        canonical=frozenset(get_args(EditorialStance)),
    ),
    # article_status: marked_read は ArticleStatus Literal にあるが旧 labels.ts で欠落して
    # いた (生値表示になっていた)。ここで「既読」を補完し canonical で網羅を強制する。
    "article_status": _vocab(
        "article_status",
        {
            "summarized": "要約済",
            "posted": "投稿済",
            "marked_read": "既読",
            "extract_failed": "本文抽出失敗",
            "summarize_failed": "要約失敗",
            "post_failed": "投稿失敗",
            "skipped_duplicate": "重複でスキップ",
            "collected": "取得済",
            "collected_duplicate": "取得済 (重複)",
        },
        canonical=frozenset(get_args(ArticleStatus)),
    ),
    # trigger: RunRecord は scheduler/manual/cli のみ。reactive/recovery は表示上の追加種別。
    "trigger": _vocab(
        "trigger",
        {
            "scheduler": "自動実行",
            "manual": "手動実行",
            "cli": "コマンド実行",
            "reactive": "連動実行",
            "recovery": "自動リカバリ",
        },
        canonical=frozenset(get_args(TriggerSource)) | {"reactive", "recovery"},
    ),
    # health: CheckStatus は ok/warning/error。unknown は未チェック時の表示 sentinel。
    "health_status": _vocab(
        "health_status",
        {"ok": "正常", "warning": "注意", "error": "異常", "unknown": "不明"},
        canonical=frozenset({"ok", "warning", "error", "unknown"}),
    ),
    # category: KNOWN_ARTICLE_CATEGORIES + legacy "recap" (digest 由来の記事に現存)。
    "category": _vocab(
        "category",
        {
            "apt": "APT",
            "apt_leak": "APT リーク",
            "vulnerability": "脆弱性",
            "malware": "マルウェア",
            "incident": "インシデント",
            "breach": "情報漏洩",
            "advisory": "アドバイザリ",
            "policy": "政策",
            "phishing": "フィッシング",
            "research": "研究",
            "geopolitical": "地政学",
            "recap": "まとめ",
            "other": "その他",
        },
        canonical=frozenset(KNOWN_ARTICLE_CATEGORIES) | {"recap"},
    ),
    # category_group: UI 合成カテゴリ (backend _CATEGORY_GROUPS)。個別カテゴリと範囲差を明示。
    "category_group": _vocab(
        "category_group",
        {
            "vuln": "脆弱性・アドバイザリ",
            "threat": "マルウェア・APT 等",
            "incident_breach": "侵害・インシデント",
        },
        canonical=frozenset({"vuln", "threat", "incident_breach"}),
    ),
    # confidence: CTI 重大度と同じく **英語維持** (High/Moderate/Low、ICD 203)。backend に 2 scale
    # (ICD-203 grounded=high/moderate/low、diamond intent=high/medium/low) が実在するため
    # 両キーを 1 vocab に収める (英語なら Medium/Moderate は別ラベルで自然)。data 移行は不要。
    # canonical は両 Literal の和 (test が estimate.Confidence / diamond.Confidence の被覆を確認)。
    "confidence": _vocab(
        "confidence",
        {"high": "High", "medium": "Medium", "moderate": "Moderate", "low": "Low"},
        canonical=frozenset({"high", "medium", "moderate", "low"}),
    ),
    # period_type: 運用語 (日本語)。backend は複数定数 (_VALID_PERIODS/SpotlightPeriod/
    # SpotlightWindow/Cadence) に分散、値は共通。test が全定数の一致を確認する。
    "period_type": _vocab(
        "period_type",
        {"daily": "日次", "weekly": "週次", "monthly": "月次"},
        canonical=frozenset({"daily", "weekly", "monthly"}),
    ),
    # transport: ソースの取得方式。RSS/Atom は CTI/tech 語彙で英語維持、他は日本語。
    # atom は表示上の変種で TransportT (rss/sitemap/html_scraper) には無いため canonical に加える。
    "transport": _vocab(
        "transport",
        {
            "rss": "RSS",
            "atom": "Atom",
            "sitemap": "サイトマップ",
            "html_scraper": "HTML 解析",
        },
        canonical=frozenset({"rss", "atom", "sitemap", "html_scraper"}),
    ),
    # article_type: 記述語 (日本語)。advisory は backend ArticleType にあるが routingLabels で
    # 欠落していた (生値表示) → 補完。canonical は article_classifier.ArticleType と test で突合。
    "article_type": _vocab(
        "article_type",
        {
            "breaking": "速報",
            "advisory": "アドバイザリ",
            "recap": "まとめ・振り返り",
            "tutorial": "チュートリアル",
            "research": "研究・技術解析",
            "press": "プレスリリース",
            "opinion": "オピニオン",
        },
        canonical=frozenset(
            {"breaking", "advisory", "recap", "tutorial", "research", "press", "opinion"}
        ),
    ),
    # ---- long-tail (単一 consumer だが将来の multi-copy 化を防ぐため vocab へ集約) ----
    # 戦略的 intent。SocioPoliticalIntent。unknown は frontend INTENT_META で欠落していた補完。
    "intent": _vocab(
        "intent",
        {
            "espionage": "諜報",
            "financial": "金銭",
            "prepositioning": "事前配置",
            "disruption": "破壊",
            "influence": "影響工作",
            "hacktivism": "ハクティビズム",
            "coercion": "威圧",
            "deterrence": "抑止",
            "territorial": "領土",
            "subversion": "体制転覆",
            "diplomacy": "外交",
            "unknown": "不明",
        },
        canonical=frozenset(
            {
                "espionage",
                "financial",
                "prepositioning",
                "disruption",
                "influence",
                "hacktivism",
                "coercion",
                "deterrence",
                "territorial",
                "subversion",
                "diplomacy",
                "unknown",
            }
        ),
    ),
    "delta_type": _vocab(
        "delta_type",
        {
            "opened": "新規",
            "hypothesis_flip": "見立て転換",
            "strengthened": "強化",
            "weakened": "後退",
            "escalated": "拡大",
            "reopened": "再燃",
            "claim_revised": "更新",
            "closing": "収束",
            "no_change": "継続",
        },
        canonical=frozenset(
            {
                "opened",
                "hypothesis_flip",
                "strengthened",
                "weakened",
                "escalated",
                "reopened",
                "claim_revised",
                "closing",
                "no_change",
            }
        ),
    ),
    "situation_status": _vocab(
        "situation_status",
        {"active": "追跡中", "dormant": "休眠", "closed": "終結"},
        canonical=frozenset({"active", "dormant", "closed"}),
    ),
    # situation_rel_type: temporal_sequence は backend にあるが frontend で欠落していた補完。
    "situation_rel_type": _vocab(
        "situation_rel_type",
        {
            "same_actor": "同一アクター",
            "same_campaign": "同一作戦",
            "shared_nation": "国家の共有",
            "temporal_sequence": "時系列",
        },
        canonical=frozenset({"same_actor", "same_campaign", "shared_nation", "temporal_sequence"}),
    ),
    "polarity": _vocab(
        "polarity",
        {"supports": "支持", "contradicts": "反証", "neutral": "中立"},
        canonical=frozenset({"supports", "contradicts", "neutral"}),
    ),
    # assigned_by: llm は backend AssignedBy にあるが frontend で欠落していた補完。
    "assigned_by": _vocab(
        "assigned_by",
        {
            "anchor": "アンカー一致",
            "nation": "国家一致",
            "standing": "常設",
            "token": "語句一致",
            "seed": "初期登録",
            "llm": "LLM 判定",
        },
        canonical=frozenset({"anchor", "nation", "standing", "token", "seed", "llm"}),
    ),
    "forecast_scope": _vocab(
        "forecast_scope",
        {
            "actor": "アクター",
            "intent": "意図",
            "cve": "CVE",
            "malware_family": "マルウェア",
            "sector": "セクター",
            "country": "被害国",
        },
        canonical=frozenset({"actor", "intent", "cve", "malware_family", "sector", "country"}),
    ),
    "forecast_direction": _vocab(
        "forecast_direction",
        {"increasing": "増加", "decreasing": "減少", "stable": "横ばい"},
        canonical=frozenset({"increasing", "decreasing", "stable"}),
    ),
    # unevaluated = 情勢が再評価されず指標を一度も照会できなかった (外れではない)。
    # 的中率の分母から外す — 判定規則の SSoT は assessment/forecast.py `_terminal_status`。
    "forecast_verdict": _vocab(
        "forecast_verdict",
        {
            "realized": "現実化",
            "partial": "部分的",
            "missed": "外れ",
            "unevaluated": "未照会",
        },
        canonical=frozenset({"realized", "partial", "missed", "unevaluated"}),
    ),
    # ACH 仮説ラベルは hypotheses.py (値の源泉) から導出 — 静的複製は POSTURE 系 3 仮説の
    # 欠落 (生 ID が UI に漏出) を起こした (2026-07-23)。仮説追加は自動で配信に載る。
    "ach_hypothesis": _vocab(
        "ach_hypothesis",
        {h.id: h.label for h in HYPOTHESIS_MENU},
        canonical=frozenset(h.id for h in HYPOTHESIS_MENU),
    ),
    # activity_state: 脅威ティアの兄弟軸。日本語 (severity ではないため — docs §2.6 注意)。
    "activity_state": _vocab(
        "activity_state",
        {"spiking": "急増", "active": "活動中", "quiet": "静穏", "dormant": "休眠"},
        canonical=frozenset({"spiking", "active", "quiet", "dormant"}),
    ),
    # threat_tier: CTI 重大度スケール → 英語維持 (Critical/High/Moderate/Watch)。
    "threat_tier": _vocab(
        "threat_tier",
        {"critical": "Critical", "high": "High", "moderate": "Moderate", "watch": "Watch"},
        canonical=frozenset({"critical", "high", "moderate", "watch"}),
    ),
    "entity_type": _vocab(
        "entity_type",
        {
            "actor": "脅威アクター",
            "actor_provisional": "暫定アクター(未承認)",
            "cve": "CVE",
            "malware_family": "マルウェア",
            "tool": "ツール",
            "ttp": "ATT&CK",
            "ioc_ip": "IP",
            "ioc_domain": "ドメイン",
            "ioc_url": "URL",
            "ioc_sha256": "SHA-256",
            "ioc_sha1": "SHA-1",
            "ioc_md5": "MD5",
        },
    ),
    "matched_via": _vocab(
        "matched_via",
        {"keyword": "キーワード", "vector": "意味類似", "structured": "属性"},
        canonical=frozenset({"keyword", "vector", "structured"}),
    ),
    # PIR 照合条件ツリーの property 名 (authoring 統一 2026-07-23)。
    # SSoT = src/pir/signal_match.py LEAF_OPS (閉 enum、種別 A)。UI の照合条件表示が参照。
    "pir_match_property": _vocab(
        "pir_match_property",
        {
            "category": "分類",
            "intent": "意図",
            "victim_country": "被害国",
            "victim_sector": "被害セクター",
            "is_ransomware": "ランサムウェア事案",
            "feed_title": "フィード名",
            "text": "全文キーワード",
            "title": "タイトルキーワード",
            "actor": "アクター",
            "actor_nation": "アクター国籍",
        },
        canonical=frozenset(
            {
                "category",
                "intent",
                "victim_country",
                "victim_sector",
                "is_ransomware",
                "feed_title",
                "text",
                "title",
                "actor",
                "actor_nation",
            }
        ),
    ),
    "detected_via": _vocab(
        "detected_via",
        {
            "direct": "指定 URL から直接検出",
            "html_link": "ページ内リンクから検出",
            "category_path": "カテゴリパス探索で検出",
            "common_path": "定番パス探索で検出",
            "subdomain": "サブドメイン探索で検出",
            "html_scrape_user_intent": "HTML 解析 (指定 URL) で検出",
            "html_scrape_visual_picker": "HTML 解析 (ビジュアル選択) で検出",
        },
    ),
    "actor_kind": _vocab(
        "actor_kind",
        {"group": "攻撃グループ", "organization": "国家機関", "contractor": "請負業者"},
        canonical=frozenset({"group", "organization", "contractor"}),
    ),
    # Grok セッションの検証/実行状態 (2 field 分)。
    "grok_session_status": _vocab(
        "grok_session_status",
        {"ok": "OK", "session_expired": "セッション失効", "success": "成功", "failed": "失敗"},
    ),
    "jpci_stage": _vocab(
        "jpci_stage",
        {
            "quiet": "静穏",
            "indication": "予兆",
            "targeting": "標的化",
            "intrusion_it": "侵害(IT系)",
            "ot_approach": "制御系接近",
        },
        canonical=frozenset({"quiet", "indication", "targeting", "intrusion_it", "ot_approach"}),
    ),
    "jpci_domain": _vocab(
        "jpci_domain",
        {"ot": "制御系(OT)", "boundary": "境界・監視系", "it": "情報系(IT)", "unknown": "不明"},
        canonical=frozenset({"ot", "boundary", "it", "unknown"}),
    ),
    "jpci_precision": _vocab(
        "jpci_precision",
        {
            "sector_only": "分野のみ",
            "operator_identified": "事業者特定",
            "system_identified": "システム特定",
        },
        canonical=frozenset({"sector_only", "operator_identified", "system_identified"}),
    ),
    "pmesii_role": _vocab(
        "pmesii_role",
        {"home": "自国", "adversary": "敵対", "allied": "同盟", "other": "その他"},
        canonical=frozenset({"home", "adversary", "allied", "other"}),
    ),
    # geo_precision: 地図の精密点レイヤー (Phase 1b 後半) の精度 Tier。SSoT = src.cti.geocoder
    # の GeoPrecision (地図の見た目・ポップアップ文言が分岐する根拠)。生 enum 値の直接表示禁止
    # (UI 表示文言の規約) のため、frontend は必ずこの vocab 経由でラベルを出す。
    "geo_precision": _vocab(
        "geo_precision",
        {
            "facility": "施設",
            "org_hq": "組織本社",
            "city": "市区",
            "admin1": "州・県 (代表点=州内最大人口都市)",
            "country": "国",
            "region": "地域",
        },
        canonical=frozenset(p.value for p in GeoPrecision),
    ),
    "event_date_basis": _vocab(
        "event_date_basis",
        {
            "occurred": "発生",
            "detected": "検知",
            "disclosed": "公表",
            "reported": "報道",
            "relative": "推定",
        },
        canonical=frozenset({"occurred", "detected", "disclosed", "reported", "relative"}),
    ),
    "job_kind": _vocab(
        "job_kind",
        {"pipeline": "パイプライン", "bespoke": "専用", "reactive": "自動"},
        canonical=frozenset({"pipeline", "bespoke", "reactive"}),
    ),
    "job_protection": _vocab(
        "job_protection",
        {"critical": "重要", "important": "通常", "optional": "任意"},
        canonical=frozenset({"critical", "important", "optional"}),
    ),
    "taxonomy_proposal_status": _vocab(
        "taxonomy_proposal_status",
        {"accepted": "採用", "rejected": "却下", "deferred": "保留", "pending": "未処理"},
        canonical=frozenset({"accepted", "rejected", "deferred", "pending"}),
    ),
    # rubric_group: 判定基準エディタのグループ (作業単位)。値の源泉は
    # src/prompts/rubric_guide.GROUPS なので **導出** する (静的複製を作らない —
    # 複製時代に ach_hypothesis で仮説 3 件が欠落し生 ID が UI に漏出した前例)。
    # description を持つため _vocab ヘルパでなく Vocabulary を直接構築する。
    "rubric_group": Vocabulary(
        name="rubric_group",
        items=tuple(
            VocabItem(value=g.id, label=g.label, order=g.order, description=g.description)
            for g in GROUPS
        ),
        canonical=frozenset(g.id for g in GROUPS),
    ),
    # pmesii_axis: PMESII-PT 軸。canonical = taxonomy_normalizer.PMESII_AXES (確定軸)。
    # T 軸は 2026-07-16 に運用から外したが、正規値集合には残っている (過去データが持つ)
    # ため値は保持し deprecated=True で「新規に付けない軸」と表示側に伝える。
    "pmesii_axis": Vocabulary(
        name="pmesii_axis",
        items=(
            VocabItem("P", "政治 (P)", 0),
            VocabItem("M", "軍事 (M)", 1),
            VocabItem("E", "経済 (E)", 2),
            VocabItem("S", "社会 (S)", 3),
            VocabItem("I-infra", "情報インフラ (I-infra)", 4),
            VocabItem("I-cyber", "サイバー (I-cyber)", 5),
            VocabItem("P-env", "物理環境 (P-env)", 6),
            VocabItem("T", "時間 (T)", 7, deprecated=True),
        ),
        canonical=PMESII_AXES,
    ),
}


def _dynamic_vocabularies() -> dict[str, Vocabulary]:
    """config 由来 (B) の語彙を serve 時に構築する。config 編集を配信に反映するため
    static registry に持たず毎回読む (yaml load は軽量、frontend は boot 時 + 無効化時に取得)。
    sector/country のラベルは日本語 (固有名詞で severity ではないため英語化しない)。nation は
    ISO2 なので country vocab を再利用する (別 vocab は作らない・11/18 ラベルギャップも自動解消)。
    """
    from src.cti.taxonomy_normalizer import load_country_display, load_sector_display

    out: dict[str, Vocabulary] = {}
    sector = load_sector_display()
    if sector:
        out["sector"] = _vocab("sector", sector, canonical=frozenset(sector))
    country = load_country_display()
    if country:
        out["country"] = _vocab("country", country, canonical=frozenset(country))
    # actor family (P2-S6, 2026-07-26): 生 id (panda/ransom_group 等) の直接表示を廃し、
    # actor_aliases.yaml の families.label を SSoT として配信 (UI 文言規約の適用)。
    from src.cti.actor_normalizer import load_actor_families

    families = load_actor_families()
    if families:
        out["actor_family"] = _vocab(
            "actor_family",
            {fid: info.get("label") or fid for fid, info in families.items()},
            canonical=frozenset(families),
        )
    return out


def all_vocabularies() -> dict[str, list[dict[str, object]]]:
    """全登録語彙 (static A/B + 動的 config 由来) を配信形式で返す。"""
    merged: dict[str, Vocabulary] = {**_REGISTRY, **_dynamic_vocabularies()}
    return {name: vocab.serialize() for name, vocab in merged.items()}


def get_vocabulary(name: str) -> Vocabulary | None:
    return _REGISTRY.get(name) or _dynamic_vocabularies().get(name)


def registered_names() -> tuple[str, ...]:
    return tuple(_REGISTRY.keys()) + tuple(_dynamic_vocabularies().keys())


def vocabularies_with_canonical() -> tuple[Vocabulary, ...]:
    """canonical (正規値集合) を持つ vocab のみ (強制テスト用)。"""
    return tuple(v for v in _REGISTRY.values() if v.canonical is not None)
