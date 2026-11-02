"""Render the Traceon Replay launch film from docs/video/index.html.

The page exposes ``window.seek(t)``, which draws the frame for time ``t``
deterministically. This script opens the page in headless Chrome (Playwright),
seeks every frame, screenshots it and encodes the result with ffmpeg.

Full render (writes the three files in docs/assets/):

    python docs/video/render.py

    docs/assets/traceon-launch.mp4         1920x1080, 30 fps, H.264, yuv420p, no audio
    docs/assets/demo-replay.gif            960 px wide cut of record -> replay -> divergence
    docs/assets/traceon-launch-poster.png  a single 1920x1080 frame

Partial renders go to a temporary directory unless --out is given:

    python docs/video/render.py --scene replay --preview
    python docs/video/render.py --from 30 --to 40 --out clip.mp4
    python docs/video/render.py --stills 12.5,27,41 --out stills/
    python docs/video/render.py --contact sheet.png --every 2 --preview

Requirements: ``pip install playwright imageio-ffmpeg pillow`` and a Chrome
install (the script uses ``channel="chrome"``; pass --chromium to use
Playwright's bundled Chromium after ``playwright install chromium``).
"""

from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
ASSETS = REPO / "docs" / "assets"
PAGE = (HERE / "index.html").as_uri() + "?render=1"
W, H = 1920, 1080

# Scene boundaries in seconds; keep in sync with scenes/timeline.js.
SCENES = {
    "open": (0.0, 4.6),
    "problem": (4.6, 14.1),
    "record": (14.1, 24.1),
    "replay": (24.1, 34.1),
    "divergence": (34.1, 47.1),
    "ci": (47.1, 54.1),
    "end": (54.1, 60.1),
}
# The GIF: end of recording, the offline replay, the fork. (start, end) pairs.
GIF_SEGMENTS = [(19.4, 22.6), (25.0, 32.3), (40.2, 45.8)]
GIF_FPS = 15
POSTER_T = 45.4


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        found = shutil.which("ffmpeg")
        if not found:
            sys.exit("ffmpeg not found: pip install imageio-ffmpeg")
        return found


def _worker(args: tuple) -> int:
    """Render frames [indices] to out_dir/%05d.png in one browser."""
    indices, times, out_dir, scale, chromium = args
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch() if chromium else p.chromium.launch(channel="chrome")
        page = browser.new_page(viewport={"width": W, "height": H}, device_scale_factor=scale)
        page.goto(PAGE)
        page.wait_for_function("window.ready !== undefined")
        page.evaluate("window.ready")
        page.evaluate("document.fonts.ready.then(() => true)")
        for i, t in zip(indices, times, strict=False):
            page.evaluate("t => window.seek(t)", t)
            page.screenshot(
                path=str(Path(out_dir) / f"{i:05d}.png"), type="png", animations="disabled"
            )
        browser.close()
    return len(indices)


def render_frames(
    times: list[float], out_dir: Path, *, scale: float, workers: int, chromium: bool
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    n = len(times)
    workers = max(1, min(workers, n))
    jobs = []
    for k in range(workers):
        idx = list(range(k, n, workers))
        jobs.append((idx, [times[i] for i in idx], str(out_dir), scale, chromium))
    t0 = time.time()
    if workers == 1:
        _worker(jobs[0])
    else:
        with mp.get_context("spawn").Pool(workers) as pool:
            pool.map(_worker, jobs)
    print(f"rendered {n} frames in {time.time() - t0:.1f}s ({workers} workers)")


def encode_mp4(frames: Path, fps: int, out: Path, crf: int = 18) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-i",
        str(frames / "%05d.png"),
        "-vf",
        "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        str(crf),
        "-tune",
        "animation",
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-colorspace",
        "bt709",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-color_range",
        "tv",
        "-r",
        str(fps),
        "-movflags",
        "+faststart",
        "-an",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")


def encode_gif(
    frames: Path, fps: int, start: float, out: Path, segments: list[tuple[float, float]]
) -> None:
    """Cut segments out of a frame sequence that starts at time `start`."""
    parts = []
    for i, (a, b) in enumerate(segments):
        parts.append(
            f"[0:v]trim=start={a - start:.3f}:end={b - start:.3f},setpts=PTS-STARTPTS[s{i}]"
        )
    chain = ";".join(parts)
    chain += (
        ";"
        + "".join(f"[s{i}]" for i in range(len(segments)))
        + f"concat=n={len(segments)}:v=1:a=0[c]"
    )
    chain += f";[c]fps={GIF_FPS},scale=960:-1:flags=lanczos,split[x][y]"
    chain += ";[x]palettegen=max_colors=192:stats_mode=full:reserve_transparent=0[pal]"
    chain += ";[y][pal]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    cmd = [
        ffmpeg_exe(),
        "-y",
        "-loglevel",
        "error",
        "-framerate",
        str(fps),
        "-i",
        str(frames / "%05d.png"),
        "-filter_complex",
        chain,
        "-loop",
        "0",
        str(out),
    ]
    subprocess.run(cmd, check=True)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.2f} MB)")


