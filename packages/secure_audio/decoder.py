from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
import io
import os
from pathlib import Path
import subprocess
import sys
from threading import Event, Timer
from typing import TYPE_CHECKING, Protocol

import av

if TYPE_CHECKING:
    import numpy as np


SAMPLE_RATE = 11025
SAMPLE_BYTES = 4


class Decoder(Protocol):
    def decode(self, media: bytes, max_seconds: int) -> np.ndarray: ...


class DecodeError(ValueError):
    pass


def _format(media: bytes) -> str:
    if media.startswith(b"RIFF") and media[8:12] == b"WAVE":
        return "wav"
    if media.startswith(b"ID3") or len(media) >= 2 and media[0] == 255 and media[1] & 0xE0 == 0xE0:
        return "mp3"
    raise DecodeError("Expected WAV or MP3 audio")


def _append_pcm(pcm: bytearray, frames: Iterable[av.AudioFrame], limit: int) -> None:
    for frame in frames:
        size = frame.samples * SAMPLE_BYTES
        if len(pcm) + size > limit:
            raise DecodeError("Audio duration exceeds the configured limit")
        pcm.extend(memoryview(frame.planes[0])[:size])


def _decode(media: bytes, max_seconds: int) -> bytes:
    resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
    pcm = bytearray()
    limit = SAMPLE_RATE * max_seconds * SAMPLE_BYTES
    with av.open(io.BytesIO(media), format=_format(media)) as container:
        if len(container.streams.audio) != 1:
            raise DecodeError("Expected one audio stream")
        for frame in container.decode(audio=0):
            if not 8000 <= frame.sample_rate <= 96000 or not 1 <= len(frame.layout.channels) <= 2:
                raise DecodeError("Unsupported channel count or sample rate")
            _append_pcm(pcm, resampler.resample(frame), limit)
        _append_pcm(pcm, resampler.resample(None), limit)
    if not pcm:
        raise DecodeError("Missing audio or audio duration exceeds the configured limit")
    if sys.byteorder == "big":
        from array import array

        samples = array("f")
        samples.frombytes(pcm)
        samples.byteswap()
        return samples.tobytes()
    return bytes(pcm)


class SubprocessDecoder:
    def __init__(self, timeout_seconds: int = 20):
        self.timeout_seconds = timeout_seconds
        self._warming = False
        self._spare: subprocess.Popen[bytes] | None = None

    @contextmanager
    def prewarm(self):
        if self._warming:
            raise RuntimeError("Decoder session already open")
        self._warming = True
        try:
            self._prepare_spare()
            yield
        finally:
            self._warming = False
            spare, self._spare = self._spare, None
            if spare is not None:
                with spare:
                    if spare.poll() is None:
                        spare.kill()

    def _spawn(self) -> subprocess.Popen[bytes]:
        environment = {name: os.environ[name] for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP") if name in os.environ}
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve())], env=environment,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _prepare_spare(self) -> None:
        if self._warming:
            try:
                self._spare = self._spawn()
            except OSError:
                self._spare = None

    def _take_process(self) -> subprocess.Popen[bytes]:
        process, self._spare = self._spare, None
        if process is not None:
            if process.poll() is None:
                return process
            with process:
                pass
        return self._spawn()

    def _communicate(self, process: subprocess.Popen[bytes], request: bytes) -> tuple[bytes, bytes]:
        timed_out = Event()

        def expire():
            if process.poll() is None:
                timed_out.set()
                process.kill()

        watchdog = Timer(self.timeout_seconds, expire)
        watchdog.daemon = True
        watchdog.start()
        try:
            output, error = process.communicate(input=request)
        finally:
            watchdog.cancel()
            watchdog.join()
            if process.poll() is None:
                process.kill()
        if timed_out.is_set():
            raise DecodeError("Audio decoding timed out")
        return output, error

    def decode(self, media: bytes, max_seconds: int) -> np.ndarray:
        import numpy as np

        _format(media)
        request = f"{max_seconds}\n".encode("ascii") + media
        try:
            with self._take_process() as process:
                output, error = self._communicate(process, request)
        finally:
            self._prepare_spare()
        if process.returncode != 0 or not output:
            reason = error.decode("utf-8", errors="replace").strip() if process.returncode == 2 else ""
            raise DecodeError(reason or "Audio decoding failed")
        samples = np.frombuffer(output, dtype="<f4")
        if not np.isfinite(samples).all():
            raise DecodeError("Audio contains non-finite samples")
        return samples


if __name__ == "__main__":
    try:
        duration = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.buffer.readline(32)
        sys.stdout.buffer.write(_decode(sys.stdin.buffer.read(), int(duration)))
    except DecodeError as exc:
        sys.stderr.write(str(exc))
        sys.exit(2)
    except (av.FFmpegError, ValueError) as exc:
        sys.stderr.write("Malformed audio")
        sys.exit(2)
