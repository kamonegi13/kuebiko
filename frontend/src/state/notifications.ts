// 通知ストア。WS イベント (article_posted / synthesis_ready 等) を蓄積し、TopBar の
// ベルに未読数 + ドロップダウンで提示する。full-reload ナビでも残るよう localStorage 永続。

import { create } from "zustand";

export interface AppNotification {
  id: string;
  ts: number; // epoch ms
  icon: string;
  title: string;
  href: string;
}

interface NotificationState {
  items: AppNotification[];
  lastSeen: number;
  push: (n: Omit<AppNotification, "id" | "ts"> & { ts?: number }) => void;
  markAllRead: () => void;
  clear: () => void;
}

const STORE_KEY = "cti.notifications.v1";
const SEEN_KEY = "cti.notifications.lastSeen";
const MAX_ITEMS = 50;

function load(): { items: AppNotification[]; lastSeen: number } {
  if (typeof window === "undefined") return { items: [], lastSeen: 0 };
  try {
    const items = JSON.parse(localStorage.getItem(STORE_KEY) ?? "[]") as AppNotification[];
    const lastSeen = Number(localStorage.getItem(SEEN_KEY) ?? "0");
    return { items: Array.isArray(items) ? items : [], lastSeen: Number.isFinite(lastSeen) ? lastSeen : 0 };
  } catch {
    return { items: [], lastSeen: 0 };
  }
}

function persist(items: AppNotification[], lastSeen: number): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(items.slice(0, MAX_ITEMS)));
    localStorage.setItem(SEEN_KEY, String(lastSeen));
  } catch {
    /* localStorage 不可は無視 */
  }
}

function genId(): string {
  try {
    return crypto.randomUUID();
  } catch {
    return `${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  }
}

export const useNotifications = create<NotificationState>((set, get) => {
  const { items, lastSeen } = load();
  return {
    items,
    lastSeen,
    push: (n) => {
      const item: AppNotification = { id: genId(), ts: n.ts ?? Date.now(), icon: n.icon, title: n.title, href: n.href };
      const next = [item, ...get().items].slice(0, MAX_ITEMS);
      persist(next, get().lastSeen);
      set({ items: next });
    },
    markAllRead: () => {
      const now = Date.now();
      persist(get().items, now);
      set({ lastSeen: now });
    },
    clear: () => {
      const now = Date.now();
      persist([], now);
      set({ items: [], lastSeen: now });
    },
  };
});

export function unreadCount(items: AppNotification[], lastSeen: number): number {
  return items.filter((i) => i.ts > lastSeen).length;
}