def contact_sheet(frames: list[Path], labels: list[str], out: Path, cols: int = 4) -> None:
    from PIL import Image, ImageDraw

    ims = [Image.open(f).convert("RGB") for f in frames]
    tw = 480
    th = round(ims[0].height * tw / ims[0].width)
    rows = math.ceil(len(ims) / cols)
    sheet = Image.new("RGB", (cols * tw + (cols + 1) * 8, rows * (th + 26) + 8), (40, 40, 44))
    d = ImageDraw.Draw(sheet)
    for i, (im, lab) in enumerate(zip(ims, labels, strict=False)):
        x = 8 + (i % cols) * (tw + 8)
        y = 8 + (i // cols) * (th + 26)
        sheet.paste(im.resize((tw, th), Image.LANCZOS), (x, y + 18))
        d.text((x, y + 2), lab, fill=(220, 220, 220))
    sheet.save(out)
    print(f"wrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--scene", choices=sorted(SCENES), help="render one scene")
    ap.add_argument("--from", dest="t_from", type=float, help="start time (s)")
    ap.add_argument("--to", dest="t_to", type=float, help="end time (s)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--preview", action="store_true", help="half resolution (960x540), fast")
    ap.add_argument("--stills", help="comma-separated times; write one PNG per time")
    ap.add_argument("--contact", help="write a contact sheet PNG (one frame every --every s)")
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--out", help="output file or directory for partial renders")
    ap.add_argument("--workers", type=int, default=max(1, min(6, (os.cpu_count() or 2) - 1)))
    ap.add_argument("--keep-frames", help="keep the rendered PNG frames in this directory")
    ap.add_argument("--chromium", action="store_true", help="use Playwright's bundled Chromium")
    a = ap.parse_args()

    total = SCENES["end"][1]
    scale = 0.5 if a.preview else 1.0
    tmp = Path(tempfile.mkdtemp(prefix="traceon-video-"))
    try:
        if a.stills or a.contact:
            if a.stills:
                ts = [float(x) for x in a.stills.split(",") if x.strip()]
            else:
                lo = a.t_from or 0.0
                hi = a.t_to if a.t_to is not None else total
                ts = [lo + i * a.every for i in range(int((hi - lo) / a.every) + 1)]
            render_frames(ts, tmp, scale=scale, workers=a.workers, chromium=a.chromium)
            files = [tmp / f"{i:05d}.png" for i in range(len(ts))]
            if a.contact:
                contact_sheet(files, [f"{t:.1f}s" for t in ts], Path(a.contact))
            else:
                out = Path(a.out or HERE / "stills")
                out.mkdir(parents=True, exist_ok=True)
                for t, f in zip(ts, files, strict=False):
                    shutil.copy(f, out / f"frame_{t:06.2f}.png")
                print(f"wrote {len(files)} stills to {out}")
            return

        partial = bool(a.scene or a.t_from is not None or a.t_to is not None or a.preview)
        lo, hi = SCENES[a.scene] if a.scene else (0.0, total)
        if a.t_from is not None:
            lo = a.t_from
        if a.t_to is not None:
            hi = a.t_to
        n = round((hi - lo) * a.fps)
        times = [lo + i / a.fps for i in range(n)]
        frames = Path(a.keep_frames) if a.keep_frames else tmp / "frames"
        render_frames(times, frames, scale=scale, workers=a.workers, chromium=a.chromium)

        if partial:
            out = (
                Path(a.out)
                if a.out
                else tmp.parent
                / f"traceon-{a.scene or 'clip'}{'-preview' if a.preview else ''}.mp4"
            )
            encode_mp4(frames, a.fps, out)
            return

        ASSETS.mkdir(parents=True, exist_ok=True)
        encode_mp4(frames, a.fps, ASSETS / "traceon-launch.mp4")
        encode_gif(frames, a.fps, lo, ASSETS / "demo-replay.gif", GIF_SEGMENTS)
        poster = round((POSTER_T - lo) * a.fps)
        shutil.copy(frames / f"{poster:05d}.png", ASSETS / "traceon-launch-poster.png")
        print(f"wrote {ASSETS / 'traceon-launch-poster.png'} (t={POSTER_T}s)")
        print(f"duration {n / a.fps:.2f}s, {n} frames")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
