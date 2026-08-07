// アプリ全体の外枠: 左 Sidebar + (TopBar + ページ本体) の 2 カラム。
// Sidebar は fixed、本体は md+ で sidebar 幅ぶんの margin。折りたたみ状態は localStorage 永続。
// document スクロールを維持 (既存ページの sticky offset を壊さない)。
// ⌘K パレット + WS 由来の通知集約もここで担う (全ページ共通)。

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Sidebar } from "./Sidebar";
import { TopBar } from "./TopBar";
import { BottomTabBar } from "./BottomTabBar";
import { CommandPalette } from "./CommandPalette";
import { useChannelMeta } from "./channel";
import { useWebSocket } from "../hooks/useWebSocket";
import { useNotifications } from "../state/notifications";
import { vocabLabel } from "../hooks/useVocab";
import { actorHref } from "../utils/intelNav";

const COLLAPSE_KEY = "cti.sidebar.collapsed";

interface AppShellProps {
  pathname: string;
  children: ReactNode;
}

export function AppShell({ pathname, children }: AppShellProps) {
  const [collapsed, setCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem(COLLAPSE_KEY) === "1";
    } catch {
      return false;
    }
  });
  const [mobileOpen, setMobileOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const pushNotification = useNotifications((s) => s.push);
  const chMeta = useChannelMeta();

  useEffect(() => {
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      /* localStorage 不可環境は無視 */
    }
  }, [collapsed]);

  // モバイル drawer 表示中は body スクロールをロック
  useEffect(() => {
    if (!mobileOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = prev; };
  }, [mobileOpen]);

  // ページの sticky フィルタ/検索バーはスクロールで隠さず常時表示 (sticky)。
  // (旧: 下スクロールで隠す挙動は、再表示が過敏/上端でしか戻せず利点が薄いため撤去。
  //  chrome state は常に visible のまま。mid-page 検索は ⌘K グローバル検索も利用可。)

  // ⌘K / Ctrl+K でパレット開閉
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // WS イベント → 通知へ集約 (全ページ共通の 1 接続)
  useWebSocket(useCallback((ev) => {
    const p = ev.payload ?? {};
    if (ev.type === "article_posted") {
      const importance = typeof p.importance === "string" ? p.importance : "";
      const channel = typeof p.channel === "string" ? p.channel : "";
      // ノイズ抑制: high / alert ch のみ通知
      if (importance === "high" || channel === "alert") {
        const channelLabel = channel ? chMeta(channel).label : "";
        pushNotification({ icon: "", title: `重要記事を投稿${channelLabel ? ` (${channelLabel})` : ""}`, href: "/app/history" });
      }
    } else if (ev.type === "synthesis_ready") {
      const period = typeof p.period_type === "string" ? p.period_type : "";
      const periodLabel = vocabLabel("period_type", period);
      pushNotification({ icon: "", title: `${periodLabel ? `${periodLabel} ` : ""}現況評価を更新`, href: "/app/intel/synthesis" });
    } else if (ev.type === "spike_alert") {
      const name = typeof p.actor === "string" ? p.actor : typeof p.name === "string" ? p.name : "";
      pushNotification({ icon: "", title: `アクター活動が急増${name ? `: ${name}` : ""}`, href: name ? actorHref(name) : "/app/intel/threats" });
    } else if (ev.type === "actor_new") {
      const name = typeof p.actor === "string" ? p.actor : typeof p.name === "string" ? p.name : "";
      pushNotification({ icon: "", title: `新規アクター${name ? `: ${name}` : ""}`, href: name ? actorHref(name) : "/app/intel/threats" });
    }
  }, [pushNotification, chMeta]));

  return (
    <div className="min-h-screen">
      <Sidebar
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        pathname={pathname}
        onToggleCollapse={() => setCollapsed((c) => !c)}
        onCloseMobile={() => setMobileOpen(false)}
      />
      {/* overflow-x-clip: 子要素の横はみ出しでページ全体が横に広がる (モバイルで
          スクロール不能なまま見切れる) のを防ぐ安全網。clip は sticky を壊さない。 */}
      <div className={`min-w-0 overflow-x-clip transition-[margin] duration-200 ease-out ${collapsed ? "md:ml-14" : "md:ml-60"}`}>
        <TopBar
          pathname={pathname}
          onOpenPalette={() => setPaletteOpen(true)}
        />
        {/* 下部 padding: モバイルの BottomTabBar (h-14=56px) + safe-area ぶんを確保し
            コンテンツが隠れないようにする。md+ は bar 非表示なので 0。 */}
        <main className="md:pb-0 pb-[calc(3.5rem+env(safe-area-inset-bottom))]">{children}</main>
      </div>
      <BottomTabBar pathname={pathname} onOpenMenu={() => setMobileOpen(true)} />
      <CommandPalette open={paletteOpen} onClose={() => setPaletteOpen(false)} />
    </div>
  );
}
