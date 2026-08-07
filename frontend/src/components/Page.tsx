// 全ページ共通のレイアウト primitive (幅・余白・見出しの統一)。
// 一覧/表/ダッシュボード = wide (~1680px)、設定/フォーム/読み物 = narrow (~920px)。
// sidebar 導入後のワイド画面で余白が過大にならないよう上限を広めに取る。

import type { ReactNode } from "react";

export type PageWidth = "wide" | "narrow" | "fluid";

const MAXW: Record<PageWidth, string> = {
  wide: "max-w-[1680px]",
  narrow: "max-w-[920px]",
  fluid: "max-w-none",
};

// ページ root コンテナの className。横 padding は画面サイズで段階的に拡大。
export function pageContainer(width: PageWidth = "wide"): string {
  return `mx-auto w-full px-4 md:px-6 lg:px-8 py-5 ${MAXW[width]}`;
}

// 統一ページ見出し class。
export const PAGE_TITLE = "m-0 text-xl font-bold text-fg tracking-tight";

// 統一ページヘッダ (タイトル + 任意の subtitle / 右寄せ actions)。
export function PageHeader({ title, subtitle, actions }: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3 flex-wrap">
      <div className="min-w-0">
        <h2 className={PAGE_TITLE}>{title}</h2>
        {subtitle && <p className="text-sm text-fg-muted mt-0.5">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  );
}
