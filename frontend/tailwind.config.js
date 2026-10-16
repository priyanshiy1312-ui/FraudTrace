/** Palette: an ink-blue base, a three-step risk ramp (steel -> amber -> coral) that carries meaning,
 *  teal for "the evidence the agent used", violet for "came from memory". */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#0d141c', panel: '#131c27', raised: '#1a2635', line: '#25354a',
        text: '#e5ecf4', mute: '#8798ad',
        steel: '#6f8fae', amber: '#e5b23c', coral: '#f0646b',
        evidence: '#3fd0b6', memory: '#a08bff',
      },
      fontFamily: {
        sans: ['"Instrument Sans"', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
}
