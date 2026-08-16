"""プロンプト / 設定ファイルのカタログ (区分・説明の SSoT)。

UI のプロンプト tab / 設定ファイル tab は生ファイル名の羅列で「何をする
ファイルか」が読めない (2026-08-15 利用者指摘)。本モジュールが
**ファイル → 区分 / 表示名 / 説明** の対応を一元管理し、一覧 API が
これを添えて返す (ラベルは backend 配信の単一 SSoT、という既存規約に従う)。

運用規約: prompts/**/*.j2 / config/**/*.yaml を追加する PR は本カタログにも
1 行足すこと (tests/unit/test_file_catalog.py の網羅テストが強制する)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 一覧表示の区分順 (UI はこの順でグループを描画する)
PROMPT_CATEGORY_ORDER: tuple[str, ...] = (
    "ブリーフィング",
    "ダイジェスト",
    "状況総括 (synthesis)",
    "Spotlight",
    "共有パーツ",
    "その他",
)

CONFIG_CATEGORY_ORDER: tuple[str, ...] = (
    "情報源",
    "パイプライン",
    "CTI 辞書・語彙",
    "配信・PIR",
    "プロンプト",
    "その他",
)


@dataclass(frozen=True)
class FileInfo:
    """カタログ 1 エントリ (表示専用メタデータ)。"""

    category: str
    label: str
    description: str


PROMPT_CATALOG: dict[str, FileInfo] = {
    "prompts/briefing/summarizer.j2": FileInfo(
        "ブリーフィング",
        "記事要約・翻訳",
        "daily-briefing の per-article 要約 + 和訳 + 重要度/カテゴリ判定。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の判定基準はプロンプトタブのカード編集)",
    ),
    "prompts/digest/pir_daily_focus.j2": FileInfo(
        "ダイジェスト",
        "PIR Daily Focus",
        "morning-brief の PIR 別 24h 集約 (PIR ごとに要点 1-2 文を生成)",
    ),
    "prompts/digest/weekly_recap.j2": FileInfo(
        "ダイジェスト",
        "週次リキャップ",
        "weekly-recap の週間総括 narrative",
    ),
    "prompts/digest/deep_dive_rubric.j2": FileInfo(
        "ダイジェスト",
        "深掘り選定ルーブリック",
        "週次深掘りダイジェストの対象記事選定基準 (deep_dive_selector)",
    ),
    "prompts/synthesis/status_synthesis.j2": FileInfo(
        "状況総括 (synthesis)",
        "状況総括 narrative",
        "Intel Graph の状況総括レポート生成 (synthesis ティア、1 run 1 呼出)",
    ),
    "prompts/synthesis/nominate.j2": FileInfo(
        "状況総括 (synthesis)",
        "情勢候補の指名",
        "台帳更新の対象とする情勢候補を記事群から指名する",
    ),
    "prompts/synthesis/detect_new.j2": FileInfo(
        "状況総括 (synthesis)",
        "新規情勢の検出",
        "既存台帳に無い新しい情勢の立ち上げを検出する",
    ),
    "prompts/synthesis/ground_ach.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 + ACH (初回)",
        "情勢の初回評価: 証拠接地 → 競合仮説分析 (ACH) → 確度較正",
    ),
    "prompts/synthesis/ground_incremental.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 (増分)",
        "既存情勢への増分 ACH 更新 (新規証拠の取込 + 指標の照会)",
    ),
    "prompts/synthesis/adversarial.j2": FileInfo(
        "状況総括 (synthesis)",
        "対称 adversarial 検証",
        "評価の過確信を防ぐ反対仮説側からの検証パス",
    ),
    "prompts/synthesis/render.j2": FileInfo(
        "状況総括 (synthesis)",
        "narrative 射影",
        "canonical estimate から報告 narrative への射影 (状態中心アーキテクチャの出力側)",
    ),
    "prompts/spotlight/pir_spotlight.j2": FileInfo(
        "Spotlight",
        "PIR Spotlight",
        "PIR 縦断の週次 narrative (headline + key_events + outlook)",
    ),
    "prompts/_persona.j2": FileInfo(
        "共有パーツ",
        "共有ペルソナ (公開版)",
        "読み物系プロンプトが {% include %} する共通ペルソナ文",
    ),
    "prompts/_persona_local.j2": FileInfo(
        "共有パーツ",
        "共有ペルソナ (ローカル上書き)",
        "運用者ローカルのペルソナ上書き (gitignore、存在すれば公開版より優先)",
    ),
}

CONFIG_CATALOG: dict[str, FileInfo] = {
    "config/sources/feeds.yaml": FileInfo(
        "情報源",
        "RSS feed 購読",
        "直接 RSS の購読リスト (初回 seed 専用 — runtime SSoT は DB、UI 編集は購読ソース画面)",
    ),
    "config/sources/watchers.yaml": FileInfo(
        "情報源",
        "sitemap watcher",
        "RSS の無い一次ソースの sitemap 監視 (初回 seed 専用 — runtime SSoT は DB)",
    ),
    "config/sources/scrapers.yaml": FileInfo(
        "情報源",
        "HTML scraper",
        "HTML スクレイパー型ソース (初回 seed 専用 — runtime SSoT は DB)",
    ),
    "config/sources/source_quality.yaml": FileInfo(
        "情報源",
        "ソース品質 (降格・cap)",
        "ソース別の品質補正 (初回 seed 専用 — runtime SSoT は DB)",
    ),
    "config/sources/source_reliability.yaml": FileInfo(
        "情報源",
        "ソース信頼度 (出典バッジ)",
        "出典の信頼度分類 (ICD 203 系バッジの根拠、source_basis が参照)",
    ),
    "config/sources/source_reliability_overrides.yaml": FileInfo(
        "情報源",
        "ソース信頼度の上書き",
        "個別ソースの信頼度を運用判断で上書きする層",
    ),
    "config/pipelines.yaml": FileInfo(
        "パイプライン",
        "パイプライン宣言",
        "実行パイプラインの宣言 (bespoke scraper 含む、git 管理のまま = DB 移行対象外)",
    ),
    "config/cti/actor_aliases.yaml": FileInfo(
        "CTI 辞書・語彙",
        "アクター辞書",
        "脅威アクターの canonical + 別名辞書 (actor_normalizer / リコール層の基盤)",
    ),
    "config/cti/actor_aliases.translated.yaml": FileInfo(
        "CTI 辞書・語彙",
        "アクター辞書 和訳",
        "アクター辞書の説明文 和訳キャッシュ (MITRE 同期の LLM 和訳出力)",
    ),
    "config/cti/malware_aliases.yaml": FileInfo(
        "CTI 辞書・語彙",
        "マルウェア別名辞書",
        "マルウェア/ツール名の canonical + 別名",
    ),
    "config/cti/tag_registry.yaml": FileInfo(
        "CTI 辞書・語彙",
        "言及タグ レジストリ",
        "言及タグ層の entity 種別定義 (新 entity_type はここ + テストの両方に追加)",
    ),
    "config/cti/countries.yaml": FileInfo(
        "CTI 辞書・語彙",
        "国語彙 (victim 国)",
        "被害国の正規化語彙 (diamond_model / geo map が参照する SSoT)",
    ),
    "config/cti/victim_sectors.yaml": FileInfo(
        "CTI 辞書・語彙",
        "被害セクタ語彙",
        "被害組織セクタの正規化語彙 (sector ラベルの SSoT)",
    ),
    "config/cti/pmesii_default_mapping.yaml": FileInfo(
        "CTI 辞書・語彙",
        "PMESII 既定マッピング",
        "カテゴリ → PMESII 軸の既定対応 (taxonomy_normalizer が参照)",
    ),
    "config/delivery/routing_rules.yaml": FileInfo(
        "配信・PIR",
        "配信ルール",
        "チャンネル配信ルール (初回 seed 専用 — runtime SSoT は DB、UI 編集は情報フロー画面)",
    ),
    "config/delivery/pir.yaml": FileInfo(
        "配信・PIR",
        "PIR (優先情報要件)",
        "PIR 定義 (初回 seed 専用 — runtime SSoT は DB、UI 編集は PIR 画面)",
    ),
    "config/prompts/summarizer_rubric.yaml": FileInfo(
        "プロンプト",
        "記事要約の判定基準",
        "summarizer の判定基準 (初回 seed 専用 — runtime SSoT は DB、UI 編集はプロンプトタブ)",
    ),
}

_FALLBACK_CATEGORY = "その他"


def annotate_files(
    files: list[str], catalog: dict[str, FileInfo], category_order: tuple[str, ...]
) -> list[dict[str, Any]]:
    """ファイル一覧を区分ごとのグループ構造に整形する (UI 表示用)。

    カタログ未登録のファイルは「その他」区分にファイル名のまま出す
    (新ファイルが UI から消える事故を防ぐ。登録漏れは網羅テストが検出する)。
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in files:
        info = catalog.get(path)
        category = info.category if info else _FALLBACK_CATEGORY
        entry: dict[str, Any] = {
            "path": path,
            "label": info.label if info else path.rsplit("/", 1)[-1],
            "description": info.description if info else "",
        }
        grouped.setdefault(category, []).append(entry)
    ordered = [c for c in category_order if c in grouped]
    # category_order 外の区分も落とさない (定義ミス時の可視性優先)
    ordered.extend(c for c in grouped if c not in ordered)
    return [{"category": c, "files": grouped[c]} for c in ordered]
