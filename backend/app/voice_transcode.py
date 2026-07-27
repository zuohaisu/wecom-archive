"""Failure-isolated AMR/SILK voice conversion for browser playback (RND-258).

It never logs raw media, storage references, or ffmpeg error output. A failed
conversion is represented solely by ``None`` so archival of the original can
continue normally.
"""

from __future__ import annotations

import subprocess
from typing import NamedTuple, Optional


class VoiceTranscodeResult(NamedTuple):
    """Playable bytes plus their allow-listed filename extension."""

    data: bytes
    extension: str


_FFMPEG_TIMEOUT_SECONDS = 30


def _run_ffmpeg(data: bytes, command: list[str]) -> Optional[bytes]:
    try:
        completed = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    return completed.stdout


def transcode_voice_to_playable(
    data: bytes, *, prefer_mp3: bool = True
) -> Optional[VoiceTranscodeResult]:
    """Convert bytes from stdin to MP3, falling back to WAV.

    ffmpeg probes the input itself. Any missing executable, unsupported
    decoder/encoder (including unsupported WeCom SILK_v3), malformed input,
    non-zero exit, or timeout returns ``None``.
    """
    if not data:
        return None

    common = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
    if prefer_mp3:
        mp3 = _run_ffmpeg(
            data,
            common
            + [
                "-vn",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                "64k",
                "-f",
                "mp3",
                "pipe:1",
            ],
        )
        if mp3 is not None:
            return VoiceTranscodeResult(mp3, ".mp3")

    wav = _run_ffmpeg(
        data,
        common + ["-vn", "-codec:a", "pcm_s16le", "-f", "wav", "pipe:1"],
    )
    if wav is None:
        return None
    return VoiceTranscodeResult(wav, ".wav")
