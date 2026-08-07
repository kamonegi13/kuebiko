// ⌘K コマンドパレット: ページ / アクター / PIR を横断ジャンプ。
// データは開いた時に lazy fetch (アクター辞書 = 軽量 YAML API、pir list)。
// アクターは canonical だけでなく別名 (aliases) / MITRE ID でも照合する。

import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { pagesApi } from "../api/pages";
import { pirApi } from "../api/pir";
import { visibleNavFlat } from "./nav";
import { actorHref } from "../utils/intelNav";
import { useRuntimeFlags, shouldHideFullOnly } from "../hooks/useRuntimeFlags";

interface CmdItem {
  key: string;
  label: string;
  sub?: string;
  href: string;
  group: "ページ" | "アクター" | "PIR";
}

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

const MAX_PER_GROUP = 8;

export function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const flags = useRuntimeFlags();
  const hideFullOnly = shouldHideFullOnly(flags);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // アクター辞書 (YAML のみで軽量)。queryKey "actors" は辞書ページと共有キャッシュ。
  // 旧実装の api.snapshot({time:"90"}) は記事全走査を誘発しパレット表示が数秒詰まっていた。
  const { data: actorsDict } = useQuery({
    queryKey: ["actors"],
    queryFn: () => pagesApi.getActors(),
    enabled: open,
    staleTime: 5 * 60_000,
  });
  const { data: pirList } = useQuery({
    queryKey: ["palette-pir"],
    queryFn: () => pirApi.list(),
    enabled: open,
    staleTime: 5 * 60_000,
  });

  // open になったら reset + focus
  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const items = useMemo<CmdItem[]>(() => {
    const q = query.trim().toLowerCase();
    const navItems: CmdItem[] = visibleNavFlat(hideFullOnly).map((n) => ({ key: `nav:${n.href}`, label: n.label, href: n.href, group: "ページ" }));
    // sub に別名 + MITRE ID を含めることで match() がそのまま別名検索になる
    // (例: "Cicada" → APT10、"G0045" → APT10)。
    const actorItems: CmdItem[] = (actorsDict?.actors ?? []).map((a) => ({
      key: `actor:${a.id}`,
      label: a.canonical,
      sub: [...(a.aliases ?? []), a.mitre_group ?? ""].filter(Boolean).join(", ") || a.id,
      href: actorHref(a.id),
      group: "アクター",
    }));
    const pirItems: CmdItem[] = (pirList?.priorities ?? []).map((p) => ({
      key: `pir:${p.id}`, label: p.title, sub: p.id, href: `/app/pir/${encodeURIComponent(p.id)}`, group: "PIR",
    }));

    const match = (it: CmdItem) => q === "" || it.label.toLowerCase().includes(q) || (it.sub?.toLowerCase().includes(q) ?? false);
    const cap = (arr: CmdItem[]) => (q === "" ? arr.slice(0, MAX_PER_GROUP) : arr.filter(match).slice(0, MAX_PER_GROUP));

    return [
      ...navItems.filter(match),
      ...cap(actorItems),
      ...cap(pirItems),
    ];
  }, [query, actorsDict, pirList, hideFullOnly]);

  // active を範囲内にクランプ
  useEffect(() => { setActive((a) => Math.max(0, Math.min(a, items.length - 1))); }, [items.length]);

  // active 行をスクロール内に
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${active}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  if (!open) return null;

  const go = (it: CmdItem | undefined) => {
    if (!it) return;
    onClose();
    window.location.href = it.href;
  };

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === "Enter") { e.preventDefault(); go(items[active]); }
    else if (e.key === "Escape") { e.preventDefault(); onClose(); }
  };

  // group ごとに区切りラベルを差し込みながら描画
  let lastGroup = "";
  return (
    <div className="fixed inset-0 z-[100] flex items-start justify-center pt-[12vh] px-4" onMouseDown={onClose}>
      <div className="absolute inset-0 bg-black/55 backdrop-blur-[2px]" aria-hidden />
      <div
        className="relative w-full max-w-[620px] bg-surface-1 border border-border-emphasized rounded-xl shadow-2xl overflow-hidden"
        onMouseDown={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="コマンドパレット"
      >
        <div className="flex items-center gap-2 px-3.5 h-12 border-b border-border-subtle">
          <Search size={16} className="text-fg-subtle shrink-0" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
            placeholder="ページ / アクター / PIR を検索…"
            className="flex-1 bg-transparent text-[15px] text-fg placeholder:text-fg-subtle focus:outline-none"
          />
          <kbd className="text-[10px] font-mono text-fg-subtle bg-surface-3 px-1.5 py-0.5 rounded">esc</kbd>
        </div>

        <div ref={listRef} className="max-h-[52vh] overflow-y-auto py-1.5">
          {items.length === 0 ? (
            <div className="px-4 py-6 text-center text-fg-subtle text-sm">該当なし</div>
          ) : (
            items.map((it, idx) => {
              const showHeader = it.group !== lastGroup;
              lastGroup = it.group;
              return (
                <div key={it.key}>
                  {showHeader && (
                    <div className="px-3.5 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-fg-subtle">{it.group}</div>
                  )}
                  <button
                    data-idx={idx}
                    onMouseEnter={() => setActive(idx)}
                    onClick={() => go(it)}
                    className={`w-full flex items-center gap-2.5 px-3.5 py-2 text-left ${
                      idx === active ? "bg-accent-subtle" : "hover:bg-surface-2"
                    }`}
                  >
                    <span className={`flex-1 min-w-0 truncate text-sm ${idx === active ? "text-accent-hover font-medium" : "text-fg"}`}>{it.label}</span>
                    {it.sub && <span className="text-[11px] text-fg-subtle font-mono truncate max-w-[180px] shrink-0">{it.sub}</span>}
                  </button>
                </div>
              );
            })
          )}
        </div>

        <div className="flex items-center gap-3 px-3.5 h-8 border-t border-border-subtle text-[10px] text-fg-subtle">
          <span><kbd className="font-mono">↑↓</kbd> 移動</span>
          <span><kbd className="font-mono">↵</kbd> 開く</span>
          <span className="ml-auto">{items.length} 件</span>
        </div>
      </div>
    </div>
  );
}
