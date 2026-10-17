import colors from 'tailwindcss/colors'

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Gains and losses are written as `text-green-600 dark:text-green-400` and
        // `text-red-600 dark:text-red-400` at ~70 call sites and pinned by several tests.
        // Rather than rename them, the two scales are re-pointed at emerald and rose —
        // calmer on the new canvas and still 4.5:1 on both themes.
        green: { ...colors.green, 400: colors.emerald[400], 500: colors.emerald[500], 600: colors.emerald[700] },
        red: { ...colors.red, 400: colors.rose[400], 500: colors.rose[500], 600: colors.rose[600] },
        yellow: { ...colors.yellow, 400: colors.amber[400], 500: colors.amber[500], 600: colors.amber[700] },
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },
        raised: "hsl(var(--raised))",
        "border-strong": "hsl(var(--border-strong))",
        // Semantic state (index.css). `text-gain` is `text-green-600 dark:text-green-400`
        // in one class; the older pair stays valid and the two are pinned equal.
        gain: { DEFAULT: "hsl(var(--gain))", soft: "hsl(var(--gain-soft))" },
        loss: { DEFAULT: "hsl(var(--loss))", soft: "hsl(var(--loss-soft))" },
        warn: { DEFAULT: "hsl(var(--warn))", soft: "hsl(var(--warn-soft))" },
        info: { DEFAULT: "hsl(var(--info))", soft: "hsl(var(--info-soft))" },
        // The categorical chart palette, hex per theme in index.css: `bg-chart-3` for a
        // legend swatch. No opacity modifier (the values are hex, not HSL triples).
        chart: {
          1: "var(--chart-1)", 2: "var(--chart-2)", 3: "var(--chart-3)", 4: "var(--chart-4)",
          5: "var(--chart-5)", 6: "var(--chart-6)", 7: "var(--chart-7)", 8: "var(--chart-8)",
          9: "var(--chart-9)", 10: "var(--chart-10)", other: "var(--chart-other)",
        },
      },
      borderRadius: {
        lg: "var(--radius)",
        md: "calc(var(--radius) - 2px)",
        sm: "calc(var(--radius) - 4px)",
      },
      // Theme-aware elevation from index.css: a soft lift in light mode, none in dark,
      // where a lighter surface carries it instead.
      boxShadow: {
        card: "var(--shadow-card)",
        pop: "var(--shadow-pop)",
        raised: "var(--shadow-raised)",
      },
    },
  },
  plugins: [],
}
