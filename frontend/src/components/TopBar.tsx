// 細い TopBar: drawer トグル + breadcrumb + グローバル検索 (⌘K) + 通知ベル。
// sticky top-0 / h-12 = 既存ページの sticky offset (top-12) に整合。

import { ChevronRight, Search } from "lucide-react";
import { findActiveWithGroup } from "./nav";
import { NotificationsBell } from "./NotificationsBell";

interface TopBarProps {
  pathname: string;
  onOpenPalette: () => void;
}

// モバイルの drawer 起動はボトムタブバーの「メニュー」に一本化したため、
// TopBar からハンバーガーは撤去 (上下の重複解消)。上部は breadcrumb + 検索 + 通知。
export function TopBar({ pathname, onOpenPalette }: TopBarProps) {
  const crumb = findActiveWithGroup(pathname);
  return (
    <header className="sticky top-0 z-40 h-12 shrink-0 bg-bg/85 backdrop-blur-md border-b border-border-subtle px-3 md:px-5 flex items-center gap-3">
      {/* breadcrumb: グループ › 現在地 */}
      <nav aria-label="現在地" className="flex items-center gap-1.5 min-w-0">
        {crumb ? (
          <>
            <span className="hidden sm:inline text-[12px] text-fg-subtle">{crumb.group}</span>
            <ChevronRight size={13} className="hidden sm:block text-fg-subtle shrink-0" />
            <span className="flex items-center gap-1.5 min-w-0">
              <crumb.item.Icon size={15} className="text-accent shrink-0" />
              <span className="text-[14px] font-semibold text-fg truncate">{crumb.item.label}</span>
            </span>
          </>
        ) : (
          <span className="text-[14px] font-semibold text-fg truncate">Kuebiko</span>
        )}
      </nav>

      {/* グローバル検索 (⌘K) */}
      <button
        onClick={onOpenPalette}
        className="ml-auto inline-flex items-center gap-2 h-8 px-2.5 rounded-md bg-surface-2 border border-border-subtle text-fg-subtle hover:text-fg hover:border-border-default transition-colors"
        title="検索・ジャンプ (⌘K)"
        aria-label="検索"
      >
        <Search size={15} />
        <span className="hidden md:inline text-[12px]">検索…</span>
        <kbd className="hidden md:inline text-[10px] font-mono bg-surface-3 px-1.5 py-0.5 rounded ml-1">⌘K</kbd>
      </button>

      <NotificationsBell />
    </header>
  );
}
