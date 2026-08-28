import { useState } from "react";

export type Theme = "light" | "dark";

/** 明暗テーマの切替。
 *
 *  属性は **描画前に HTML 側で確定させている** (index.html / public.html の inline
 *  script)。React 待ちにすると初回に別配色の画面が一瞬出るため。ここでは
 *  利用者が明示的に選んだときの反映と保存だけを受け持つ。
 */
export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(() =>
    typeof document !== "undefined" && document.documentElement.dataset.theme === "light"
      ? "light"
      : "dark",
  );
  const toggle = () => {
    const next: Theme = theme === "light" ? "dark" : "light";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", next === "light" ? "#faf9f6" : "#0b0d11");
    try {
      localStorage.setItem("kuebiko-theme", next);
    } catch {
      /* プライベートブラウズ等で書けなくても切替自体は効かせる */
    }
  };
  return { theme, toggle };
}
