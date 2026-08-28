import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      // 色は CSS 変数を通す (2026-08-28 ライトモード対応)。
      // ``:root`` の既定値は従来のダーク値そのままなので、運用画面の見た目は変わらない。
      // ライト値は **公開サイトだけ** が `data-theme="light"` で拾う (src/index.css)。
      // 不透明度つき (`bg-accent/20` 等) を保つため、変数は RGB 三値で持ち
      // `rgb(var(--x) / <alpha-value>)` の形にする。hex を入れると alpha が効かない。
      colors: {
        bg: "rgb(var(--c-bg) / <alpha-value>)",
        "surface-1": "rgb(var(--c-surface-1) / <alpha-value>)",
        "surface-2": "rgb(var(--c-surface-2) / <alpha-value>)",
        "surface-3": "rgb(var(--c-surface-3) / <alpha-value>)",
        "surface-overlay": "rgb(var(--c-surface-overlay) / <alpha-value>)",
        fg: "rgb(var(--c-fg) / <alpha-value>)",
        "fg-muted": "rgb(var(--c-fg-muted) / <alpha-value>)",
        // 2026-08-28 可読性診断: 旧値 #6b7280 は背景上 4.02:1 / カード上 3.68:1 で
        // WCAG AA (4.5:1) 未達だった。しかも **公開面で最も多用する文字色**を
        // 10〜11px に当てていた (小さい文字ほど高コントラストが要る原則と逆)。
        "fg-subtle": "rgb(var(--c-fg-subtle) / <alpha-value>)",
        // 旧値 #4a5260 は 2.47:1 で読めない。灰色の階層を 4 段持つのが無理だったので
        // fg-subtle に畳む (トークン名は残す — 参照箇所が多く、消すと差分が読めなくなる)。
        "fg-faint": "rgb(var(--c-fg-subtle) / <alpha-value>)",
        accent: {
          DEFAULT: "rgb(var(--c-accent) / <alpha-value>)",
          hover: "rgb(var(--c-accent-hover) / <alpha-value>)",
          subtle: "rgb(var(--c-accent) / 0.08)",
          soft: "rgb(var(--c-accent) / 0.18)",
          strong: "rgb(var(--c-accent) / 0.32)",
          ring: "rgb(var(--c-accent) / 0.35)",
        },
        critical: "rgb(var(--c-critical) / <alpha-value>)",
        "critical-soft": "rgb(var(--c-critical) / 0.12)",
        warning: "rgb(var(--c-warning) / <alpha-value>)",
        "warning-soft": "rgb(var(--c-warning) / 0.12)",
        success: "rgb(var(--c-success) / <alpha-value>)",
        "success-soft": "rgb(var(--c-success) / 0.12)",
        "border-subtle": "rgb(var(--c-border) / var(--c-border-a-subtle))",
        "border-default": "rgb(var(--c-border) / var(--c-border-a-default))",
        "border-emphasized": "rgb(var(--c-border) / var(--c-border-a-emphasized))",
        "border-strong": "rgb(var(--c-border) / var(--c-border-a-strong))",
      },
      fontFamily: {
        // 和文を先頭に置かない: 欧文は各 OS の UI 書体に任せ、和文だけ Noto Sans JP で
        // 揃える。逆順にすると欧文まで Noto の欧文になり、数字や CVE 番号の字面が
        // 目に馴染まなくなる (2026-08-28 可読性診断)。
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Inter",
          "SF Pro Text",
          "Noto Sans JP",
          "Hiragino Kaku Gothic ProN",
          "Meiryo",
          "sans-serif",
        ],
        mono: [
          "ui-monospace",
          "SF Mono",
          "JetBrains Mono",
          "Cascadia Mono",
          "Monaco",
          "monospace",
        ],
      },
      // 2026-08-28 可読性診断: 旧 base は 13.5px で、参考基準の下限 (14px) を割っていた。
      // 和文の行間も 1.55 で推奨 (1.6〜1.8) の下。**密度は行間ではなく余白で作る**方針に
      // 変え、行間を確保したうえでサイズを 1 段上げる。
      fontSize: {
        xs: ["11.5px", { lineHeight: "1.5" }],
        sm: ["13px", { lineHeight: "1.65" }],
        base: ["14px", { lineHeight: "1.75" }],
        md: ["16px", { lineHeight: "1.7" }],
        lg: ["18px", { lineHeight: "1.5" }],
        xl: ["21px", { lineHeight: "1.4" }],
        "2xl": ["27px", { lineHeight: "1.3" }],
      },
      borderRadius: { sm: "4px", md: "6px", lg: "8px", xl: "12px" },
      transitionTimingFunction: { smooth: "cubic-bezier(0.16, 1, 0.3, 1)" },
      animation: {
        "fade-in": "fadeIn 160ms ease-out",
        shimmer: "shimmer 1.6s ease-in-out infinite",
        "slide-in-right": "slideInRight 220ms cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        fadeIn: {
          from: { opacity: "0.4", transform: "translateY(2px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        slideInRight: {
          from: { transform: "translateX(100%)" },
          to: { transform: "translateX(0)" },
        },
      },
    },
  },
  plugins: [],
} satisfies Config;
