"""Unit tests for the failure-isolated RND-258 ffmpeg wrapper."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from app.voice_transcode import transcode_voice_to_playable


def _minimal_amr_nb() -> bytes:
    # AMR-NB magic + one valid-ish FT=0/Q=1 speech frame. The payload is
    # silence-like rather than a production recording; it is enough for the
    # ffmpeg AMR demuxer/decoder smoke test when ffmpeg is installed.
    return b"#!AMR\n" + b"\x04" + (b"\x00" * 12)


def test_missing_ffmpeg_returns_none(monkeypatch) -> None:
    def _missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(subprocess, "run", _missing)
    assert transcode_voice_to_playable(_minimal_amr_nb()) is None


def test_mp3_encoder_falls_back_to_wav(monkeypatch) -> None:
    calls = []

    def _run(command, **kwargs):
        calls.append(command)
        if "libmp3lame" in command:
            return subprocess.CompletedProcess(command, 1, b"", b"no mp3")
        return subprocess.CompletedProcess(command, 0, b"RIFFfakeWAVE", b"")

    monkeypatch.setattr(subprocess, "run", _run)
    result = transcode_voice_to_playable(b"voice")

    assert result is not None
    assert result.extension == ".wav"
    assert result.data.startswith(b"RIFF")
    assert len(calls) == 2


def test_silk_payload_is_not_forged_into_playable_audio() -> None:
    assert transcode_voice_to_playable(b"#!SILK_V3\nnot-a-real-silk") is None


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_converts_amr_fixture_to_mp3() -> None:
    result = transcode_voice_to_playable(_minimal_amr_nb())

    assert result is not None
    assert result.extension == ".mp3"
    assert result.data.startswith((b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
