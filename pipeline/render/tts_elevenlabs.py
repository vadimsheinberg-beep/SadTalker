"""ElevenLabs narration.

The API key belongs on the server as ``/opt/tzoar/deploy/secrets/elevenlabs.env``
with mode 0600; it must never travel through chat, a commit, or a log line.
``ELEVENLABS_VOICE_ID_RU`` and ``ELEVENLABS_VOICE_ID_EN`` are filled in after
the voices are cloned.

Beats are synthesised one file each rather than as one long take. That gives
the compositor a real duration per slide instead of an estimate, and it means a
single reworded beat costs one API call instead of the whole script.
"""

from __future__ import annotations

import json
import re
import struct
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import SECRETS, SETTINGS, RenderPolicy, Secrets
from ..contracts import Language, Script

API_ROOT = "https://api.elevenlabs.io/v1"


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class Utterance:
    index: int
    role: str
    text: str
    path: Path
    duration_s: float


class Lexicon:
    """Pronunciation overrides applied before synthesis.

    Tractate names, Aramaic and Hebrew terms, and proper nouns are the words a
    multilingual TTS reliably gets wrong, and they are exactly the words the
    claim beat is made of. Overrides are whole-word and case-insensitive, so
    "Bava" in "Bava Metzia" is replaced while "Bavarian" is untouched.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = dict(mapping or {})
        self._pattern = self._compile()

    def _compile(self) -> re.Pattern[str] | None:
        if not self._mapping:
            return None
        keys = sorted(self._mapping, key=len, reverse=True)
        joined = "|".join(re.escape(key) for key in keys)
        return re.compile(rf"(?<!\w)({joined})(?!\w)", re.IGNORECASE | re.UNICODE)

    @classmethod
    def load(cls, path: Path) -> "Lexicon":
        if not Path(path).exists():
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw if isinstance(raw, dict) else raw.get("terms", {}))

    def apply(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(
            lambda m: self._lookup(m.group(0)), text
        )

    def _lookup(self, word: str) -> str:
        for key, value in self._mapping.items():
            if key.casefold() == word.casefold():
                return value
        return word


class ElevenLabsClient:
    def __init__(
        self,
        secrets: Secrets | None = None,
        policy: RenderPolicy | None = None,
        timeout: int = 120,
    ) -> None:
        self._secrets = secrets or SECRETS
        self._policy = policy or SETTINGS.render
        self._timeout = timeout

    def voice_id(self, language: Language) -> str:
        key = f"ELEVENLABS_VOICE_ID_{language.value.upper()}"
        return self._secrets.require("elevenlabs", key)

    def synthesize(
        self,
        text: str,
        language: Language,
        destination: Path,
        stability: float = 0.45,
        similarity_boost: float = 0.8,
    ) -> Path:
        body = json.dumps(
            {
                "text": text,
                "model_id": self._policy.elevenlabs_model,
                "voice_settings": {
                    "stability": stability,
                    "similarity_boost": similarity_boost,
                },
            }
        ).encode("utf-8")
        url = f"{API_ROOT}/text-to-speech/{self.voice_id(language)}?output_format=mp3_44100_128"
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "xi-api-key": self._secrets.require("elevenlabs", "ELEVENLABS_API_KEY"),
                "content-type": "application/json",
                "accept": "audio/mpeg",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                audio = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise TTSError(f"ElevenLabs {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TTSError(f"ElevenLabs unreachable: {exc}") from exc

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio)
        return destination


def narrate(
    script: Script,
    outdir: Path,
    client: ElevenLabsClient | None = None,
    lexicon: Lexicon | None = None,
) -> list[Utterance]:
    """Synthesise every beat. Returns utterances with measured durations."""
    client = client or ElevenLabsClient()
    lexicon = lexicon or Lexicon()
    outdir = Path(outdir)

    utterances: list[Utterance] = []
    for index, beat in enumerate(script.beats):
        path = outdir / f"{index:02d}_{beat.role}.mp3"
        client.synthesize(lexicon.apply(beat.text), script.language, path)
        utterances.append(
            Utterance(
                index=index,
                role=beat.role,
                text=beat.text,
                path=path,
                duration_s=probe_duration(path),
            )
        )
    return utterances


def probe_duration(path: Path) -> float:
    """Duration in seconds via ffprobe, falling back to a WAV header read."""
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return round(float(result.stdout.strip()), 3)

    if Path(path).suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as handle:
            return round(handle.getnframes() / float(handle.getframerate()), 3)

    raise TTSError(
        f"cannot determine duration of {path}: install ffprobe or use WAV output"
    )


def concat(paths: Iterable[Path], destination: Path) -> Path:
    """Join per-beat audio into one track using ffmpeg's concat demuxer."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise TTSError("ffmpeg is required to concatenate narration")

    paths = [Path(p) for p in paths]
    destination.parent.mkdir(parents=True, exist_ok=True)
    listing = destination.parent / "concat.txt"
    listing.write_text(
        "\n".join(f"file '{path.resolve()}'" for path in paths), encoding="utf-8"
    )
    subprocess.run(
        [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1", str(destination)],
        check=True,
        capture_output=True,
    )
    return destination


def silent_wav(destination: Path, duration_s: float, rate: int = 16000) -> Path:
    """Write a silent mono WAV. Used for timing dry runs without API calls."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames = int(duration_s * rate)
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * frames)
    return destination
