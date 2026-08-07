// widget レジストリ: 追加はここに 1 entry 足すだけ。
// config の動的選択肢 (アクター/国) は loadChoices で snapshot から取得する。

import { api } from "../../api/client";
import { pagesApi } from "../../api/pages";
import { channelsApi } from "../../api/channels";
import { COUNT_CHOICES, type ConfigOption, type WidgetDef } from "./shared";
import { StatusStripWidget, StandingAssessmentWidget, SynthesisSectionWidget } from "./widgets/situational";
import { LatestHeadlinesWidget } from "./widgets/headlines";
import { TopActorsWidget, ActorDossierWidget } from "./widgets/threats";
import { SituationWidget, MiniMapWidget, GeoRankingWidget, GeoTrendWidget } from "./widgets/situation_geo";
import { PirCoverageWidget, PirSpotlightWidget } from "./widgets/pir";
import { HoldingsWidget, SourceContributionWidget } from "./widgets/holdings";
import { DailyPostsTrendWidget, HealthWidget, RecentRunsWidget } from "./widgets/ops";
import { ArticleFeedWidget } from "./widgets/articles";
import { KpiRowWidget, ThreatPictureWidget, NotableActorsWidget, VulnerabilitiesWidget } from "./widgets/overview";
import { JpCiThreatWidget } from "./widgets/jpci";

// ── 共通 config option 定義 ──
const PER_OPTION: ConfigOption = { key: "per", label: "件数", choices: COUNT_CHOICES };
const HEADLINE_AXES: ConfigOption = {
  key: "axis", label: "分類軸",
  choices: [
    { value: "pmesii", label: "PMESII軸" },
    { value: "pir", label: "PIR (ミッション分類)" },
  ],
};
const SECTION_OPTION: ConfigOption = {
  key: "section", label: "セクション",
  choices: [
    { value: "cog", label: "重心" },
    { value: "spillover", label: "波及" },
    { value: "chain", label: "連鎖" },
    { value: "weight", label: "比重" },
    { value: "pir", label: "PIR 評価" },
  ],
};
const PERIOD_OPTION: ConfigOption = {
  key: "period", label: "期間",
  choices: [
    { value: "auto", label: "全体の期間に合わせる (既定)" },
    { value: "daily", label: "日次 固定" },
    { value: "weekly", label: "週次 固定" },
    { value: "monthly", label: "月次 固定" },
  ],
};
const IMPORTANCE_OPTION: ConfigOption = {
  key: "importance", label: "重要度",
  choices: [
    { value: "", label: "すべて" },
    { value: "high", label: "高のみ" },
    { value: "medium", label: "中以上" },
  ],
};
const DAYS_OPTION: ConfigOption = {
  key: "days", label: "日数",
  choices: [
    { value: "7", label: "7日" },
    { value: "14", label: "14日" },
    { value: "30", label: "30日" },
  ],
};
// 概観帯の共有窓に連動する widget 共通の期間 option (⚙ 閲覧設定から個別固定できる)。
// kpi_row は共有窓のコントローラ (チップ) 自体を持つため対象外。
const WINDOW_OPTION: ConfigOption = {
  key: "days", label: "期間",
  choices: [
    { value: "auto", label: "全体の期間に合わせる (既定)" },
    { value: "1", label: "24h 固定" },
    { value: "7", label: "7日 固定" },
    { value: "30", label: "30日 固定" },
    { value: "90", label: "90日 固定" },
  ],
};
// 重要インフラ脅威 widget: 既定は概観の共有窓 (24h/7日/30日) に連動。
// 国家アクター行動は長窓ほど見えるため 90日/全期間の明示指定も残す。
const JPCI_DAYS_OPTION: ConfigOption = {
  key: "days", label: "期間",
  choices: [
    { value: "auto", label: "全体の期間に合わせる (既定)" },
    { value: "30", label: "30日 固定" },
    { value: "90", label: "90日 固定" },
    { value: "0", label: "全期間" },
  ],
};
// 動的: snapshot から国・アクター一覧を取得。
const NATION_OPTION: ConfigOption = {
  key: "nation", label: "国",
  placeholder: { value: "", label: "全て" },
  loadChoices: async () => {
    const snap = await api.snapshot({ time: "30" });
    return snap.nations.map((n) => ({ value: n, label: n.toUpperCase() }));
  },
};
const ACTOR_OPTION: ConfigOption = {
  key: "actor_id", label: "アクター",
  placeholder: { value: "", label: "未選択" },
  loadChoices: async () => {
    const snap = await api.snapshot({ time: "30" });
    return Object.entries(snap.actor_lookup)
      .map(([id, name]) => ({ value: id, label: name }))
      .sort((a, b) => a.label.localeCompare(b.label));
  },
};

