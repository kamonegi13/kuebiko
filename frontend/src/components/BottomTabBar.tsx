// モバイル専用ボトムタブバー (md:hidden)。高頻度 4 項目 + 「メニュー」(全 nav を
// sidebar drawer で開く)。href は plain anchor で Sidebar と同じ full-reload 挙動に揃える。
// fixed bottom のため、AppShell 側で本体に下部 padding を足してコンテンツの隠れを防ぐ。

import { Menu } from "lucide-react";
import { BOTTOM_NAV, isActive } from "./nav";

interface BottomTabBarProps {
  pathname: string;
  onOpenMenu: () => void;
}

export function BottomTabBar({ pathname, onOpenMenu }: BottomTabBarProps) {
  // safe-area padding は背景を持つ外側 nav に置き、項目は内側の固定高 (h-14) 行に分離する。
  // こうすると Chrome のツールバー出入りで safe-area-inset が変動しても、背景 (nav) が
  // 常に項目行 + safe-area を覆い、アイコンが背景からはみ出さない。
  return (
    <nav
      aria-label="モバイルナビ"
      className="md:hidden fixed bottom-0 inset-x-0 z-40 bg-bg/95 backdrop-blur-md border-t border-border-subtle"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <div className="h-14 flex items-stretch">
        {BOTTOM_NAV.map((item) => {
          const active = isActive(item, pathname);
          return (
            <a
              key={item.href}
              href={item.href}
              className={`flex-1 flex flex-col items-center justify-center gap-0.5 no-underline ${
                active ? "text-accent" : "text-fg-muted"
              }`}
            >
              <item.Icon size={20} className="shrink-0" />
              <span className="text-[10px] leading-none">{item.label}</span>
            </a>
          );
        })}
        <button
          onClick={onOpenMenu}
          aria-label="メニューを開く"
          className="flex-1 flex flex-col items-center justify-center gap-0.5 text-fg-muted active:text-accent"
        >
          <Menu size={20} className="shrink-0" />
          <span className="text-[10px] leading-none">メニュー</span>
        </button>
      </div>
    </nav>
  );
}
