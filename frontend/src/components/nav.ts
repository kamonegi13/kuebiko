// 左サイドバー / TopBar 共通のナビ構成 (single source of truth)。
// IA: 概観 / インテリジェンス / コンテンツ / 運用 / 設定 の 5 グループ。
// Intel Graph の 4 タブ (synthesis/pmesii/threats/operations) は第一級項目へ昇格。

import { BookOpen, BookOpenText, Bookmark, CalendarClock, ClipboardCheck, Crosshair, FileText, Flag, HelpCircle, History, LayoutDashboard, Map, MessageSquareText, Newspaper, Rss, Scale, Settings, ShieldAlert, TrendingUp, Users, Workflow } from "lucide-react";

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
  /** 写し (Cloudflare Pages) に含める画面。**書き出したデータがある画面だけ** true。
   *
   *  ⚠ 印が無い画面を写しのナビに残すと、データが無いまま開いて読み込み中のまま
   *  固まる (2026-08-29 実測: 着地先のダッシュボードがこれで固まっていた)。
   *  写しへ画面を足すときは export_mirror.py の書き出しと必ず同時に印を付ける。 */
  mirror?: boolean;
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
      { href: "/app/questions", mirror: true, label: "PIR (問い)", Icon: HelpCircle, prefixes: ["/app/questions"] },
      { href: "/app/intel/synthesis", mirror: true, label: "現況", Icon: FileText, exact: ["/app/intel", "/app/intel/"], prefixes: ["/app/intel/synthesis"] },
      { href: "/app/intel/pmesii", mirror: true, label: "国家情勢", Icon: Scale, prefixes: ["/app/intel/pmesii"] },
      { href: "/app/intel/threats", mirror: true, label: "脅威アクター", Icon: Crosshair, prefixes: ["/app/intel/threats"] },
      { href: "/app/map", mirror: true, label: "脅威マップ", Icon: Map, prefixes: ["/app/map"] },
      { href: "/app/jpci", mirror: true, label: "重要インフラ脅威", Icon: ShieldAlert, prefixes: ["/app/jpci"] },
      { href: "/app/intel/forecast", mirror: true, label: "将来予測", Icon: TrendingUp, prefixes: ["/app/intel/forecast"] },
      // 振り返り (過去参照) はブリーフページの週次ビューに統合 (2026-07-25 時間軸統合)
      { href: "/app/pir", mirror: true, label: "SIR / Spotlight", Icon: Flag, prefixes: ["/app/pir"] },
      // 事象ニュース (2026-08-24 交代 → 2026-08-24 インテリジェンスへ移動)。
      // 同一事象の複数報道を束ね、**ツールが生成した**読み物。収集物そのものではなく
      // 生成された分析なので コンテンツ ではなく インテリジェンス に置く
      // (収集 = コンテンツ / 生成 = インテリジェンス の区分)。
      { href: "/app/eventnews", mirror: true, label: "事象ニュース", Icon: Newspaper, prefixes: ["/app/eventnews"] },
      // 分析チャット (2026-07-12): 自然言語でデータ照会 → 簡易レポート。
      // read-only ツールのみのため readonly instance でも利用可 (2026-07-19 allowlist 化)
      // 分析チャットは写しに入れない — LLM を都度呼ぶので、静止画では成立しない
      // (利用者と合意済。運用・設定も同じく Tier2 のみ)。
      { href: "/app/assistant", label: "分析チャット", Icon: MessageSquareText, prefixes: ["/app/assistant"] },
    ],
  },
  {
    title: "コンテンツ",
    items: [
      // 日次ブリーフ = 完成した配信物 (朝刊/夕刊) を読むページ。分析サーフェスではなく
      // コンテンツ (2026-07-12 ユーザー指摘で インテリジェンス → コンテンツ へ移動)。
      { href: "/app/daily-brief", mirror: true, label: "ブリーフ・振り返り", Icon: BookOpen, prefixes: ["/app/daily-brief", "/app/retrospect"] },
      { href: "/app/deep-dive", label: "週次深掘り", Icon: BookOpenText, prefixes: ["/app/deep-dive"] },
      // 収集した個々の記事を探す画面。生成物ではないので コンテンツ に残す
      // (事象ニュースは生成物なので インテリジェンス へ移動した)。
      { href: "/app/news", mirror: true, label: "ニュース検索", Icon: Newspaper, prefixes: ["/app/news", "/app/search", "/app/pivot"] },
      { href: "/app/notes", mirror: true, label: "ブックマーク・メモ", Icon: Bookmark, prefixes: ["/app/notes"] },
      { href: "/app/subscriptions", mirror: true, label: "購読ソース", Icon: Rss, prefixes: ["/app/subscriptions"] },
      { href: "/app/actors", mirror: true, label: "アクター辞書", Icon: Users, prefixes: ["/app/actors"] },
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
const MIRROR = import.meta.env.VITE_MIRROR === "1";

export function visibleNavGroups(hideFullOnly: boolean): NavGroup[] {
  // 絞り込みは **ここ 1 箇所** に集約する。サイドバーとコマンドパレットで別々に
  // 判定すると、片方に写していない画面が残って読み込み中で固まる。
  if (MIRROR) return mirrorNavGroups();
  if (!hideFullOnly) return NAV_GROUPS;
  return NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter((it) => !it.fullOnly) })).filter(
    (g) => g.items.length > 0,
  );
}

// 写し用: mirror 印のある項目だけを残す。fullOnly の絞り込みと同じ形。
export function mirrorNavGroups(): NavGroup[] {
  return NAV_GROUPS.map((g) => ({ ...g, items: g.items.filter((it) => it.mirror) })).filter(
    (g) => g.items.length > 0,
  );
}

/** 写しに含まれない path か。含まれないなら「写しに無い」と伝えて止める。 */
export function isOutsideMirror(pathname: string): boolean {
  return !NAV_FLAT.some((it) => it.mirror && isActive(it, pathname));
}

/** 写しの着地先。ダッシュボードは写しに無いので、写した画面へ着地させる。 */
export const MIRROR_HOME = "/app/eventnews";

export function visibleNavFlat(hideFullOnly: boolean): NavLink[] {
  if (MIRROR) return mirrorNavGroups().flatMap((g) => g.items);
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
  // モバイルの主導線も事象ニュース (読む画面)。記事一覧はメニューから辿る。
  { href: "/app/eventnews", mirror: true, label: "事象ニュース", Icon: Newspaper, prefixes: ["/app/eventnews"] },
  { href: "/app/intel/pmesii", mirror: true, label: "情勢", Icon: Scale, prefixes: ["/app/intel"] },
  { href: "/app/map", mirror: true, label: "マップ", Icon: Map, prefixes: ["/app/map"] },
];

/** モバイル下部タブ。写しでは写した画面だけ (サイドバーと同じ規則)。 */
export function visibleBottomNav(): NavLink[] {
  return MIRROR ? BOTTOM_NAV.filter((it) => it.mirror) : BOTTOM_NAV;
}

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