// ── 日次推移 (被害報道) widget 用 config ──
const GEO_TREND_GROUP: ConfigOption = {
  key: "group", label: "系列",
  choices: [
    { value: "country", label: "国別" },
    { value: "sector", label: "セクター別" },
  ],
};
const GEO_TREND_MODE: ConfigOption = {
  key: "mode", label: "表示",
  choices: [
    { value: "line", label: "折れ線" },
    { value: "stacked", label: "積み上げ" },
  ],
};

// ── 記事フィード widget 用 config ──
const CATEGORY_OPTION: ConfigOption = {
  key: "category", label: "カテゴリ",
  choices: [
    { value: "", label: "全カテゴリ" },
    { value: "vuln", label: "脆弱性・アドバイザリ" },
    { value: "threat", label: "マルウェア・APT 等" },
    { value: "incident_breach", label: "侵害/インシデント" },
    { value: "vulnerability", label: "脆弱性" },
    { value: "breach", label: "侵害" },
    { value: "malware", label: "マルウェア" },
    { value: "apt", label: "APT" },
    { value: "geopolitical", label: "地政" },
    { value: "policy", label: "サイバー政策" },
    { value: "research", label: "研究" },
    { value: "advisory", label: "アドバイザリ" },
  ],
};
// チャンネルは live registry から動的に取得 (custom / ops も含む、固定マップは stale 化する)。
const CHANNEL_OPTION: ConfigOption = {
  key: "channel", label: "チャンネル",
  placeholder: { value: "", label: "全ch" },
  loadChoices: () =>
    channelsApi.get().then((r) => r.channels.map((c) => ({ value: c.id, label: c.label }))),
};
const MODE_OPTION: ConfigOption = {
  key: "mode", label: "表示",
  choices: [
    { value: "headline", label: "見出しのみ" },
    { value: "summary", label: "要約付き" },
  ],
};
const SINCE_OPTION: ConfigOption = {
  key: "since_hours", label: "期間",
  choices: [
    { value: "0", label: "全期間" },
    { value: "24", label: "24時間" },
    { value: "72", label: "3日" },
    { value: "168", label: "7日" },
  ],
};
const FEED_OPTION: ConfigOption = {
  key: "feed", label: "サイト",
  placeholder: { value: "", label: "全サイト" },
  loadChoices: async () => {
    const subs = await pagesApi.subscriptions();
    return subs.subscriptions
      .map((s) => ({ value: s.title, label: s.title }))
      .sort((a, b) => a.label.localeCompare(b.label));
  },
};
// 記事フィード共通の全 config option (preset は defaultConfig で初期値を固定)。
const ARTICLE_FEED_OPTIONS: ConfigOption[] = [
  CATEGORY_OPTION, FEED_OPTION, CHANNEL_OPTION, IMPORTANCE_OPTION, MODE_OPTION, SINCE_OPTION, PER_OPTION,
];

