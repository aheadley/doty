import logging
import numpy.typing
from typing import (
    Optional,
    Union,
)
from pymumble_py3.constants import (
    PYMUMBLE_SAMPLERATE,
    PYMUMBLE_AUDIO_PER_PACKET,
)
from pymumble_py3.soundqueue import SoundChunk

import io
import time
import wave

import numpy
import samplerate.converters

from numpy_ringbuffer import RingBuffer

from .constants import (
    AUDIO_CHANNELS,
    SAMPLE_FRAME_FORMAT,
    SAMPLE_FRAME_RATE,
    SAMPLE_FRAME_SIZE,
    SOUNDCHUNK_SIZE,
    SHORT_POLL,
    LONG_POLL,
)
from .utils import (
    get_worker_logger,
)

def audio_resample(buf: numpy.typing.ArrayLike, src_sr: int, dest_sr: int,
        method: str ='sinc_best') -> numpy.typing.ArrayLike:
    sr_ratio = float(dest_sr) / src_sr
    return samplerate.converters.resample(buf, sr_ratio, converter_type=method).astype(numpy.int16)

def audio_duration(buf: bytes, sample_rate: int=SAMPLE_FRAME_RATE) -> float:
    return (len(buf) / SAMPLE_FRAME_SIZE) / SAMPLE_FRAME_RATE

def buffer2wav(buffer: bytes) -> bytes:
    with io.BytesIO() as output:
        with wave.open(output, 'wb') as wav_output:
            # pylint: disable=E1103
            wav_output.setnchannels(AUDIO_CHANNELS)
            wav_output.setsampwidth(SAMPLE_FRAME_SIZE)
            wav_output.setframerate(PYMUMBLE_SAMPLERATE)
            wav_output.writeframes(buffer)
            # pylint: enable=E1103
        return output.getvalue()

def silent_chunk(last_packet: SoundChunk, offset: int, missing_packets: int) -> SoundChunk:
    dt = offset * (PYMUMBLE_AUDIO_PER_PACKET / missing_packets)
    return SoundChunk(
        bytes(SOUNDCHUNK_SIZE),
        last_packet.sequence + SHORT_POLL + (offset * SHORT_POLL),
        SOUNDCHUNK_SIZE,
        last_packet.time + SHORT_POLL + dt,
        last_packet.type,
        last_packet.target,
        timestamp=last_packet.timestamp + SHORT_POLL + dt,
    )

class AssignableRingBuffer(RingBuffer):
    def _normalize_slice(self, idx: slice) -> slice:
        if idx.step not in (None, 1):
            raise IndexError('Slice stepping not supported')
        if idx.start is None:
            idx = slice(0, idx.stop, idx.step)
        elif idx.start < 0:
            idx = slice(self._capacity + idx.start, idx.stop, idx.step)
        if idx.stop in (None, 0):
            idx = slice(idx.start, self._capacity, idx.step)
        elif idx.stop < 0:
            idx = slice(idx.start, self._capacity + idx.stop, idx.step)
        return idx

    def __setitem__(self, idx: Union[int, slice], value: numpy.typing.ArrayLike) -> None:
        if isinstance(idx, slice):
            idx = self._normalize_slice(idx)
            assert (idx.stop - idx.start) == len(value), \
                'len(slice) != len(value): idx.start={} idx.stop={} diff={} len(value)={}'.format(
                    idx.start, idx.stop, idx.stop - idx.start, len(value))
            assert len(value) < self._capacity, \
                'slice too large: len(value)={}'.format(len(value))

            # first chunk
            sl_start = (self._left_index + idx.start) % self._capacity
            sl_end = (self._left_index + idx.stop) % self._capacity
            if sl_start < sl_end:
                # simple case, doesn't wrap
                self._arr[sl_start:sl_end] = value
            else:
                sl1 = value[:len(self._arr[sl_start:])]
                self._arr[sl_start:] = sl1
                self._arr[:sl_end] = value[len(sl1):]
        elif isinstance(idx, int):
            if idx >= 0:
                self._arr[self._left_index+idx % self._capacity] = value
            else:
                self._arr[self._capacity+idx] = value
        else:
            raise IndexError('Invalid index type: {}'.format(idx))

