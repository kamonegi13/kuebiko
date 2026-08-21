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
    "prompts/synthesis/status_synthesis_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "状況総括の骨格 (code 所有)",
        "status_synthesis の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 2 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/digest/pir_daily_focus.j2": FileInfo(
        "ダイジェスト",
        "PIR Daily Focus",
        "morning-brief の PIR 別 24h 集約 (PIR ごとに要点 1-2 文を生成)。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 3 本目 2026-08-20)",
    ),
    "prompts/digest/pir_daily_focus_skeleton.j2": FileInfo(
        "ダイジェスト",
        "PIR Daily Focus の骨格 (code 所有)",
        "pir_daily_focus の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 3 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/digest/weekly_recap.j2": FileInfo(
        "ダイジェスト",
        "週次リキャップ",
        "weekly-recap の週間総括 narrative。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 3 本目 2026-08-20)",
    ),
    "prompts/digest/weekly_recap_skeleton.j2": FileInfo(
        "ダイジェスト",
        "週次リキャップの骨格 (code 所有)",
        "weekly_recap の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 3 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/digest/deep_dive_rubric.j2": FileInfo(
        "ダイジェスト",
        "深掘り選定ルーブリック",
        "週次深掘りダイジェストの対象記事選定基準 (deep_dive_selector)。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 3 本目 2026-08-20)",
    ),
    "prompts/digest/deep_dive_rubric_skeleton.j2": FileInfo(
        "ダイジェスト",
        "深掘り選定ルーブリックの骨格 (code 所有)",
        "deep_dive_rubric の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 3 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/status_synthesis.j2": FileInfo(
        "状況総括 (synthesis)",
        "状況総括 narrative",
        "Intel Graph の状況総括レポート生成 (synthesis ティア、1 run 1 呼出)",
    ),
    "prompts/synthesis/nominate.j2": FileInfo(
        "状況総括 (synthesis)",
        "情勢候補の指名",
        "台帳更新の対象とする情勢候補を記事群から指名する。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/nominate_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "情勢候補の指名の骨格 (code 所有)",
        "nominate の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 7〜12 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/detect_new.j2": FileInfo(
        "状況総括 (synthesis)",
        "新規情勢の検出",
        "既存台帳に無い新しい情勢の立ち上げを検出する。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/detect_new_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "新規情勢の検出の骨格 (code 所有)",
        "detect_new の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 7〜12 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/ground_ach.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 + ACH (初回)",
        "情勢の初回評価: 証拠接地 → 競合仮説分析 (ACH) → 確度較正。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/ground_ach_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 + ACH (初回) の骨格 (code 所有)",
        "ground_ach の Jinja データ注入部 (ソース本文・仮説メニューのループ) + block マーカー。"
        "指示散文 (blocks) は DB 所有でプロンプトタブから編集 (層分け 7〜12 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/ground_incremental.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 (増分)",
        "既存情勢への増分 ACH 更新 (新規証拠の取込 + 指標の照会)。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/ground_incremental_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "証拠接地 (増分) の骨格 (code 所有)",
        "ground_incremental の Jinja データ注入部 (前回判定・新着ソース・仮説メニューのループ) + "
        "block マーカー。指示散文 (blocks) は DB 所有でプロンプトタブから編集 "
        "(層分け 7〜12 本目 2026-08-20)。seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/adversarial.j2": FileInfo(
        "状況総括 (synthesis)",
        "対称 adversarial 検証",
        "評価の過確信を防ぐ反対仮説側からの検証パス。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/adversarial_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "対称 adversarial 検証の骨格 (code 所有)",
        "adversarial の Jinja データ注入部 (judgments/evidence のループ) + block マーカー。"
        "指示散文 (blocks) は DB 所有でプロンプトタブから編集 (層分け 7〜12 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/synthesis/render.j2": FileInfo(
        "状況総括 (synthesis)",
        "narrative 射影",
        "canonical estimate から報告 narrative への射影 (状態中心アーキテクチャの出力側)。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集 (プロンプト id は synthesis_render)、"
        "層分け 7〜12 本目 2026-08-20)",
    ),
    "prompts/synthesis/render_skeleton.j2": FileInfo(
        "状況総括 (synthesis)",
        "narrative 射影の骨格 (code 所有)",
        "render (プロンプト id: synthesis_render) の Jinja データ注入部 "
        "(moved/standing/pir_rollup/relation_lines のループ、headline_mode 分岐) + block マーカー。"
        "指示散文 (blocks) は DB 所有でプロンプトタブから編集 (層分け 7〜12 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
    ),
    "prompts/spotlight/pir_spotlight.j2": FileInfo(
        "Spotlight",
        "PIR Spotlight",
        "PIR 縦断の週次 narrative (headline + key_events + outlook)。"
        "**構造化編集が有効なため本ファイルは rollback 用の据置コピー** "
        "(実際の指示散文はプロンプトタブの骨格 + block 編集、層分け 3 本目 2026-08-20)",
    ),
    "prompts/spotlight/pir_spotlight_skeleton.j2": FileInfo(
        "Spotlight",
        "PIR Spotlight の骨格 (code 所有)",
        "pir_spotlight の Jinja データ注入部 + block マーカー。指示散文 (blocks) は "
        "DB 所有でプロンプトタブから編集 (層分け 3 本目 2026-08-20)。"
        "seed 合成 = legacy .j2 と byte 一致が golden 不変量",
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
    "config/prompts/status_synthesis_rubric.yaml": FileInfo(
        "プロンプト",
        "状況総括の編集層 (blocks)",
        "status_synthesis の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 2 本目 2026-08-20)",
    ),
    "config/prompts/weekly_recap_rubric.yaml": FileInfo(
        "プロンプト",
        "週次リキャップの編集層 (blocks)",
        "weekly_recap の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 3 本目 2026-08-20)",
    ),
    "config/prompts/pir_daily_focus_rubric.yaml": FileInfo(
        "プロンプト",
        "PIR Daily Focus の編集層 (blocks)",
        "pir_daily_focus の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 3 本目 2026-08-20)",
    ),
    "config/prompts/deep_dive_rubric.yaml": FileInfo(
        "プロンプト",
        "深掘り選定ルーブリックの編集層 (blocks)",
        "deep_dive_rubric の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 3 本目 2026-08-20)",
    ),
    "config/prompts/pir_spotlight_rubric.yaml": FileInfo(
        "プロンプト",
        "PIR Spotlight の編集層 (blocks)",
        "pir_spotlight の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 3 本目 2026-08-20)",
    ),
    "config/prompts/ground_ach_rubric.yaml": FileInfo(
        "プロンプト",
        "証拠接地 + ACH (初回) の編集層 (blocks)",
        "ground_ach の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/ground_incremental_rubric.yaml": FileInfo(
        "プロンプト",
        "証拠接地 (増分) の編集層 (blocks)",
        "ground_incremental の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/nominate_rubric.yaml": FileInfo(
        "プロンプト",
        "情勢候補の指名の編集層 (blocks)",
        "nominate の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/detect_new_rubric.yaml": FileInfo(
        "プロンプト",
        "新規情勢の検出の編集層 (blocks)",
        "detect_new の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/adversarial_rubric.yaml": FileInfo(
        "プロンプト",
        "対称 adversarial 検証の編集層 (blocks)",
        "adversarial の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/synthesis_render_rubric.yaml": FileInfo(
        "プロンプト",
        "narrative 射影の編集層 (blocks)",
        "render (プロンプト id: synthesis_render) の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。層分け 7〜12 本目 2026-08-20)",
    ),
    "config/prompts/ioc_llm_verifier_rubric.yaml": FileInfo(
        "プロンプト",
        "IoC LLM 検証の編集層 (blocks)",
        "ioc_llm_verifier の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。Python コードが構造を所有する動的プロンプト "
        "= group 4 の 1 本目、2026-08-21)",
    ),
    "config/prompts/judgment_rubric.yaml": FileInfo(
        "プロンプト",
        "統合判断分類器の編集層 (blocks)",
        "judgment_classifier の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。Python コードが構造を所有する動的プロンプト "
        "= group 4 の 2 本目、2026-08-21)",
    ),
    "config/prompts/triage_rubric.yaml": FileInfo(
        "プロンプト",
        "記事 triage (重要度判定) の編集層 (blocks)",
        "article_triage の PIR-driven 経路の指示散文 (初回 seed 専用 — runtime SSoT は DB、"
        "UI 編集はプロンプトタブ。high/medium 判定基準は PIR レイヤの動的注入のため対象外。"
        "Python コードが構造を所有する動的プロンプト = group 4 の 3 本目・最終、2026-08-21)",
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
