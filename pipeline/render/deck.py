"""Slide generation for the YouTube presentation format.

Slides are emitted as SVG -- text, no binary dependency, diffable in review,
and rasterised only at the end. The claim slide is built differently from every
other slide: it carries its sources on screen, because a viewer should be able
to check the one factual statement in the video without leaving it.
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..config import SETTINGS, RenderPolicy
from ..contracts import AtlasClaim, Beat, Script
from .pngtools import PngError, crop_top_left, read_png

# Brand tokens. Replace with the real TAMHA / Yahalom palette and typefaces
# once the brand kit lands; nothing else in the module hard-codes appearance.
THEME = {
    "bg": "#0d1117",
    "bg_claim": "#141b2d",
    "fg": "#f2f4f8",
    "muted": "#9aa4b2",
    "accent": "#e8b33c",
    "font": "Inter, 'Noto Sans', 'DejaVu Sans', sans-serif",
}


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class Slide:
    index: int
    role: str
    lines: tuple[str, ...]
    footnote: str
    duration_s: float
    path: Path | None = None


def wrap(text: str, width: int) -> list[str]:
    """Greedy wrap by character budget. Good enough for large display type."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = len(" ".join([*current, word]))
        if current and candidate > width:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines or [""]


def build_slides(
    script: Script,
    claim: AtlasClaim,
    durations: Sequence[float],
) -> list[Slide]:
    if len(durations) != len(script.beats):
        raise RenderError(
            f"{len(durations)} durations for {len(script.beats)} beats"
        )
    slides: list[Slide] = []
    for index, (beat, duration) in enumerate(zip(script.beats, durations)):
        slides.append(
            Slide(
                index=index,
                role=beat.role,
                lines=tuple(wrap(beat.text, 34 if beat.role == "claim" else 28)),
                footnote=_footnote(beat, claim),
                duration_s=float(duration),
            )
        )
    return slides


def _footnote(beat: Beat, claim: AtlasClaim) -> str:
    if beat.role != "claim":
        return ""
    return " · ".join(
        f"{source.tractate} {source.folio}" for source in claim.sources
    )


def slide_svg(slide: Slide, policy: RenderPolicy | None = None) -> str:
    policy = policy or SETTINGS.render
    width, height = policy.width, policy.height
    is_claim = slide.role == "claim"
    size = 64 if is_claim else 78
    leading = int(size * 1.35)
    block = leading * len(slide.lines)
    top = (height - block) // 2 + size

    body = "\n".join(
        f'    <text x="{width // 2}" y="{top + i * leading}" '
        f'text-anchor="middle" font-size="{size}" font-family="{THEME["font"]}" '
        f'font-weight="{600 if is_claim else 800}" fill="{THEME["fg"]}">'
        f"{html.escape(line)}</text>"
        for i, line in enumerate(slide.lines)
    )

    accent = (
        f'    <rect x="{width // 2 - 240}" y="{top - size - 56}" width="480" '
        f'height="6" rx="3" fill="{THEME["accent"]}"/>'
        if is_claim
        else ""
    )
    footnote = (
        f'    <text x="{width // 2}" y="{height - 88}" text-anchor="middle" '
        f'font-size="34" font-family="{THEME["font"]}" fill="{THEME["muted"]}">'
        f"{html.escape(slide.footnote)}</text>"
        if slide.footnote
        else ""
    )

    return "\n".join(
        part
        for part in (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}">',
            f'    <rect width="{width}" height="{height}" fill="'
            f'{THEME["bg_claim"] if is_claim else THEME["bg"]}"/>',
            accent,
            body,
            footnote,
            "</svg>",
        )
        if part
    )


def write_deck(
    slides: Sequence[Slide], outdir: Path, policy: RenderPolicy | None = None
) -> list[Slide]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Slide] = []
    for slide in slides:
        path = outdir / f"{slide.index:02d}_{slide.role}.svg"
        path.write_text(slide_svg(slide, policy), encoding="utf-8")
        written.append(
            Slide(
                index=slide.index,
                role=slide.role,
                lines=slide.lines,
                footnote=slide.footnote,
                duration_s=slide.duration_s,
                path=path,
            )
        )
    manifest = outdir / "deck.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "index": s.index,
                    "role": s.role,
                    "duration_s": s.duration_s,
                    "svg": s.path.name if s.path else "",
                    "lines": list(s.lines),
                    "footnote": s.footnote,
                }
                for s in written
            ],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return written


