// 左サイドバー (グループ化ナビ + 折りたたみレール + モバイル drawer)。
// 折りたたみ時はアイコンのみ + tooltip。href は plain anchor (既存どおり full reload)。

import { ChevronsLeft, ChevronsRight, LogIn, LogOut } from "lucide-react";
import { isActive, visibleNavGroups } from "./nav";
import { useRuntimeFlags, shouldHideFullOnly } from "../hooks/useRuntimeFlags";

interface SidebarProps {
  collapsed: boolean;
  mobileOpen: boolean;
  pathname: string;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
}

export function Sidebar({ collapsed, mobileOpen, pathname, onToggleCollapse, onCloseMobile }: SidebarProps) {
  // readonly instance の未認証時のみ fullOnly (編集/操作系) 項目をメニューから隠す
  const flags = useRuntimeFlags();
  const hideFullOnly = shouldHideFullOnly(flags);
  const width = collapsed ? "md:w-14" : "md:w-60";
  return (
    <>
      {/* モバイル backdrop */}
      {mobileOpen && (
        <div onClick={onCloseMobile} aria-hidden className="md:hidden fixed inset-0 z-[60] bg-black/50" />
      )}

      <aside
        className={[
          "fixed top-0 left-0 z-[70] h-screen flex flex-col",
          "bg-surface-1 border-r border-border-subtle",
          "transition-[width,transform] duration-200 ease-out",
          // mobile: off-canvas drawer (w-60)、md+: 常時表示で width 切替
          "w-60",
          mobileOpen ? "translate-x-0" : "-translate-x-full md:translate-x-0",
          width,
        ].join(" ")}
        aria-label="メインナビゲーション"
      >
        {/* ブランド + 折りたたみ */}
        <div className="h-12 shrink-0 flex items-center gap-2 px-3 border-b border-border-subtle">
          <span className="text-accent text-base shrink-0">◆</span>
          {!collapsed && <span className="font-semibold text-[14px] tracking-tight text-fg truncate flex-1">Kuebiko</span>}
          <button
            onClick={onToggleCollapse}
            className="hidden md:inline-flex items-center justify-center w-7 h-7 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-2 shrink-0"
            title={collapsed ? "展開" : "折りたたむ"}
            aria-label={collapsed ? "サイドバーを展開" : "サイドバーを折りたたむ"}
          >
            {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          </button>
        </div>

        {/* ナビ本体 (スクロール) */}
        <nav className="flex-1 overflow-y-auto py-2 px-2 space-y-3">
          {visibleNavGroups(hideFullOnly).map((group) => (
            <div key={group.title}>
              {!collapsed && (
                <div className="px-2 mb-1 text-[10px] font-semibold uppercase tracking-wider text-fg-subtle">{group.title}</div>
              )}
              {collapsed && <div className="mx-2 mb-1 h-px bg-border-subtle" aria-hidden />}
              <ul className="space-y-0.5">
                {group.items.map((item) => {
                  const active = isActive(item, pathname);
                  return (
                    <li key={item.href}>
                      <a
                        href={item.href}
                        onClick={onCloseMobile}
                        title={collapsed ? item.label : undefined}
                        className={[
                          "group flex items-center gap-2.5 rounded-md no-underline transition-colors",
                          collapsed ? "md:justify-center md:px-0 px-2.5 py-2" : "px-2.5 py-2",
                          active
                            ? "bg-accent-subtle text-accent-hover font-semibold"
                            : "text-fg-muted hover:bg-surface-2 hover:text-fg",
                        ].join(" ")}
                      >
                        <item.Icon size={17} className="shrink-0" strokeWidth={active ? 2.4 : 2} />
                        <span className={collapsed ? "md:hidden text-[13px] truncate" : "text-[13px] truncate"}>{item.label}</span>
                      </a>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        {/* ログイン導線 (公開 instance で Cloudflare Access が設定済みのときだけ表示)。
            認証すると運用系ページの閲覧とジョブ即時実行が解放される (write はローカル専用)。 */}
        {flags.auth_available && (
          <div className="shrink-0 border-t border-border-subtle p-2">
            <a
              // ログアウトは Access 保護対象外の /logout (保護下に置くと cookie 破棄後に
              // ログイン画面へ送られてしまう)。ログインは保護対象の /auth/login。
              href={flags.authenticated ? "/logout" : "/auth/login"}
              title={flags.authenticated ? "ログアウト" : "ログイン (運用ページを表示)"}
              className={[
                "flex items-center gap-2.5 rounded-md no-underline px-2.5 py-2",
                "text-fg-muted hover:bg-surface-2 hover:text-fg",
                collapsed ? "md:justify-center md:px-0" : "",
              ].join(" ")}
            >
              {flags.authenticated ? (
                <LogOut size={17} className="shrink-0" strokeWidth={2} />
              ) : (
                <LogIn size={17} className="shrink-0" strokeWidth={2} />
              )}
              <span className={collapsed ? "md:hidden text-[13px] truncate" : "text-[13px] truncate"}>
                {flags.authenticated ? "ログアウト" : "ログイン"}
              </span>
            </a>
          </div>
        )}
      </aside>
    </>
  );
}
