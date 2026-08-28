/** 地図の配色をテーマから読む。
 *
 *  Leaflet は色を **JS の値**として受け取り SVG 属性へ落とすため、
 *  `var(--x)` をそのまま渡しても解決されない環境がある。実値を
 *  ``getComputedStyle`` で読み、テーマが変わったら塗り直す。
 */
export type MapColors = { sea: string; land: string; border: string };

export function readMapColors(): MapColors {
  if (typeof document === "undefined") {
    return { sea: "#0a0e16", land: "#1a2030", border: "rgba(255,255,255,0.13)" };
  }
  const s = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => s.getPropertyValue(name).trim() || fallback;
  return {
    sea: v("--map-sea", "#0a0e16"),
    land: v("--map-land", "#1a2030"),
    border: v("--map-border", "rgba(255,255,255,0.13)"),
  };
}

/** テーマ切替を購読する。戻り値を呼ぶと解除。
 *
 *  ``useTheme`` が切替時に投げる custom event を拾う。属性の MutationObserver でも
 *  よいが、発生源が 1 つなので event のほうが読み解きやすい。 */
export function onThemeChange(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener("kuebiko-theme", handler);
  return () => window.removeEventListener("kuebiko-theme", handler);
}