def rasterize(svg_path: Path, png_path: Path, policy: RenderPolicy | None = None) -> Path:
    """SVG -> PNG through whichever converter the host actually has.

    Order is by fidelity for text-heavy slides: rsvg and Inkscape do real font
    shaping; headless Chromium is the fallback that is present on more boxes.
    """
    policy = policy or SETTINGS.render
    png_path.parent.mkdir(parents=True, exist_ok=True)

    attempts: list[list[str]] = []
    if shutil.which("rsvg-convert"):
        attempts.append(
            ["rsvg-convert", "-w", str(policy.width), "-h", str(policy.height),
             "-o", str(png_path), str(svg_path)]
        )
    if shutil.which("inkscape"):
        attempts.append(
            ["inkscape", str(svg_path), f"--export-filename={png_path}",
             f"--export-width={policy.width}"]
        )
    browser = _find_browser()
    wrapper: Path | None = None
    crop_after: bool = False
    if browser:
        # Chromium loading an SVG directly treats it as a scrollable document:
        # scrollbars steal width and the bottom of the slide -- the source card
        # -- is cropped away. Wrapping it in a non-scrolling page fixes that,
        # but the layout viewport is still shorter than the requested window,
        # so the window is oversized by the measured deficit and the screenshot
        # trimmed back to the artboard afterwards.
        wrapper = png_path.with_suffix(".html")
        wrapper.write_text(_browser_wrapper(svg_path), encoding="utf-8")
        deficit = _viewport_deficit(browser)
        attempts.append(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--screenshot={png_path}",
             f"--window-size={policy.width},{policy.height + deficit}",
             f"file://{wrapper.resolve()}"]
        )
        crop_after = True

    errors: list[str] = []
    try:
        for command in attempts:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False
            )
            if result.returncode == 0 and png_path.exists():
                if crop_after and command[0] == browser:
                    crop_top_left(png_path, policy.width, policy.height)
                return png_path
            errors.append(f"{command[0]}: {result.stderr.strip()[:200]}")
    finally:
        if wrapper is not None:
            wrapper.unlink(missing_ok=True)

    raise RenderError(
        "no working SVG rasteriser found (tried rsvg-convert, inkscape, "
        f"chromium). Install librsvg2-bin on the server. Details: {errors}"
    )


def _browser_wrapper(svg_path: Path) -> str:
    """A non-scrolling page holding the slide inline, sized to the viewport.

    ``100vw``/``100vh`` rather than pixel dimensions: paired with the measured
    window oversize, the artboard lands exactly in the top-left of the shot.
    """
    svg = Path(svg_path).read_text(encoding="utf-8")
    return (
        "<!doctype html><meta charset='utf-8'><style>"
        f"html,body{{margin:0;padding:0;overflow:hidden;background:{THEME['bg']};}}"
        "svg{display:block;width:100vw;height:100vh;}"
        f"</style>{svg}"
    )


# Probing costs one browser launch, so the answer is cached per binary.
_VIEWPORT_DEFICIT: dict[str, int] = {}

_PROBE_PAGE = (
    "<!doctype html><meta charset='utf-8'><style>"
    "html,body{margin:0;padding:0;overflow:hidden;background:#000;}"
    "#p{position:fixed;top:0;left:0;width:100vw;height:100vh;background:#f00;}"
    "</style><div id=p></div>"
)
_PROBE_HEIGHT = 600


def _viewport_deficit(browser: str) -> int:
    """How many pixels shorter the layout viewport is than the window.

    Headless Chromium lays out in a viewport smaller than ``--window-size``
    while still screenshotting the full window height, and the difference has
    changed between versions. Measuring beats hard-coding it: a wrong constant
    would silently crop the citation off the claim slide.
    """
    if browser in _VIEWPORT_DEFICIT:
        return _VIEWPORT_DEFICIT[browser]

    deficit = 0
    with tempfile.TemporaryDirectory() as tmp:
        probe_html = Path(tmp) / "probe.html"
        probe_png = Path(tmp) / "probe.png"
        probe_html.write_text(_PROBE_PAGE, encoding="utf-8")
        result = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--hide-scrollbars", "--force-device-scale-factor=1",
             f"--screenshot={probe_png}", f"--window-size=200,{_PROBE_HEIGHT}",
             f"file://{probe_html.resolve()}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and probe_png.exists():
            try:
                image = read_png(probe_png)
                filled = sum(
                    1
                    for y in range(image.height)
                    if image.pixel(image.width // 2, y)[:3] == (255, 0, 0)
                )
                if 0 < filled <= _PROBE_HEIGHT:
                    deficit = _PROBE_HEIGHT - filled
            except PngError:
                deficit = 0

    _VIEWPORT_DEFICIT[browser] = deficit
    return deficit


def _find_browser() -> str | None:
    """Locate Chromium on PATH, or in a Playwright browser bundle.

    Playwright installs to ``PLAYWRIGHT_BROWSERS_PATH`` rather than PATH, so a
    host with a perfectly good Chromium looks like it has none.
    """
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    bundle_root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers"))
    if bundle_root.is_dir():
        candidates = sorted(bundle_root.glob("chromium*/chrome-linux/chrome"))
        if candidates:
            return str(candidates[-1])
    return None
