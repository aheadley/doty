from typing import (
    Optional,
    Tuple,
)

import ctypes

import numpy

from .constants import (
    SAMPLE_FRAME_RATE,
    SAMPLE_FRAME_SIZE,
)
from .utils import (
    batch,
    get_worker_logger,
)

class RNNoise:
    # Based on https://github.com/Shb742/rnnoise_python/blob/master/rnnoise.py
    SAMPLE_RATE: int = SAMPLE_FRAME_RATE
    SAMPLE_SIZE: int = SAMPLE_FRAME_SIZE
    SAMPLES_IN_FRAME = 480
    FRAME_SIZE = SAMPLES_IN_FRAME * SAMPLE_SIZE

    _lib: Optional[ctypes.CDLL] = None

    @classmethod
    def _load_library(cls) -> ctypes.CDLL:
        # a few sanity checks just in case
        assert cls.SAMPLES_IN_FRAME * 100 == cls.SAMPLE_RATE

        lib_path = ctypes.util.find_library('rnnoise')
        get_worker_logger('transcriber.rnnoise').debug('Loading rnnoise lib: path=%s', lib_path)

        lib = ctypes.cdll.LoadLibrary(lib_path)

        lib.rnnoise_process_frame.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float)
        ]
        lib.rnnoise_process_frame.restype = ctypes.c_float
        lib.rnnoise_create.restype = ctypes.c_void_p
        lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]

        return lib

    @classmethod
    @property
    def lib(cls) -> ctypes.CDLL:
        if cls._lib is None:
            cls._lib = cls._load_library()
        return cls._lib

    def __init__(self):
        self._log = get_worker_logger('transcriber.rnnoise')
        self._state = self.lib.rnnoise_create(None)

    def __del__(self):
        if self._state is not None:
            self.lib.rnnoise_destroy(self._state)
            self._state = None

    def process_frame(self, buffer: bytes) -> Tuple[bytes, float]:
        in_buf = numpy.ndarray((self.SAMPLES_IN_FRAME,), numpy.int16, buffer).astype(ctypes.c_float)
        in_ptr = in_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        out_buf = numpy.ndarray((self.SAMPLES_IN_FRAME,), ctypes.c_float)
        out_ptr = out_buf.ctypes.data_as(ctypes.POINTER(ctypes.c_float))

        vad_prob = self.lib.rnnoise_process_frame(self._state, out_ptr, in_ptr)
        output = out_buf.astype(ctypes.c_short).tobytes()

        assert len(output) == len(buffer)

        return output, vad_prob

    def denoise(self, buffer: bytes) -> bytes:
        # append silence to pad out to the right length
        buffer += bytes([0]) * (len(buffer) % (self.SAMPLES_IN_FRAME * self.SAMPLE_SIZE))

        return b''.join(self.process_frame(bytes(chunk))[0] \
            for chunk in batch(buffer, self.SAMPLES_IN_FRAME * self.SAMPLE_SIZE))
