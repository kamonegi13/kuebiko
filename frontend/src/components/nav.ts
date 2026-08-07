// 左サイドバー / TopBar 共通のナビ構成 (single source of truth)。
// IA: 概観 / インテリジェンス / コンテンツ / 運用 / 設定 の 5 グループ。
// Intel Graph の 4 タブ (synthesis/pmesii/threats/operations) は第一級項目へ昇格。

import {
  BookOpen, Bookmark, CalendarClock, ClipboardCheck, Crosshair, FileText,
  Flag, History, LayoutDashboard, Map, MessageSquareText, Newspaper, Rss, Scale, Settings, ShieldAlert,
  TrendingUp, Users, Workflow,
} from "lucide-react";

// lucide の LucideIcon 型は公開 export されていないため icon 値から型を取り出す
// (全 icon が同一の ForwardRefExoticComponent 型)。
export type IconComponent = typeof LayoutDashboard;

export interface NavLink {
  href: string;
  label: string;
  Icon: IconComponent;
  // active 判定: exact 一致 or これらの prefix で前方一致
  exact?: string[];
  prefixes?: string[];
  // full instance 専用 (主目的が編集/操作のページ)。readonly instance (Cloudflare 公開)
  // では write が全て 403 のため、メニューに出さない (機能しない項目を見せない)。
  fullOnly?: boolean;
}

export interface NavGroup {
  title: string;
  items: NavLink[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    title: "概観",
    items: [
      { href: "/app", label: "ダッシュボード", Icon: LayoutDashboard, exact: ["/app", "/app/"], prefixes: ["/app/dashboard"] },
    ],
  },
  {
    title: "インテリジェンス",
    items: [
      { href: "/app/intel/synthesis", label: "現況", Icon: FileText, exact: ["/app/intel", "/app/intel/"], prefixes: ["/app/intel/synthesis"] },
      { href: "/app/intel/pmesii", label: "国家情勢", Icon: Scale, prefixes: ["/app/intel/pmesii"] },
      { href: "/app/intel/threats", label: "脅威アクター", Icon: Crosshair, prefixes: ["/app/intel/threats"] },
      { href: "/app/map", label: "脅威マップ", Icon: Map, prefixes: ["/app/map"] },
      { href: "/app/jpci", label: "重要インフラ脅威", Icon: ShieldAlert, prefixes: ["/app/jpci"] },
      { href: "/app/intel/forecast", label: "将来予測", Icon: TrendingUp, prefixes: ["/app/intel/forecast"] },
      // 振り返り (過去参照) はブリーフページの週次ビューに統合 (2026-07-25 時間軸統合)
      { href: "/app/pir", label: "PIR / Spotlight", Icon: Flag, prefixes: ["/app/pir"] },
      // 分析チャット (2026-07-12): 自然言語でデータ照会 → 簡易レポート。
      // read-only ツールのみのため readonly instance でも利用可 (2026-07-19 allowlist 化)
      { href: "/app/assistant", label: "分析チャット", Icon: MessageSquareText, prefixes: ["/app/assistant"] },
    ],
  },
  {
    title: "コンテンツ",
    items: [
      // 日次ブリーフ = 完成した配信物 (朝刊/夕刊) を読むページ。分析サーフェスではなく
      // コンテンツ (2026-07-12 ユーザー指摘で インテリジェンス → コンテンツ へ移動)。
      { href: "/app/daily-brief", label: "ブリーフ・振り返り", Icon: BookOpen, prefixes: ["/app/daily-brief", "/app/retrospect"] },
      { href: "/app/news", label: "ニュース・検索", Icon: Newspaper, prefixes: ["/app/news", "/app/search", "/app/pivot"] },
      { href: "/app/notes", label: "ブックマーク・メモ", Icon: Bookmark, prefixes: ["/app/notes"] },
      { href: "/app/subscriptions", label: "購読ソース", Icon: Rss, prefixes: ["/app/subscriptions"] },
      { href: "/app/actors", label: "アクター辞書", Icon: Users, prefixes: ["/app/actors"] },
    ],
  },
  {
    title: "運用",
    items: [
      // 死活監視ページは廃止 (設定・死活の画面統合 P4) — ダッシュボード widget と
      // 各対象画面 (情報フロー/購読ソース/モデルタブ) に統合。
      { href: "/app/schedule", fullOnly: true, label: "ジョブ管理", Icon: CalendarClock, prefixes: ["/app/schedule", "/app/runs"] },
      // 旧「履歴・インシデント」(2026-07-25 改名+移動): 実体はパイプライン処理ログ
      // (失敗/重複含む全記事 + run 監査導線) = 運用サーフェス。読む用途はニュース・検索へ。
      { href: "/app/history", label: "取込・処理履歴", Icon: History, prefixes: ["/app/history", "/app/run/"] },
      { href: "/app/intel/operations", fullOnly: true, label: "運用レビュー", Icon: ClipboardCheck, prefixes: ["/app/intel/operations"] },
    ],
  },
  {
    title: "設定",
    items: [
      // ナビ整理 (2026-07-26): マッチリスト・設定変更履歴は設定のタブへ統合、
      // STIX エクスポート (空一覧の死にページ) は廃止 (単記事 STIX は記事詳細のボタン)。
      // 新ページ追加の規約: グループ動詞との語彙一致・専用ページに足る厚み/頻度・
      // 実体が生きている、の 3 基準を満たさなければ既存ページのタブ/カードにする。
      { href: "/app/config", fullOnly: true, label: "設定", Icon: Settings, exact: ["/app/config"], prefixes: ["/app/prompts", "/app/config-history"] },
      // 配信ルール / チャンネルの編集・プレビューは情報フローに完全内蔵 (専用ページ廃止)。
      { href: "/app/flow", fullOnly: true, label: "情報フロー", Icon: Workflow, prefixes: ["/app/flow", "/app/match-lists"] },
    ],
  },
];

