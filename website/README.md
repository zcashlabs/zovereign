# Zovereign website

A static, buildless landing page for Zovereign. Everything the site serves is
in `dist/`:

| File | Purpose |
| --- | --- |
| `dist/index.html` | The page, with its illustrations drawn as inline SVG. |
| `dist/styles.css` | All styles, including responsive layout and reduced-motion rules. |
| `dist/site.js` | Optional enhancements: copy buttons, install tabs, blueprint tracing, mobile-menu closing and scroll reveals. |
| `dist/favicon.svg` | Site icon. |

There is no build step, package manifest or framework. The page works without
JavaScript: both install methods show in full, the FAQ uses native
`<details>`, and the mobile menu is a `<details>` disclosure. Fonts come from
Google Fonts (Fraunces, Instrument Sans, JetBrains Mono), with system serif,
sans-serif and monospace fallbacks if they fail to load.

## Preview locally

Serve `dist/` with any static file server, for example:

```bash
python3 -m http.server --directory website/dist 8000
```

Then open <http://127.0.0.1:8000/>. Opening `dist/index.html` directly also
works, but browsers only allow the Clipboard API on secure origins (HTTPS or
`localhost`). On other origins the copy buttons fall back to
`document.execCommand('copy')`, or select the command so it can be copied
manually.

## Deploy

Upload the contents of `dist/` to any static host. All asset paths are
relative, so the site also works from a subdirectory.

## Editing guidelines

- Keep claims consistent with `README.md` and `docs/single-container.md`.
  Measured results (byte counts, block count, test count, timings) are dated
  evidence from 14 November 2026. Present them as dated evidence, not live
  metrics or guarantees.
- Don't describe the stack as trustless. Snapshot checksums protect
  integrity, not publisher authenticity.
- Never show `docker compose down -v` as an ordinary stop command. It
  deletes the persistent volume.
- The hero's isometric drawing was generated once from isometric coordinates
  and pasted inline. Edit the SVG directly. Classes such as `.zak-t`
  (top face), `.zak-l` (left face) and `.zak-r` (right face) map each face to
  a colour token in `styles.css`.
