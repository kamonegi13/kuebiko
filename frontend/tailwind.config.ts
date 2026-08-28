import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // CTI dark theme refined palette
        bg: "#0b0d11",
        "surface-1": "#14181f",
        "surface-2": "#1a1f28",
        "surface-3": "#212733",
        "surface-overlay": "#2a3140",
        fg: "#e8ebf0",
        "fg-muted": "#a0a8b4",
        // 2026-08-28 可読性診断: 旧値 #6b7280 は背景上 4.02:1 / カード上 3.68:1 で
        // WCAG AA (4.5:1) 未達だった。しかも **公開面で最も多用する文字色**を
        // 10〜11px に当てていた (小さい文字ほど高コントラストが要る原則と逆)。
        // 新値は 4 つの面すべてで 5.0:1 以上。
        "fg-subtle": "#8f97a6",
        // 旧値 #4a5260 は 2.47:1 で読めない。灰色の階層を 4 段持つのが無理だったので
        // fg-subtle に畳む (トークン名は残す — 参照箇所が多く、消すと差分が読めなくなる)。
        "fg-faint": "#8f97a6",
        accent: {
          DEFAULT: "#6b88ff",
          hover: "#8aa1ff",
          subtle: "rgba(107,136,255,0.08)",
          soft: "rgba(107,136,255,0.18)",
          strong: "rgba(107,136,255,0.32)",
          ring: "rgba(107,136,255,0.35)",
        },
        critical: "#ff6b6b",
        "critical-soft": "rgba(255,107,107,0.12)",
        warning: "#f5a623",
        "warning-soft": "rgba(245,166,35,0.12)",
        success: "#45b878",
        "success-soft": "rgba(69,184,120,0.12)",
        "border-subtle": "rgba(255,255,255,0.05)",
        "border-default": "rgba(255,255,255,0.09)",
        "border-emphasized": "rgba(255,255,255,0.16)",
        "border-strong": "rgba(255,255,255,0.24)",
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
