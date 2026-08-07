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
        "fg-subtle": "#6b7280",
        "fg-faint": "#4a5260",
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
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "Inter",
          "SF Pro Text",
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
      fontSize: {
        xs: ["11px", { lineHeight: "1.4" }],
        sm: ["12.5px", { lineHeight: "1.55" }],
        base: ["13.5px", { lineHeight: "1.55" }],
        md: ["15px", { lineHeight: "1.5" }],
        lg: ["17px", { lineHeight: "1.4" }],
        xl: ["20px", { lineHeight: "1.3" }],
        "2xl": ["26px", { lineHeight: "1.2" }],
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
