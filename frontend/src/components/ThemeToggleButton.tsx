import { Moon, Sun } from "lucide-react";
import { useTheme } from "../hooks/useTheme";

/** 明暗の切替。
 *
 *  置き場所は **ヘッダ右の通知ベルの隣** — 公開サイト (Tier0) の題字右と
 *  同じ「画面の右上」に揃える (2026-08-28 利用者指摘)。サイドバー下部だと
 *  折りたたみ時に見えず、面ごとに場所が変わって覚えられない。
 */
export function ThemeToggleButton() {
  const { theme, toggle } = useTheme();
  return (
    <button
      onClick={toggle}
      title={theme === "light" ? "暗い配色に切り替える" : "明るい配色に切り替える"}
      aria-label={theme === "light" ? "暗い配色に切り替える" : "明るい配色に切り替える"}
      className="inline-flex items-center justify-center h-8 w-8 rounded-md text-fg-subtle hover:text-fg hover:bg-surface-2 transition-colors"
    >
      {theme === "light" ? <Moon size={16} /> : <Sun size={16} />}
    </button>
  );
}
