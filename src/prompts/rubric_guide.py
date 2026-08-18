"""判定基準エディタの **表示メタ** (グループ分け・効く先・根拠) の SSoT。

``SummarizerRubric`` は「LLM に verbatim で渡る本文」の SSoT であり、グループや説明文の
ような表示メタは持たせない。混ぜると分類を変えるたびに ``config_store`` の版が増え、
``/api/v1/config-history/summarizer_rubric`` の diff が「プロンプトが変わった」ように
見える (監査ノイズ) 上、既存 DB 値への migration も必要になる。表示メタは backend の
このモジュールが持ち、GET 応答の ``guide`` キーで配信する (DB 変更ゼロ)。

グループの **日本語ラベル / 説明** はここではなく語彙レジストリ (``rubric_group``) が
配信する — frontend に value→label の静的マップを作らないため (docs
vocabulary_label_architecture §4.2-4.3)。``src/vocab/registry.py`` が ``GROUPS`` を
読んでラベルを組み立てるので、**本モジュールは src 配下の他モジュールを import しない**
(import cycle を作らない・stdlib のみに依存する)。

``FIELD_GUIDE`` の網羅は ``tests/unit/test_rubric_guide.py`` が ``SummaryOutput`` と
突合して機械強制する (``fill_rate_audit.METRICS`` と同じ「新フィールド追加 PR は 1 行
足す」規約)。忘れても UI からフィールドが消えないよう ``resolve_group`` は ``other`` に
fail-open し、``guide_payload`` が ``unmapped_fields`` として申告する。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# 未登録フィールドの受け皿 (fail-open)。定義漏れでも UI からカードを消さない。
OTHER_GROUP_ID = "other"

# 未登録フィールドの表示順。既知グループ (1..N) の後ろへ回す。
UNMAPPED_ORDER = 99


@dataclass(frozen=True)
class RubricGroup:
    """判定基準カードのグループ (作業単位)。"""

    id: str
    label: str
    description: str  # UI のグループ見出し下に出る 1 行
    order: int


@dataclass(frozen=True)
class FieldGuide:
    """1 フィールドの表示メタ。"""

    field_id: str
    group: str
    order: int  # グループ内の表示順 (1-based)
    effect: str  # 「効く先」1 行
    sources: tuple[str, ...]  # 根拠となる消費者コードのパス (repo 相対)


# ---------------------------------------------------------------------------
# グループ定義
# ---------------------------------------------------------------------------
# 表示順の根拠: 利用者のループは「ある記事の判定がおかしい → 原因の基準を見つける」。
# 誤判定の一次容疑は常に「重要度が違う / 変なチャンネルに来た」なので classification →
# routing を隣接させ、以降は地図・ピボット・文面という「下流の見え方」の順、最後に
# 触っても効かない suppressed を置く。
GROUPS: tuple[RubricGroup, ...] = (
    RubricGroup(
        id="classification",
        label="分類の判定",
        description="この記事が何であり、どれだけ重要か。ほぼ全ての下流分岐の起点です。",
        order=1,
    ),
    # 「配信・可視化の制御」群は 2026-08-18 に消滅した (routing_flags / pmesii_axes が
    # ともに suppressed へ移動し、所属フィールドが 0 になったため)。空の見出しを
    # UI に残さないよう定義ごと削除する。復活させるなら order を詰め直すこと。
    RubricGroup(
        id="victim",
        label="被害の帰属",
        description="誰が・どこでやられたか。脅威マップと国別 KPI の原料です。",
        order=2,
    ),
    RubricGroup(
        id="technical",
        label="技術指標の抽出",
        description="何を使って攻撃したか。記事横断のピボットに使われます。",
        order=3,
    ),
    RubricGroup(
        id="narrative",
        label="読み物の生成",
        description="人が読む文章。文字数と文体の基準です。",
        order=4,
    ),
    RubricGroup(
        id="suppressed",
        label="出力させない",
        description=(
            "summarizer には出力させず、取込時の分類器が確定します。"
            "編集しても本文が空ならプロンプトに出ません。"
        ),
        order=5,
    ),
    RubricGroup(
        id=OTHER_GROUP_ID,
        label="未分類",
        description="グループ定義に登録されていないフィールドです (定義漏れの可能性)。",
        order=UNMAPPED_ORDER,
    ),
)


# ---------------------------------------------------------------------------
# フィールド定義 (24 件 — SummaryOutput の全フィールド)
# ---------------------------------------------------------------------------
# 宣言順 = 表示順 (グループ順 × グループ内 order)。``sources`` は unit テストが
# ``Path.exists()`` で実在検証するため、消費者を消したら赤くなる (根拠が腐らない)。
_FIELD_GUIDES: tuple[FieldGuide, ...] = (
    # ---- classification: 何であり、どれだけ重要か ----
    FieldGuide(
        field_id="category",
        group="classification",
        order=1,
        effect=(
            "配信ルールの条件・記事一覧の絞り込み・PIR 照合・週次まとめの母集団。"
            "ほぼ全ての下流分岐の起点です。"
        ),
        sources=(
            "src/cti/routing_rules.py",
            "src/pir/signal_match.py",
            "src/ui/api/articles_feed.py",
        ),
    ),
    FieldGuide(
        field_id="importance",
        group="classification",
        order=2,
        effect=(
            "配信ルールの優先度判定と、ダッシュボード・PIR の KPI 集計。"
            "脆弱性・アドバイザリの high は悪用の裏付けが無いと medium へ自動降格されます。"
        ),
        sources=(
            "src/cti/routing_rules.py",
            "src/pipeline/summary.py",
            "src/ui/services/overview.py",
        ),
    ),
    # ---- victim: 誰が・どこでやられたか ----
    FieldGuide(
        field_id="victim_country",
        group="victim",
        order=1,
        effect="脅威マップの国バブル、日本標的 KPI、国別の絞り込み。",
        sources=(
            "src/cti/geocoder.py",
            "src/cti/japan_relevance.py",
            "src/ui/api/geo.py",
        ),
    ),
    FieldGuide(
        field_id="victim_sector",
        group="victim",
        order=2,
        effect="被害セクター別の集計と、日本重要インフラ ボードの分野判定。",
        sources=(
            "src/cti/taxonomy_normalizer.py",
            "src/ui/services/jp_ci_board.py",
            "src/ui/services/threat_operations.py",
        ),
    ),
    FieldGuide(
        field_id="victim_city",
        group="victim",
        order=3,
        effect="脅威マップの都市レベルのプロット。明示が無いのに埋めると地図に偽の点が出ます。",
        sources=("src/cti/geocoder.py", "src/pipeline/persistence.py"),
    ),
    FieldGuide(
        field_id="victim_orgs",
        group="victim",
        order=4,
        effect="脅威マップの組織本社プロットと、被害組織の一覧。ベンダを混ぜると地図に誤った点が出ます。",
        sources=("src/cti/geocoder.py", "src/pipeline/persistence.py"),
    ),
    FieldGuide(
        field_id="involved_countries",
        group="victim",
        order=5,
        effect="地政学事象の当事国。サイバー事案と地政学を「国」で突き合わせる情勢把握の結合キーです。",
        sources=(
            "src/pipeline/persistence.py",
            "src/ui/services/situation.py",
            "src/cti/nation_gazetteer.py",
        ),
    ),
    # ---- technical: 何を使って攻撃したか ----
    FieldGuide(
        field_id="iocs",
        group="technical",
        order=1,
        effect="Discord 投稿の IoC 欄と、CVE / IP / ドメイン別の記事横断ピボット。",
        sources=(
            "src/pipeline/persistence.py",
            "src/tools/discord_publisher.py",
            "src/ui/api/articles_feed.py",
        ),
    ),
    FieldGuide(
        field_id="mitre_techniques",
        group="technical",
        order=2,
        effect="Discord 投稿の ATT&CK 欄と、アクター プロファイルの TTP 履歴。",
        sources=(
            "src/tools/discord_publisher.py",
            "src/storage/repo_actor_profile.py",
            "src/pipeline/persistence.py",
        ),
    ),
    FieldGuide(
        field_id="malware_families",
        group="technical",
        order=3,
        effect=(
            "マルウェア別のピボットとアクター プロファイル。"
            "正規化辞書に無い名前は保存時に落ちます。"
        ),
        sources=(
            "src/cti/malware_normalizer.py",
            "src/storage/repo_actor_profile.py",
            "src/pipeline/persistence.py",
        ),
    ),
    FieldGuide(
        field_id="tools",
        group="technical",
        order=4,
        effect="攻撃ツール別のピボット。兵器名や一般 SaaS を混ぜると Capability 分析が汚れます。",
        sources=("src/cti/malware_normalizer.py", "src/pipeline/persistence.py"),
    ),
    # ---- narrative: 人が読む文章 ----
    FieldGuide(
        field_id="title_ja",
        group="narrative",
        order=1,
        effect=(
            "Discord 投稿の見出しと、記事一覧・記事詳細のタイトル。"
            "原題が日本語ならそのまま使われ、原文に無い英字が混じると原題へ差し戻されます。"
        ),
        sources=(
            "src/pipeline/briefing.py",
            "src/pipeline/summary.py",
            "src/tools/discord_publisher.py",
        ),
    ),
    FieldGuide(
        field_id="summary",
        group="narrative",
        order=2,
        effect="Discord 投稿の本文と、記事一覧・週次まとめ・状況総括が読む要約テキスト。",
        sources=(
            "src/tools/discord_publisher.py",
            "src/digest/high_threat_digest.py",
            "src/synthesis/generator.py",
        ),
    ),
    FieldGuide(
        field_id="analyst_note",
        group="narrative",
        order=3,
        effect="Discord 投稿と記事詳細の「所見」欄。本文に無い判断を担う唯一の欄です。",
        sources=("src/tools/discord_publisher.py", "src/pipeline/publish.py"),
    ),
    FieldGuide(
        field_id="remediation",
        group="narrative",
        order=4,
        effect="記事詳細の「対処」欄。読んで何をするかの行動可能性を担う唯一の欄です。",
        sources=("src/ui/api/articles_feed.py", "src/pipeline/summary.py"),
    ),
    # ---- suppressed: summarizer には出させない (取込時の分類器が確定) ----
    FieldGuide(
        field_id="editorial_stance",
        group="suppressed",
        order=1,
        effect=(
            "(出力させない) 記事の論調。取込時の分類器が確定し、プロパガンダの降格に使われます。"
        ),
        sources=("src/cti/llm_routing_flags.py", "src/ui/services/fill_rate_audit.py"),
    ),
    FieldGuide(
        field_id="bluf",
        group="suppressed",
        order=2,
        effect=(
            "(出力させない) RSS 経路ではタイトルと重複するため生成させていません。"
            "Grok 経路のみ機械生成します。"
        ),
        sources=("src/pipeline/briefing.py", "src/grok/jsonl_to_briefings.py"),
    ),
    FieldGuide(
        field_id="diamond",
        group="suppressed",
        order=3,
        effect="(出力させない) Diamond Model の意図・技術軸。取込時の分析軸分類器が確定します。",
        sources=("src/cti/diamond_model.py", "src/cti/analysis_axes_classifier.py"),
    ),
    FieldGuide(
        field_id="event_date",
        group="suppressed",
        order=4,
        effect=(
            "(出力させない) 事象の実発生日。報道時刻・取得時刻と分離した時間軸の基準になります。"
        ),
        sources=("src/cti/analysis_axes_classifier.py", "src/pipeline/summary.py"),
    ),
    FieldGuide(
        field_id="event_date_basis",
        group="suppressed",
        order=5,
        effect=(
            "(出力させない) 発生日が何を指すか (発生 / 検知 / 公表 / 報道 / 推定)。"
            "日付の正直さを担保します。"
        ),
        sources=("src/cti/analysis_axes_classifier.py", "src/pipeline/summary.py"),
    ),
    FieldGuide(
        field_id="compromise_date",
        group="suppressed",
        order=6,
        effect="(出力させない) 初期侵害日。event_date との差が滞留時間 (dwell) になります。",
        sources=("src/cti/analysis_axes_classifier.py", "src/pipeline/summary.py"),
    ),
    FieldGuide(
        field_id="article_type",
        group="suppressed",
        order=7,
        effect=(
            "(出力させない) 記事の種別。まとめ・チュートリアルを配信から外す判定に使われますが、"
            "確定するのは取込時の judgment 分類器です。"
        ),
        sources=(
            "src/cti/routing_rules.py",
            "src/pipeline/briefing.py",
            "src/ui/api/articles_feed.py",
        ),
    ),
    FieldGuide(
        field_id="routing_flags",
        group="suppressed",
        order=8,
        effect=(
            "(出力させない) 配信の補助フラグ。dedup_key はタイトルから自動生成、"
            "主要アクターと確度は judgment 分類器、日本標的と速報性は決定論判定が確定します。"
        ),
        sources=(
            "src/cti/routing_signals.py",
            "src/cti/dedup_key.py",
            "src/pipeline/briefing.py",
        ),
    ),
    FieldGuide(
        field_id="pmesii_axes",
        group="suppressed",
        order=9,
        effect=(
            "(出力させない) PMESII 戦略レンズの軸。category / feed の既定マッピングと"
            "judgment の重要インフラ判定が確定します。"
        ),
        sources=(
            "src/cti/taxonomy_normalizer.py",
            "config/cti/pmesii_default_mapping.yaml",
            "src/ui/services/situation.py",
        ),
    ),
)

FIELD_GUIDE: dict[str, FieldGuide] = {guide.field_id: guide for guide in _FIELD_GUIDES}


def resolve_group(field_id: str, kind: str) -> str:
    """フィールドの表示グループを決める。

    ``kind`` を最優先にするのは「本文が空 → プロンプトに出ない」という ``compose()`` の
    実挙動と表示を一致させるため。将来ユーザが suppressed のフィールドに本文を書いても
    グループは ``suppressed`` に留まり、カード内で「本文があるため出力されます」と注記する
    (グループが動くより注記の方が驚きが小さい)。

    未登録フィールドは ``other`` に fail-open する — 定義漏れで UI からカードが消える方が、
    未分類として出続けるより危険なため。
    """
    if kind == "suppressed":
        return "suppressed"
    guide = FIELD_GUIDE.get(field_id)
    return guide.group if guide is not None else OTHER_GROUP_ID


def guide_payload(
    section_ids: Sequence[str],
    kinds: Mapping[str, str],
) -> dict[str, object]:
    """GET /rubric の ``guide`` キーを組み立てる。

    ``fields`` は **rubric に現存するセクションぶんだけ** 返す (契約全体ではない)。
    グループの label / description / 表示順は載せない — ``rubric_group`` 語彙が SSoT。
    """
    fields: list[dict[str, object]] = []
    unmapped: list[str] = []
    for field_id in dict.fromkeys(section_ids):  # 重複宣言は 1 件に畳む (順序は保持)
        guide = FIELD_GUIDE.get(field_id)
        if guide is None:
            unmapped.append(field_id)
        fields.append(
            {
                "field_id": field_id,
                "group": resolve_group(field_id, kinds.get(field_id, "rubric")),
                "order": guide.order if guide is not None else UNMAPPED_ORDER,
                "effect": guide.effect if guide is not None else "",
                "sources": list(guide.sources) if guide is not None else [],
            },
        )
    return {"fields": fields, "unmapped_fields": unmapped}
