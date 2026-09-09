"""retime must produce a WAV a player can believe."""
from __future__ import annotations

import shutil
import struct

import pytest

from reldo.voice import retime


def _wav(frames: int = 800) -> bytes:
    """A minimal but honest 16-bit mono WAV."""
    data = b"\x00\x01" * frames
    return (
        b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 22050, 44100, 2, 16)
        + b"data" + struct.pack("<I", len(data)) + data
    )


def _sizes(b: bytes) -> tuple[int, int, int, int]:
    riff = struct.unpack("<I", b[4:8])[0]
    i = b.find(b"data")
    data = struct.unpack("<I", b[i + 4 : i + 8])[0]
    return riff, len(b) - 8, data, len(b) - i - 8


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_retimed_wav_declares_its_real_length():
    """Piping ffmpeg's WAV to stdout leaves 0xFFFFFFFF in both size fields.

    A browser believes it, waits for four gigabytes that never arrive, and plays
    audio that stutters, cuts out, or comes through as noise. Measured before the
    fix: 125,960 bytes declaring 4,294,967,295.
    """
    out = retime(_wav(), 1.3)
    riff, riff_actual, data, data_actual = _sizes(out)
    assert riff == riff_actual
    assert data == data_actual
    assert riff != 0xFFFFFFFF


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_retiming_actually_shortens_it():
    assert len(retime(_wav(4000), 1.5)) < len(_wav(4000))


def test_no_tempo_is_the_original_bytes():
    original = _wav()
    assert retime(original, None) is original
    assert retime(original, 1.0) is original
