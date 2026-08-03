"""Mux slides and narration into a finished video.

Slide durations come from the measured length of each beat's audio, so picture
and voice cannot drift apart over a long script the way a fixed
seconds-per-slide assumption does.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..config import SETTINGS, RenderPolicy
from .deck import RenderError, Slide, rasterize
from .tts_elevenlabs import Utterance, concat


@dataclass(frozen=True)
class Composition:
    video_path: Path
    audio_path: Path
    duration_s: float


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if not binary:
        raise RenderError("ffmpeg is required to compose video")
    return binary


def compose(
    slides: Sequence[Slide],
    utterances: Sequence[Utterance],
    outdir: Path,
    policy: RenderPolicy | None = None,
) -> Composition:
    """Render slides to PNG, build a slideshow, and mux the narration."""
    policy = policy or SETTINGS.render
    if len(slides) != len(utterances):
        raise RenderError(
            f"{len(slides)} slides against {len(utterances)} utterances"
        )
    outdir = Path(outdir)
    frames_dir = outdir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    entries: list[str] = []
    for slide, utterance in zip(slides, utterances):
        if slide.path is None:
            raise RenderError(f"slide {slide.index} was never written to disk")
        png = rasterize(slide.path, frames_dir / f"{slide.index:02d}.png", policy)
        entries.append(f"file '{png.resolve()}'")
        entries.append(f"duration {max(utterance.duration_s, 0.4):.3f}")
    # The concat demuxer drops the final image without a repeated entry.
    entries.append(entries[-2])

    listing = outdir / "slides.txt"
    listing.write_text("\n".join(entries), encoding="utf-8")

    audio_path = concat([u.path for u in utterances], outdir / "narration.wav")
    video_path = outdir / "video.mp4"

    subprocess.run(
        [
            _ffmpeg(), "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-i", str(audio_path),
            "-vf", f"fps={policy.fps},format=yuv420p,scale={policy.width}:{policy.height}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(video_path),
        ],
        check=True,
        capture_output=True,
    )

    return Composition(
        video_path=video_path,
        audio_path=audio_path,
        duration_s=round(sum(u.duration_s for u in utterances), 2),
    )


def overlay_avatar(
    base_video: Path,
    avatar_video: Path,
    destination: Path,
    policy: RenderPolicy | None = None,
    corner: str = "br",
    scale: float = 0.28,
) -> Path:
    """Composite the talking-head render as a corner inset over the slides."""
    policy = policy or SETTINGS.render
    inset_w = int(policy.width * scale)
    margin = int(policy.width * 0.025)
    position = {
        "br": f"main_w-overlay_w-{margin}:main_h-overlay_h-{margin}",
        "bl": f"{margin}:main_h-overlay_h-{margin}",
        "tr": f"main_w-overlay_w-{margin}:{margin}",
        "tl": f"{margin}:{margin}",
    }[corner]

    subprocess.run(
        [
            _ffmpeg(), "-y", "-i", str(base_video), "-i", str(avatar_video),
            "-filter_complex",
            f"[1:v]scale={inset_w}:-2[av];[0:v][av]overlay={position}",
            "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "copy", str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return destination
