"""Speech, via a local XTTS server.

Same shape as everything else here: plain ``httpx`` against a URL on your own
network, no vendor SDK, nothing hosted. The server is two calls -- ``POST /speak``
returns a filename, ``GET /audio/<name>`` returns the WAV -- which is worth
wrapping precisely because it is two calls and the first one alone looks like it
succeeded.

**Answers are trimmed before synthesis, and that is not cosmetic.** A single
short sentence came back as 209 KB of WAV; a full paragraph answer takes long
enough to generate that you would hear it well after you needed it. Speech is
for the sentence that changes what you do next, so :func:`spoken_form` keeps the
lead and drops the rest rather than reading a citation list aloud.

Nothing here decides *what* to say. The text is whatever the agent produced,
already past every grounding check -- synthesis is downstream of the perimeter,
so it cannot introduce a claim.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "http://192.168.1.78:8020"
DEFAULT_VOICE = "tsunade"

# Generation is roughly linear in characters, which on the 5090 the XTTS server
# runs on is cheap enough not to be the constraint. What remains is that a long
# answer is slower to hear than to read -- about a minute and a half at this
# budget -- so this is a listening-time trade rather than a compute one.
#
# Matched to bot.py's ANSWER_BUDGET on purpose: the spoken answer and the
# written one now end in the same place. At 320 the audio stopped two or three
# sentences in, mid-answer, with nothing to say it had -- and out loud there is
# no scrollback to notice it against.
SPOKEN_CHARS = 1500

# Synthesis on a warm GPU is a few seconds; a cold model load is much longer.
DEFAULT_TIMEOUT = 120.0

_SENTENCE = re.compile(r"(?<=[.!?])\s+")
# Markdown and citation furniture. Read aloud these become "open bracket
# Abyssal whip close bracket http colon slash slash", which is unlistenable.
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_`#>|]+")


class VoiceError(RuntimeError):
    """The speech server failed, or answered in a shape we can't use."""


def spoken_form(text: str, *, limit: int = SPOKEN_CHARS) -> str:
    """The part of an answer worth hearing.

    Whole sentences only. Cutting mid-sentence is worse out loud than on screen:
    there is no scrollback, so a clipped clause is simply lost rather than
    visibly truncated.
    """
    clean = _LINK.sub(r"\1", text)
    clean = _URL.sub("", clean)
    clean = _MARKUP.sub("", clean)
    clean = " ".join(clean.split())
    if len(clean) <= limit:
        return clean

    out: list[str] = []
    for sentence in _SENTENCE.split(clean):
        if sum(len(s) + 1 for s in out) + len(sentence) > limit and out:
            break
        out.append(sentence)

    # The first sentence is taken unconditionally -- there has to be something to
    # say -- so one enormous sentence lands here still over budget, and the loop
    # above cannot trim it because there is no boundary inside it to cut on. A
    # hard cut is the only option left, and it beats the alternative: this is the
    # exact shape that generates the minutes-long WAV the budget exists to stop.
    spoken = " ".join(out)
    return spoken if len(spoken) <= limit else spoken[:limit]


