# Traceon brand

A recorded run is a timeline. Replay walks it again. Traceon shows the point
where two timelines stop agreeing. The mark draws exactly that: one track, a
marked point, and a second track that leaves it.

## Files

| File | Use |
|---|---|
| `mark.svg` | The mark alone, for light backgrounds (24 × 24 grid). |
| `logo.svg` | Mark + wordmark, for light backgrounds. |
| `logo-dark.svg` | Mark + wordmark, for dark backgrounds. |
| `favicon.svg` | 16 × 16 grid on a graphite tile, so it reads on light and dark browser chrome. Also shipped as `src/traceon/studio/static/favicon.svg`. |
| `social-preview.svg` | 1280 × 640 GitHub social card. Export to PNG for upload. |

The wordmark is drawn as stroked paths (monoline, 2.4-unit stroke on a 14-unit
x-height), so it renders the same everywhere and does not depend on installed
fonts. Only `social-preview.svg` uses `<text>` (tagline, slug, waterfall labels),
with system font stacks; it is meant to be rasterized once.

## Palette

Neutral graphite and paper, one accent. The accent marks divergence and
nothing else that competes with it. Every text color below meets WCAG AA
(4.5:1) against both `surface` and `bg` of its theme.

| Token | Light | Dark | Role |
|---|---|---|---|
| `bg` | `#F7F7F5` | `#121315` | Page background (paper / graphite) |
| `surface` | `#FFFFFF` | `#18191C` | Panels, tables |
| `surface-2` | `#F1F1EE` | `#1E1F23` | Header strips |
| `fg` | `#1B1C1E` | `#E3E3E0` | Primary text, mark base track |
| `fg-muted` | `#5A5E66` | `#A3A6AC` | Labels, secondary text |
| `fg-subtle` | `#6C7078` | `#8D9097` | Axis ticks, placeholders |
| `border` | `#DEDED9` | `#2B2D31` | 1px hairlines |
| `grid` | `#EDEDE9` | `#222428` | Timeline gridlines |
| `selection` | `#E3E9F1` | `#233042` | Selected row |
| **`accent`** | **`#B04A0C`** | **`#E89242`** | Signal amber: divergence, mark branch |
| `tool` | `#17767C` | `#55B2B8` | Tool calls (text); bar `#2E8C92` / `#3F9DA3` |
| `model` | `#3A5FA0` | `#86A6DC` | Model calls (text); bar `#5579B8` / `#6A8DC9` |
| `value` | `#6A7320` | `#B3BC62` | Nondeterministic values (text); bar `#858F35` / `#98A24C` |
| `span` | `#666B74` | `#8F939A` | Structural spans (text); bar `#B9BCC2` / `#4A4D53` |
| `ok` | `#2D7444` | `#62B07D` | Success, added |
| `error` | `#BA3328` | `#EC746A` | Failure, removed |
| `divergence` | = accent | = accent | First divergence, changed calls |

Status is never color-only: `ok` is a square, `error` a diamond, `cancelled`
an outline; payload states differ by border style and label text.

## Type

- UI: `system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`, 12.5 px base.
- Data: `ui-monospace, "SF Mono", SFMono-Regular, "Cascadia Mono", "Segoe UI Mono", Menlo, Consolas, "Liberation Mono", monospace`, 12 px.
- `font-variant-numeric: tabular-nums` everywhere numbers line up.
- No web fonts.

## Clear space and size

- Keep clear space around the logo equal to half the mark's height on every side.
- Minimum sizes: mark 16 px (use `favicon.svg` below 24 px), logo 96 px wide.
- The wordmark is lowercase `traceon`. In running text, write "Traceon" (product) or "Traceon Replay" (package).

## Do

- Use `logo.svg` on light, `logo-dark.svg` on dark, at their original proportions.
- Keep the accent for the branch and the divergence point.
- Pair the mark with real data views (waterfalls, tables) rather than illustrations.

## Don't

- Recolor the mark, swap the accent for another hue, or use the accent for the base track.
- Add gradients, glows, shadows, outlines, or rounded containers behind the logo.
- Rotate, skew, stretch, or animate the mark.
- Set the wordmark in a different font, or rebuild it with `<text>`.
- Put the logo on busy imagery or on mid-tone backgrounds that fail contrast.