// span は 4 カラムグリッド基準 (4=全幅 / 2=半分 / 1=¼ / 3=¾)
export const WIDGET_REGISTRY: Record<string, WidgetDef> = {
  // ── 状況認識 ──
  kpi_row: { title: "KPI 概観", Component: KpiRowWidget, defaultSpan: 4, defaultHeight: 330, thumb: "kpi", blurb: "今日の観測(前日比+推移) / 高重要度(前期比) / 重要度の内訳" },
  threat_picture: { title: "脅威の構成と変化", Component: ThreatPictureWidget, defaultSpan: 2, defaultHeight: 420, thumb: "bars", blurb: "戦術=標的セクター / 戦略=敵対国籍・地域 を前期比つき横棒で (二眼)", configOptions: [WINDOW_OPTION] },
  // ── 国家情勢 + 脅威マップ (国家情勢タブ / 脅威マップの凝縮) ──
  situation: { title: "国家情勢", Component: SituationWidget, defaultSpan: 2, defaultHeight: 400, thumb: "bars", blurb: "自国+敵対の 攻撃者/標的/地政学 (国家情勢の凝縮)", configOptions: [WINDOW_OPTION] },
  geo_ranking: { title: "被害国ランキング", Component: GeoRankingWidget, defaultSpan: 1, defaultHeight: 400, thumb: "bars", blurb: "脅威マップと同じ被害国ランキング (セクター構成 + カバレッジ信頼度)", configOptions: [WINDOW_OPTION] },
  geo_trend: { title: "日次推移 (被害報道)", Component: GeoTrendWidget, defaultSpan: 2, defaultHeight: 210, thumb: "trend", blurb: "脅威マップと同じ日次件数推移 (国別/セクター別・折れ線/積み上げ)", configOptions: [WINDOW_OPTION, GEO_TREND_GROUP, GEO_TREND_MODE] },
  mini_map: { title: "脅威マップ (地図)", Component: MiniMapWidget, defaultSpan: 2, defaultHeight: 480, fill: true, thumb: "grid", blurb: "実 Leaflet 地図を小型埋め込み (やや重い)", configOptions: [WINDOW_OPTION] },
  notable_actors: { title: "今動いているアクター", Component: NotableActorsWidget, defaultSpan: 2, defaultHeight: 360, thumb: "sparkline", blurb: "急増・新規アクターを実際の名前で表示", configOptions: [WINDOW_OPTION] },
  vulnerabilities_kev: { title: "重要脆弱性 (KEV)", Component: VulnerabilitiesWidget, defaultSpan: 2, defaultHeight: 360, thumb: "list", blurb: "KEV(実悪用) / 高CVSS の脆弱性を優先表示", configOptions: [WINDOW_OPTION] },
  jp_ci_threat: { title: "重要インフラ脅威", Component: JpCiThreatWidget, defaultSpan: 2, defaultHeight: 480, thumb: "grid", blurb: "重要インフラ脅威の要約 (3つの視点): 事前配置の常設評価 + 横断キャンペーン + 16分野の状況 (日本の観測段階×世界の国家アクター行動)", configOptions: [JPCI_DAYS_OPTION], defaultConfig: { days: "auto" } },
  status_strip: { title: "システム状態", Component: StatusStripWidget, defaultSpan: 4, defaultHeight: 48, thumb: "status", blurb: "pipeline 稼働 + 失敗 + 次回実行" },
  standing_assessment: { title: "現況評価 (synthesis)", Component: StandingAssessmentWidget, defaultSpan: 4, defaultHeight: 140, thumb: "text", blurb: "全体期間に連動した状況総括の見出し (24h=日次/7日=週次/30・90日=月次)", configOptions: [WINDOW_OPTION] },
  synthesis_section: { title: "Synthesis セクション", Component: SynthesisSectionWidget, defaultSpan: 2, defaultHeight: 340, thumb: "text", multi: true, blurb: "重心/波及/連鎖/比重/PIR を選択表示", configOptions: [SECTION_OPTION, PERIOD_OPTION] },
  // ── 過去参照 ──
  latest_headlines: { title: "最新ヘッドライン (カテゴリ別)", Component: LatestHeadlinesWidget, defaultSpan: 4, defaultHeight: 460, thumb: "list", multi: true, blurb: "PMESII軸 / PIR別の最新記事", configOptions: [HEADLINE_AXES, PER_OPTION] },
  // ── 記事フィード (汎用・設定可・複数配置可。カテゴリ/CH/重要度/サイトで絞る) ──
  news_feed: { title: "記事フィード", Component: ArticleFeedWidget, defaultSpan: 2, defaultHeight: 420, thumb: "list", multi: true, blurb: "カテゴリ/CH/重要度/サイトで絞った記事。設定を変えて複数配置 (脆弱性/脅威/地政/緊急 等)", configOptions: ARTICLE_FEED_OPTIONS, defaultConfig: { mode: "summary", per: 5 } },
  // ── 発見支援 / 脅威 ──
  top_actors: { title: "主要アクター", Component: TopActorsWidget, defaultSpan: 2, defaultHeight: 340, thumb: "sparkline", multi: true, blurb: "追跡量 top (国フィルタ可)", configOptions: [NATION_OPTION, PER_OPTION] },
  actor_dossier: { title: "アクター・ドシエ", Component: ActorDossierWidget, defaultSpan: 2, defaultHeight: 380, thumb: "text", multi: true, blurb: "指定アクターの TTP/CVE/IOC", configOptions: [ACTOR_OPTION] },
  // ── PIR ──
  pir_coverage: { title: "PIR 充足 / ギャップ", Component: PirCoverageWidget, defaultSpan: 2, defaultHeight: 320, thumb: "sparkline", blurb: "PIR ごとの直近7日の該当件数" },
  // Spotlight は週次のみ生成のため期間選択肢は持たせない (日次=PIR Daily Focus 別機能 / 月次未生成)。
  pir_spotlight: { title: "PIR Spotlight (週次)", Component: PirSpotlightWidget, defaultSpan: 2, defaultHeight: 360, thumb: "text", blurb: "PIR ごとの週次まとめ" },
  // ── 蓄積 / 運用 ──
  holdings: { title: "Intelligence Holdings", Component: HoldingsWidget, defaultSpan: 2, defaultHeight: 260, thumb: "grid", blurb: "蓄積件数 (記事/アクター/IOC 等)" },
  source_contribution: { title: "ソース貢献度", Component: SourceContributionWidget, defaultSpan: 2, defaultHeight: 320, thumb: "bars", blurb: "投稿数 top feed", configOptions: [PER_OPTION] },
  daily_posts_trend: { title: "日次投稿推移", Component: DailyPostsTrendWidget, defaultSpan: 2, defaultHeight: 260, thumb: "trend", blurb: "投稿数の時系列バー", configOptions: [DAYS_OPTION] },
  health: { title: "死活監視", Component: HealthWidget, defaultSpan: 2, defaultHeight: 180, thumb: "status", blurb: "Ollama/Discord/IMAP 疎通" },
  recent_runs: { title: "直近の実行", Component: RecentRunsWidget, defaultSpan: 2, defaultHeight: 320, thumb: "list", blurb: "最近の run と結果", configOptions: [PER_OPTION] },
};