class VoiceClient:
    """Async client for the XTTS server.

    Args:
        base_url: Server root, e.g. ``http://192.168.1.78:8020``.
        voice: ``performer_id`` on the server. ``GET /health`` lists them.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        voice: str = DEFAULT_VOICE,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.voice = voice
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def __aenter__(self) -> VoiceClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def voices(self) -> list[str]:
        """Voices the server will accept. Empty if it cannot be reached."""
        try:
            payload = (await self._http.get(f"{self._base_url}/health")).json()
        except Exception as exc:
            log.warning("Could not list voices from %s: %r", self._base_url, exc)
            return []
        return list(payload.get("voices") or [])

    async def speak(
        self, text: str, *, voice: str | None = None, speed: float | None = None
    ) -> bytes:
        """Synthesise text to WAV bytes.

        Raises:
            VoiceError: the server was unreachable, refused, or returned a
                filename that then would not download. The second half matters:
                ``POST /speak`` answering 200 does not mean there is audio, and
                treating it as success yields a silent, successful-looking play.
        """
        spoken = spoken_form(text)
        if not spoken:
            raise VoiceError("Nothing to say.")

        # speed is omitted rather than sent as 1.0 when unset, so the server's
        # own default stays the default and this client is not silently pinning
        # it to whatever the value happened to be when this was written.
        body: dict[str, object] = {"text": spoken, "performer_id": voice or self.voice}
        if speed is not None:
            body["speed"] = speed
        try:
            response = await self._http.post(f"{self._base_url}/speak", json=body)
            response.raise_for_status()
            name = (response.json() or {}).get("audio")
        except httpx.HTTPError as exc:
            raise VoiceError(f"Could not reach the speech server: {exc!r}") from exc
        except ValueError as exc:
            raise VoiceError(f"Speech server sent malformed JSON: {exc!r}") from exc

        if not name:
            raise VoiceError("Speech server returned no audio filename.")

        try:
            audio = await self._http.get(f"{self._base_url}/audio/{name}")
            audio.raise_for_status()
        except httpx.HTTPError as exc:
            raise VoiceError(f"Synthesised {name!r} but could not fetch it: {exc!r}") from exc
        if not audio.content:
            raise VoiceError(f"Synthesised {name!r} and it was empty.")
        return audio.content


# Playing the WAV is deliberately a subprocess rather than an audio dependency.
# Every desktop ships something that plays a WAV, none of them agree on what, and
# a sound library is a large amount of build surface -- wheels, native bindings,
# a device abstraction -- for one call at the very end of the pipeline.
#
# Ordered by how likely it is to be the right one on the machine you are sitting
# at, not alphabetically: afplay means macOS and nothing else does.
_PLAYERS: tuple[tuple[str, ...], ...] = (
    ("afplay",),                                              # macOS, always present
    ("paplay",),                                              # PulseAudio
    ("aplay", "-q"),                                          # ALSA
    ("ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"),  # wherever ffmpeg is
)


def players() -> list[str]:
    """Which of the known players this machine actually has."""
    return [p[0] for p in _PLAYERS if shutil.which(p[0])]


def retime(audio: bytes, tempo: float | None) -> bytes:
    """Re-pace a WAV without moving its pitch. Unchanged if ffmpeg is missing.

    This exists because the server's own ``speed`` is model conditioning, not a
    rate control, and it is not smooth: measured on one line through one voice,
    1.33 gives 298,092 bytes and 1.35 gives 389,740 -- thirty percent *more*
    audio for a faster setting, which is a degenerate generation, and it is
    audibly broken. Deterministic too, byte-identical across repeats, so it is a
    property of the value rather than a bad sample you can retry past. That
    makes speed unusable for fine tuning: neighbouring values are not
    neighbouring outputs.

    ``atempo`` is arithmetic on finished audio instead, so 1.34 sits exactly
    between 1.33 and 1.35 and nothing can loop. Valid per filter instance from
    0.5 to 2.0, which is wider than any speaking rate worth having.
    """
    if not audio or tempo is None or abs(tempo - 1.0) < 0.005:
        return audio
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        log.warning("No ffmpeg: speaking at the synthesised pace, not %.2fx", tempo)
        return audio
    tempo = max(0.5, min(2.0, tempo))
    # Output to a real file, never to pipe:1. A WAV header carries the RIFF and
    # data sizes up front, and ffmpeg only learns them once the stream ends -- on
    # a pipe it cannot seek back to patch them, so it writes 0xFFFFFFFF and moves
    # on. Measured on one clip: 125,960 bytes of audio declaring 4,294,967,295.
    # Players differ on how they take that; a browser believes it, keeps waiting
    # for four gigabytes that never arrive, and the result is audio that stutters,
    # cuts off, or comes out as noise. Input on a pipe is fine -- only the writer
    # needs to seek.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out = tmp.name
    try:
        done = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-i", "pipe:0", "-filter:a", f"atempo={tempo:.3f}", out],
            input=audio, capture_output=True,
        )
        if done.returncode != 0:
            detail = done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"
            log.warning("Could not retime the audio (%s); playing it as synthesised", detail)
            return audio
        retimed = os.path.getsize(out) and open(out, "rb").read()
        return retimed or audio
    finally:
        os.unlink(out)


def play(audio: bytes, *, tempo: float | None = None) -> str:
    """Play WAV bytes out of this machine's speakers. Returns the player used.

    Synchronous and blocking on purpose: speech is the last thing that happens
    to an answer, and returning before it has been heard would race the process
    exit and cut her off mid-sentence.

    Raises:
        VoiceError: nothing on PATH could play it, or the player itself failed.
            Both are worth raising rather than logging -- a silent failure here
            is indistinguishable from a working setup with the volume down.
    """
    if not audio:
        raise VoiceError("No audio to play.")

    audio = retime(audio, tempo)
    # delete=False and an explicit unlink: the player is a separate process and
    # needs the file to still be there when it opens it.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio)
        path = tmp.name
    try:
        for player in _PLAYERS:
            exe = shutil.which(player[0])
            if exe is None:
                continue
            done = subprocess.run([exe, *player[1:], path], capture_output=True)
            if done.returncode == 0:
                return player[0]
            detail = done.stderr.decode(errors="replace").strip() or f"exit {done.returncode}"
            raise VoiceError(f"{player[0]} could not play the audio: {detail}")
        raise VoiceError(
            "No audio player on PATH. Install one of: "
            + ", ".join(p[0] for p in _PLAYERS)
        )
    finally:
        os.unlink(path)
