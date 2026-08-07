// 通知ベル + ドロップダウン。WS イベント由来の通知を提示し、開いたら既読化。

import { useEffect, useRef, useState } from "react";
import { Bell } from "lucide-react";
import { useNotifications, unreadCount } from "../state/notifications";

function relTime(ts: number): string {
  const sec = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (sec < 60) return "たった今";
  if (sec < 3600) return `${Math.floor(sec / 60)}分前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}時間前`;
  return `${Math.floor(sec / 86400)}日前`;
}

export function NotificationsBell() {
  const items = useNotifications((s) => s.items);
  const lastSeen = useNotifications((s) => s.lastSeen);
  const markAllRead = useNotifications((s) => s.markAllRead);
  const clear = useNotifications((s) => s.clear);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const unread = unreadCount(items, lastSeen);

  // 外側クリックで閉じる
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  // 開いたら既読化
  useEffect(() => {
    if (open && unread > 0) markAllRead();
  }, [open, unread, markAllRead]);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="relative inline-flex items-center justify-center w-8 h-8 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-2"
        title="通知"
        aria-label={`通知${unread > 0 ? ` (${unread} 件未読)` : ""}`}
      >
        <Bell size={17} />
        {unread > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-1 rounded-full bg-critical text-white text-[9px] font-bold leading-[15px] text-center">
            {unread > 9 ? "9+" : unread}
          </span>
        )}
      </button>

      {open && (
        <div className="absolute right-0 mt-1.5 w-[340px] max-w-[90vw] bg-surface-1 border border-border-emphasized rounded-lg shadow-2xl z-[90] overflow-hidden">
          <div className="flex items-center justify-between px-3 h-9 border-b border-border-subtle">
            <span className="text-[12px] font-semibold text-fg">通知</span>
            {items.length > 0 && (
              <button onClick={clear} className="text-[11px] text-fg-subtle hover:text-fg">クリア</button>
            )}
          </div>
          <div className="max-h-[60vh] overflow-y-auto">
            {items.length === 0 ? (
              <div className="px-3 py-8 text-center text-fg-subtle text-sm">通知はありません</div>
            ) : (
              items.map((n) => (
                <a
                  key={n.id}
                  href={n.href}
                  className="flex items-start gap-2.5 px-3 py-2 no-underline hover:bg-surface-2 border-b border-border-subtle last:border-0"
                >
                  <span className="text-base shrink-0 leading-tight">{n.icon}</span>
                  <span className="flex-1 min-w-0">
                    <span className="block text-[13px] text-fg leading-snug">{n.title}</span>
                    <span className="block text-[10px] text-fg-subtle mt-0.5">{relTime(n.ts)}</span>
                  </span>
                </a>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
