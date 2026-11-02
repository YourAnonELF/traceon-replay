# Launch film

`index.html` is the source of the launch film. `render.py` turns it into
`docs/assets/traceon-launch.mp4`, `docs/assets/demo-replay.gif` and
`docs/assets/traceon-launch-poster.png`.

The page has no animations of its own. `window.seek(t)` computes the whole frame
for time `t` (seconds), so every render produces the same frames. Everything
is self-contained: no CDN and no web fonts (system stacks: Segoe UI / Cascadia
Mono, with fallbacks).

| File | Contents |
|---|---|
| `index.html` | Stage (1920 × 1080), script loading, `window.seek`, preview playback |
| `style.css` | Palette (from `brand/README.md`) and shared components |
| `scenes/timeline.js` | Scene durations, captions, chapter index. Retime here. |
| `scenes/lib.js` | Easing, path drawing, payload chips, typed text |
| `scenes/chrome.js` | Logo (built in the open, parked lower-left, back for the end card), captions, chapters |
| `scenes/open.js`, `system.js`, `diverge.js`, `ci.js`, `end.js` | The scenes |

| Time | Scene |
|---|---|
| 0 – 4.6 s | Open: the mark builds, tagline |
| 4.6 – 14.1 s | Incident: the agent calls its services and fails parsing the ETA |
| 14.1 – 24.1 s | Record: the recorder copies each call into the fixture; secret redaction, `hash_only` model request |
| 24.1 – 34.1 s | Replay offline: services cut off, a connection attempt is blocked, responses come from the fixture tape, the same `ValueError` |
| 34.1 – 47.1 s | Divergence: the fixed parser keeps the tracks together (status error → ok); the `order_id.lower()` refactor leaves at call #0 |
| 47.1 – 54.1 s | pytest: the regression test and its failure report |
| 54.1 – 60.1 s | End card |

The data shown comes from `examples/failed_tool_call`: its fixture, the output
of `traceon inspect` / `traceon replay`, and `pytest -v` with the traceon
plugin. If that example or the CLI output changes, update the strings in
`scenes/system.js`, `scenes/diverge.js` and `scenes/ci.js`.

## Preview

Open `index.html` in a browser. It plays in a loop (space pauses). Add
`?t=30.9` to the URL to show a single frame.

## Render

```bash
pip install playwright imageio-ffmpeg pillow   # uses the installed Chrome
python docs/video/render.py                    # mp4 + gif + poster into docs/assets/
```

A full render takes under a minute with 6 workers. Other modes write to a
temporary directory unless `--out` is given:

```bash
python docs/video/render.py --scene replay --preview          # one scene, 960x540
python docs/video/render.py --from 34 --to 47 --out clip.mp4  # a time range
python docs/video/render.py --stills 13,21.2,30.9,45.4 --out stills/
python docs/video/render.py --contact sheet.png --every 2 --preview
```

Options: `--fps` (default 30), `--workers`, `--keep-frames DIR`, `--chromium`
(use Playwright's bundled Chromium instead of the installed Chrome).

Output settings: H.264 High, yuv420p, BT.709, CRF 18, `+faststart`, no audio
(LinkedIn, X and GitHub accept it). The GIF is three cuts (end of recording,
offline replay, the fork) at 15 fps and 960 px wide, with a 192-color palette.
Its segments and the poster time are set at the top of `render.py`; scene
boundaries there must match `scenes/timeline.js`.
