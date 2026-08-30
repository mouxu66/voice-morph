/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 主题色板（亮/暗通过 CSS 变量切换，见 styles/index.css）
        ink: {
          950: "rgb(var(--c-bg) / <alpha-value>)",
          900: "rgb(var(--c-bg-1) / <alpha-value>)",
          850: "rgb(var(--c-surface) / <alpha-value>)",
          800: "rgb(var(--c-surface-2) / <alpha-value>)",
          700: "rgb(var(--c-border) / <alpha-value>)",
          600: "rgb(var(--c-border-2) / <alpha-value>)",
        },
        brand: {
          400: "rgb(var(--c-accent-h) / <alpha-value>)",
          500: "rgb(var(--c-accent) / <alpha-value>)",
          600: "rgb(var(--c-accent-p) / <alpha-value>)",
        },
        // 文本/边框语义色随主题翻转
        slate: {
          200: "rgb(var(--c-t-200) / <alpha-value>)",
          300: "rgb(var(--c-t-300) / <alpha-value>)",
          400: "rgb(var(--c-t-400) / <alpha-value>)",
          500: "rgb(var(--c-t-500) / <alpha-value>)",
          600: "rgb(var(--c-t-600) / <alpha-value>)",
          700: "rgb(var(--c-t-700) / <alpha-value>)",
        },
        white: "rgb(var(--c-t-strong) / <alpha-value>)",
        // shadcn 语义色 → 映射到现有 brand 主题变量
        background: "rgb(var(--c-bg) / <alpha-value>)",
        foreground: "rgb(var(--c-t-200) / <alpha-value>)",
        card: {
          DEFAULT: "rgb(var(--c-surface) / <alpha-value>)",
          foreground: "rgb(var(--c-t-200) / <alpha-value>)",
        },
        popover: {
          DEFAULT: "rgb(var(--c-surface-2) / <alpha-value>)",
          foreground: "rgb(var(--c-t-200) / <alpha-value>)",
        },
        primary: {
          DEFAULT: "rgb(var(--c-accent) / <alpha-value>)",
          foreground: "rgb(255 255 255 / <alpha-value>)",
        },
        secondary: {
          DEFAULT: "rgb(var(--c-surface-2) / <alpha-value>)",
          foreground: "rgb(var(--c-t-300) / <alpha-value>)",
        },
        muted: {
          DEFAULT: "rgb(var(--c-surface-2) / <alpha-value>)",
          foreground: "rgb(var(--c-t-400) / <alpha-value>)",
        },
        accent: {
          DEFAULT: "rgb(var(--c-surface-2) / <alpha-value>)",
          foreground: "rgb(var(--c-t-200) / <alpha-value>)",
        },
        destructive: {
          DEFAULT: "rgb(239 68 68 / <alpha-value>)",
          foreground: "rgb(255 255 255 / <alpha-value>)",
        },
        border: "rgb(var(--c-border) / <alpha-value>)",
        input: "rgb(var(--c-border-2) / <alpha-value>)",
        ring: "rgb(var(--c-accent) / <alpha-value>)",
      },
      fontFamily: {
        display: ["Outfit", "system-ui", "sans-serif"],
        body: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      // 动画体系：入场/氛围统一 cubic-bezier，只动 opacity/transform（GPU 合成层）
      keyframes: {
        rise: {
          "0%": { opacity: "0", transform: "translateY(14px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
      },
      animation: {
        rise: "rise 0.55s cubic-bezier(0.22, 1, 0.36, 1) both",
        "fade-in": "fade-in 0.4s ease-out both",
      },
    },
  },
  plugins: [require("tailwindcss-animate")],
};