export const NAV_FLAT: NavLink[] = NAV_GROUPS.flatMap((g) => g.items);

// readonly instance (Cloudflare 公開) 向け: fullOnly 項目を除いた nav。
// hideFullOnly の判定は useRuntimeFlags.shouldHideFullOnly (readonly かつ未認証) が持つ。
// 認証済み (Tier1) では閲覧できるためメニューにも出す。
export function visibleNavGroups(hideFullOnly: boolean): NavGroup[] {
  if (!hideFullOnly) return NAV_GROUPS;
  return NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter((it) => !it.fullOnly) })).filter(
    (g) => g.items.length > 0,
  );
}

export function visibleNavFlat(hideFullOnly: boolean): NavLink[] {
  return visibleNavGroups(hideFullOnly).flatMap((g) => g.items);
}

// readonly instance で遮断する fullOnly ページの path 判定 (App.tsx のルートガード用)。
// メニュー非表示 (visibleNavGroups) と同じ fullOnly 宣言を SSoT とし、直 URL でも
// ページを描画しない。API 側はサーバの _READ_ONLY_GET_DENYLIST が 403 で防御の実体。
export function isFullOnlyPath(pathname: string): boolean {
  return NAV_FLAT.some((it) => it.fullOnly && isActive(it, pathname));
}

// モバイル ボトムタブバー: 高頻度の 4 項目 + 「メニュー」(全 nav を drawer で開く)。
// 「メニュー」は href なし (onOpenMenu コールバックで sidebar drawer を開く)。
export const BOTTOM_NAV: NavLink[] = [
  { href: "/app", label: "ホーム", Icon: LayoutDashboard, exact: ["/app", "/app/"], prefixes: ["/app/dashboard"] },
  { href: "/app/news", label: "ニュース・検索", Icon: Newspaper, prefixes: ["/app/news", "/app/search", "/app/pivot"] },
  { href: "/app/intel/pmesii", label: "情勢", Icon: Scale, prefixes: ["/app/intel"] },
  { href: "/app/map", label: "マップ", Icon: Map, prefixes: ["/app/map"] },
];

function normalize(p: string): string {
  return p.length > 1 ? p.replace(/\/$/, "") : p;
}

export function isActive(item: NavLink, pathname: string): boolean {
  const p = normalize(pathname);
  if (item.exact?.some((e) => normalize(e) === p)) return true;
  if (item.prefixes?.some((pre) => p === normalize(pre) || p.startsWith(`${normalize(pre)}/`))) return true;
  return false;
}

// 現在地に対応する nav item (TopBar の現在地ラベル用)。
export function findActive(pathname: string): NavLink | undefined {
  return NAV_FLAT.find((it) => isActive(it, pathname));
}

// 現在地の (グループ見出し, item)。breadcrumb 用。
export function findActiveWithGroup(pathname: string): { group: string; item: NavLink } | undefined {
  for (const g of NAV_GROUPS) {
    const item = g.items.find((it) => isActive(it, pathname));
    if (item) return { group: g.title, item };
  }
  return undefined;
}