class MixingBuffer:
    MIN_SILENCE_EXTENSION: float = 0.5
    DTYPE_SHORT: numpy.typing.DTypeLike = numpy.dtype(SAMPLE_FRAME_FORMAT)
    DTYPE_FLOAT: numpy.typing.DTypeLike = numpy.float32

    @staticmethod
    def _mix_frames(left: numpy.typing.ArrayLike, right: numpy.typing.ArrayLike) -> numpy.typing.ArrayLike:
        SCALE_VALUE = (2 ** ((SAMPLE_FRAME_SIZE * 8) - 1)) - 1
        scale_factor = 1/SCALE_VALUE
        left = left.astype(MixingBuffer.DTYPE_FLOAT) * scale_factor
        right = right.astype(MixingBuffer.DTYPE_FLOAT) * scale_factor
        out = (left + right) - (left * right)
        out *= SCALE_VALUE
        out = numpy.clip(out, -SCALE_VALUE, SCALE_VALUE).astype(MixingBuffer.DTYPE_SHORT)
        return out

    def __init__(self, buffer_len: int, log: Optional[logging.Logger] =None):
        if log is None:
            log = get_worker_logger('mixer')
        self._log = log
        self._buffer_len = buffer_len
        self._reset()

    def add_chunk(self, chunk: SoundChunk) -> None:
        if chunk.time < (self._current_head - self._frames_to_seconds(len(self._buffer))):
            # chunk is too far in the past, ignore it
            self._log.debug('Ignoring very old chunk: %s', chunk.time)
            return
        if (chunk.time + chunk.duration) > self._current_head:
            # chunk is ahead of us, fill with silence to mix against
            diff = (chunk.time + chunk.duration) - self._current_head
            if diff > self._frames_to_seconds(len(self._buffer)):
                # the chunk is far ahead of us, drop the entire buffer
                self._reset()
            else:
                self._extend_with_silence(diff)

        buf = numpy.frombuffer(chunk.pcm, dtype=self.DTYPE_SHORT)
        mix_start_pos = -self._seconds_to_frames(self._current_head - chunk.time)
        mix_end_pos = mix_start_pos + len(buf)
        if mix_end_pos == 0:
            mix_end_pos = None
        try:
            self._buffer[mix_start_pos:mix_end_pos] = self._mix_frames(
                self._buffer[mix_start_pos:mix_end_pos], buf)
        except Exception as err:
            self._log.warning('Failed to mix audio: [%d:%d]', mix_start_pos, mix_end_pos)
            self._log.debug('Mix stats: _buffer._left_index=%d _buffer._right_index=%d len(buf)=%d',
                self._buffer._left_index, self._buffer._right_index, len(buf))
            self._log.exception(err)

    def add_buffer(self, buf: bytes) -> None:
        now = time.time()
        if self._current_head > now:
            fake_chunk = SoundChunk(
                buf,
                0,
                len(buf),
                now,
                None,
                None,
                timestamp=now,
            )
            self.add_chunk(fake_chunk)
        else:
            self._extend_with_silence(exact=True)
            buf = numpy.frombuffer(buf, dtype=self.DTYPE_SHORT)
            self._buffer.extend(buf)
            self._current_head += self._frames_to_seconds(len(buf))

    def _extend_with_silence(self, silence_length: Optional[float] =None, exact: bool =False) -> None:
        if silence_length is None:
            now = time.time()
            if now > self._current_head:
                silence_length = now - self._current_head
            else:
                # self._current_head is in the future, don't need to come up to date
                return
        if not exact:
            silence_length = max(min(silence_length, self._buffer_len), self.MIN_SILENCE_EXTENSION)
        self._buffer.extend(numpy.zeros(self._seconds_to_frames(silence_length), dtype=self.DTYPE_SHORT))
        self._current_head += silence_length

    def _reset(self) -> None:
        self._buffer = AssignableRingBuffer(self._seconds_to_frames(self._buffer_len), dtype=self.DTYPE_SHORT)
        self._buffer.extend(numpy.zeros(self._buffer._capacity, dtype=self.DTYPE_SHORT))
        self._current_head = time.time()

    def get_last(self, seconds: float) -> numpy.typing.ArrayLike:
        seconds = min(self._frames_to_seconds(len(self._buffer) - 1), seconds)
        frame_count = self._seconds_to_frames(seconds)
        buf = self._buffer[-frame_count:].tobytes()
        # round to PYMUMBLE_AUDIO_PER_PACKET
        return buf[len(buf) % SOUNDCHUNK_SIZE:]

    def get_all(self) -> numpy.typing.ArrayLike:
        buf = self._buffer[:].tobytes()
        # round to PYMUMBLE_AUDIO_PER_PACKET
        return buf[len(buf) % SOUNDCHUNK_SIZE:]

    def _seconds_to_frames(self, seconds: float) -> int:
        return int(round(seconds * PYMUMBLE_SAMPLERATE))

    def _frames_to_seconds(self, frames: int) -> float:
        return frames / PYMUMBLE_SAMPLERATE
