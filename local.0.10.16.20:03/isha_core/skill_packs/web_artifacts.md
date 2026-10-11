# Web Artifacts Builder

Build polished, self-contained web pages and UI designs.

- Output ONE complete HTML document: semantic HTML, CSS inside `<style>`, JavaScript inside `<script>`.
- No external files, CDN links, web fonts, trackers or network requests - the page must work offline in a sandboxed preview. Use inline SVG or CSS for icons and images.
- Structure the UI as clear components/sections (header, main content, cards, forms, footer) with consistent spacing, a small colour palette and good contrast.
- Responsive layout (flex/grid, works at 360px and at desktop width), keyboard-accessible controls, labels on inputs, alt text.
- React, Tailwind or shadcn/ui style code only when the user asks; note that they then need their own build setup.
- Keep JavaScript small and readable; no eval, no storage of personal data.
