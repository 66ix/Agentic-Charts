import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        base: "#0b0e14",
        panel: "#11151c",
        panel2: "#161b24",
        line: "#1f2633",
        ink: "#d1d5db",
        mute: "#6b7280",
        up: "#22c55e",
        down: "#ef4444",
        accent: "#3b82f6",
      },
      fontFamily: {
        sans: ["var(--font-sans)", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "SFMono-Regular", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
