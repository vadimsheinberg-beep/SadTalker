"""Talking-head rendering.

SadTalker is the default engine and the reason this pipeline lives in this
repository. Its licence was relicensed to Apache 2.0 with the non-commercial
restriction removed (see README.md:42 and LICENSE), which makes it usable on
monetised channels without the ambiguity hanging over the MuseTalk option --
where the project README and the HuggingFace model card do not agree, and the
recorded policy currently assumes the permissive reading. That assumption is
unresolved and is not relied on here.

The engine is swappable behind :class:`AvatarEngine` so the question can be
settled later without touching callers.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

REPO_ROOT = Path(__file__).resolve().parents[2]


class AvatarError(RuntimeError):
    pass


@dataclass(frozen=True)
class AvatarRequest:
    """Inputs for one render.

    ``source`` is the still or first frame of the presenter. ``audio`` is the
    narration track; SadTalker drives lip motion from it directly, so it must
    be the same audio the final mux uses or the sync will be wrong.
    """

    source: Path
    audio: Path
    outdir: Path
    still: bool = True
    preprocess: str = "full"
    size: int = 256
    enhancer: str | None = "gfpgan"
    expression_scale: float = 1.0
    pose_style: int = 0
    ref_eyeblink: Path | None = None
    ref_pose: Path | None = None


class AvatarEngine(Protocol):
    def render(self, request: AvatarRequest) -> Path: ...


class SadTalkerEngine:
    """Drives ``inference.py`` as a subprocess.

    A subprocess rather than an import: SadTalker loads several torch models at
    module scope and holds GPU memory for the process lifetime, which is wrong
    for a control-plane worker that spends most of its time waiting on APIs.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        checkpoint_dir: Path | None = None,
        python: str | None = None,
        cpu: bool = False,
    ) -> None:
        self.repo_root = Path(repo_root or REPO_ROOT)
        self.checkpoint_dir = Path(checkpoint_dir or self.repo_root / "checkpoints")
        self.python = python or sys.executable
        self.cpu = cpu

    def preflight(self) -> list[str]:
        """Problems that would make a render fail, reported before the GPU spins up."""
        problems: list[str] = []
        inference = self.repo_root / "inference.py"
        if not inference.exists():
            problems.append(f"missing {inference}")
        if not self.checkpoint_dir.exists():
            problems.append(
                f"missing checkpoints at {self.checkpoint_dir} — run scripts/download_models.sh"
            )
        if not shutil.which("ffmpeg"):
            problems.append("ffmpeg not on PATH")
        return problems

    def render(self, request: AvatarRequest) -> Path:
        problems = self.preflight()
        if problems:
            raise AvatarError("; ".join(problems))
        for path, label in ((request.source, "source image"), (request.audio, "audio")):
            if not Path(path).exists():
                raise AvatarError(f"{label} not found: {path}")

        request.outdir.mkdir(parents=True, exist_ok=True)
        command = [
            self.python, "inference.py",
            "--driven_audio", str(Path(request.audio).resolve()),
            "--source_image", str(Path(request.source).resolve()),
            "--result_dir", str(Path(request.outdir).resolve()),
            "--checkpoint_dir", str(self.checkpoint_dir.resolve()),
            "--preprocess", request.preprocess,
            "--size", str(request.size),
            "--expression_scale", str(request.expression_scale),
            "--pose_style", str(request.pose_style),
        ]
        if request.still:
            command.append("--still")
        if request.enhancer:
            command += ["--enhancer", request.enhancer]
        if self.cpu:
            command.append("--cpu")
        if request.ref_eyeblink:
            command += ["--ref_eyeblink", str(Path(request.ref_eyeblink).resolve())]
        if request.ref_pose:
            command += ["--ref_pose", str(Path(request.ref_pose).resolve())]

        result = subprocess.run(
            command,
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout)[-1500:]
            raise AvatarError(f"SadTalker failed (exit {result.returncode}):\n{tail}")

        return _newest_mp4(request.outdir)


def _newest_mp4(outdir: Path) -> Path:
    """SadTalker writes into a fresh timestamped subdirectory per run."""
    candidates = sorted(
        Path(outdir).rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise AvatarError(f"SadTalker produced no mp4 under {outdir}")
    return candidates[0]
