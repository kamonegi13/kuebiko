// 名前付きの絞り込み「ビュー」をタブで表示する (docs/news_filter_ux.md §3)。
// 既定ビュー (BUILTIN_VIEWS) + 利用者保存ビューを並べる。選択で絞り込みを置き換え、
// 絞り込みを編集すると一致するタブが外れる (「変更あり」として未選択表示になる)。

import { useState } from "react";
import { Pencil, Plus, Trash2, X } from "lucide-react";
import { BUILTIN_VIEWS, isUserViewId, matchingViewId, type NewsFilters, type NewsView } from "./views";
import { useNewsViews } from "./useNewsViews";

export interface NewsViewTabsProps {
  currentFilters: NewsFilters;
  onApply: (filters: Partial<NewsFilters>) => void;
}

export function NewsViewTabs({ currentFilters, onApply }: NewsViewTabsProps) {
  const { userViews, saveView, renameView, deleteView, notice } = useNewsViews();
  const views: NewsView[] = [...BUILTIN_VIEWS, ...userViews];
  const activeId = matchingViewId(currentFilters, views);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameText, setRenameText] = useState("");
  const [savingAs, setSavingAs] = useState(false);
  const [newLabel, setNewLabel] = useState("");

  function startRename(v: NewsView): void {
    setRenamingId(v.id);
    setRenameText(v.label);
  }
  function commitRename(): void {
    if (renamingId && renameText.trim()) renameView(renamingId, renameText.trim());
    setRenamingId(null);
  }
  function commitSave(): void {
    if (newLabel.trim()) saveView(newLabel.trim(), currentFilters);
    setNewLabel("");
    setSavingAs(false);
  }

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {views.map((v) => (
        <div key={v.id} className="inline-flex items-center">
          {renamingId === v.id ? (
            <input
              autoFocus
              value={renameText}
              onChange={(e) => setRenameText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") commitRename();
                if (e.key === "Escape") setRenamingId(null);
              }}
              onBlur={commitRename}
              className="h-7 px-2 text-xs bg-surface-2 border border-accent rounded-md"
            />
          ) : (
            <button
              onClick={() => onApply(v.filters)}
              className={`h-7 px-3 rounded-md text-xs font-medium border transition-colors ${
                activeId === v.id
                  ? "border-accent text-accent bg-accent/10"
                  : "border-border-subtle text-fg-muted hover:text-fg"
              }`}
            >
              {v.label}
            </button>
          )}
          {isUserViewId(v.id) && renamingId !== v.id && (
            <span className="inline-flex items-center gap-0.5 ml-0.5">
              <button
                onClick={() => startRename(v)}
                title="名前を変更"
                className="p-1 text-fg-subtle hover:text-accent"
              >
                <Pencil className="h-3 w-3" />
              </button>
              <button
                onClick={() => deleteView(v.id)}
                title="このビューを削除"
                className="p-1 text-fg-subtle hover:text-critical"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </span>
          )}
        </div>
      ))}

      {activeId === null && !savingAs && (
        <button
          onClick={() => {
            setSavingAs(true);
            setNewLabel("");
          }}
          title="いまの絞り込みをビューとして保存"
          className="h-7 px-2.5 inline-flex items-center gap-1 rounded-md border border-dashed border-border-subtle text-fg-subtle hover:text-accent hover:border-accent text-xs"
        >
          <Plus className="h-3 w-3" /> この絞り込みをビューとして保存
        </button>
      )}
      {savingAs && (
        <span className="inline-flex items-center gap-1">
          <input
            autoFocus
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
            placeholder="ビュー名"
            onKeyDown={(e) => {
              if (e.key === "Enter") commitSave();
              if (e.key === "Escape") setSavingAs(false);
            }}
            className="h-7 px-2 text-xs bg-surface-2 border border-accent rounded-md w-32"
          />
          <button onClick={commitSave} className="h-7 px-2 text-xs rounded-md border border-accent text-accent">
            保存
          </button>
          <button onClick={() => setSavingAs(false)} className="p-1 text-fg-subtle hover:text-fg">
            <X className="h-3.5 w-3.5" />
          </button>
        </span>
      )}
      {notice && <span className="text-xs text-fg-subtle italic">{notice}</span>}
    </div>
  );
}
