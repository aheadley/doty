import struct
from pymumble_py3.constants import (
    PYMUMBLE_AUDIO_PER_PACKET,
    PYMUMBLE_SAMPLERATE,
)

APP_NAME: str = 'doty'
EXTRA_DEBUG_ENV_VAR: str = 'DOTY_EXTRA_DEBUG'
# this must be less than PYMUMBLE_AUDIO_PER_PACKET (0.02)
SHORT_POLL: float = PYMUMBLE_AUDIO_PER_PACKET / 2
LONG_POLL: float = 0.1
DENOTIFY_CHAR: str = u'\u200B'
# https://www.isip.piconepress.com/projects/speech/software/tutorials/production/fundamentals/v1.0/section_02/s02_01_p05.html
WAV_HEADER_LEN: int = 44
# https://cloud.google.com/speech-to-text/quotas
MAX_TRANSCRIPTION_TIME: float = 60.0
BASE_CONFIG_FILENAME: str = 'config.yml.example'
SCHEMA_FILENAME: str = 'config.schema'
SOUNDCHUNK_SIZE: int = 1920
AUDIO_CHANNELS: int = 1
SAMPLE_FRAME_FORMAT: str = '<h'
SAMPLE_FRAME_SIZE: int = struct.calcsize(SAMPLE_FRAME_FORMAT)
SAMPLE_FRAME_RATE: int = PYMUMBLE_SAMPLERATE
VOLUME_ADJUSTMENT_STEP: float = 0.2
DEFAULT_MEDIA_VOLUME: float = 0.5
UPLOAD_TARGET_URL: str = 'https://0x0.st/'
STATIC_DATA_DIR: str = 'static'
