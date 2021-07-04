import logging
from typing import (
    Dict,
    List,
    Optional,
)

import json
import io
from typing import NamedTuple

import contexttimer
import vosk

from .audio_utils import audio_resample
from .constants import (
    AUDIO_CHANNELS,
    SAMPLE_FRAME_RATE,
)
from .ipc import (
    MessageType,
    Packet,
)
from .mp_utils import (
    multiprocessify,
    BaseWorker,
)
from .rnnoise import RNNoise
from .utils import (
    get_worker_logger,
    extra_debugging_enabled,
)

_log = get_worker_logger('transcriber')

class TranscriptionResult(NamedTuple):
    transcript: str = ''
    confidence: float = -1.0

class BaseSTTEngine:
    def __init__(self, engine_params: Dict):
        self._log = get_worker_logger('transcriber.engine')
        self._denoiser = RNNoise()

    @contexttimer.timer(logger=get_worker_logger('transcriber.engine'), level=logging.DEBUG)
    def transcribe(self, buf: bytes, phrases: List[str]=[]) -> Optional[TranscriptionResult]:
        return self._transcribe(self._denoise(buf), phrases)

    def _transcribe(self, buf: bytes, phrases: List[str]=[]) -> Optional[TranscriptionResult]:
        raise NotImplementedError

    def _denoise(self, buffer: bytes) -> bytes:
        return self._denoiser.denoise(buffer)

class DummyEngine(BaseSTTEngine):
    def transcribe(self, buf: bytes, phrases: List[str] = []) -> Optional[TranscriptionResult]:
        return None

class VoskSTTEngine(BaseSTTEngine):
    def __init__(self, engine_params: Dict):
        super().__init__(engine_params)

        if not extra_debugging_enabled():
            vosk.SetLogLevel(-1000)
        self._model = vosk.Model(engine_params['model_path'])

    def _transcribe(self, buf: bytes, phrases: List[str] = []) -> Optional[TranscriptionResult]:
        recognizer = vosk.KaldiRecognizer(self._model, SAMPLE_FRAME_RATE,
                                          json.dumps([' '.join(phrases).lower(), '[unk]']))
        recognizer.AcceptWaveform(buf)
        result = json.loads(recognizer.FinalResult())

        try:
            if result['text'].strip():
                return TranscriptionResult(
                    result['text'].strip(),
                    sum(t['conf'] for t in result['result']) / len(result['result']) \
                        if 'result' in result else 0.0,
                )
        except Exception as err:
            self._log.exception(err)
        return None
try:
    import numpy
    import stt as coqui_speechtotext # pyright: reportMissingImports=false
except ImportError as err:
    CoquiSTTEngine = DummyEngine
    _log.warning('Error importing dependencies for CoquiSTTEngine, setting fallback as DummyEngine')
    _log.exception(err)
else:
    class CoquiSTTEngine(BaseSTTEngine):
        def __init__(self, engine_params: Dict):
            super().__init__(engine_params)

            self._model = coqui_speechtotext.Model(engine_params['model_path'])
            self._model.enableExternalScorer(engine_params['scorer_path'])
            self._resample_method = engine_params['resample_method']

        def _transcribe(self, buf: bytes, phrases: List[str]=[]) -> Optional[TranscriptionResult]:
            audio_data = numpy.frombuffer(buf, dtype=numpy.int16)
            model_samplerate = self._model.sampleRate()
            if model_samplerate != SAMPLE_FRAME_RATE:
                audio_data = audio_resample(audio_data, SAMPLE_FRAME_RATE,
                    model_samplerate, self._resample_method)

            try:
                result = self._model.sttWithMetadata(audio_data)
            except Exception as err:
                self._log.exception(err)
            else:
                for transcript in result.transcripts:
                    value = ''.join(t.text for t in transcript.tokens).strip()
                    if value:
                        return TranscriptionResult(
                            value,
                            transcript.confidence / len(transcript.tokens),
                        )
            return None

try:
    from watson_developer_cloud import SpeechToTextV1 as watson_speechtotext # pyright: reportMissingImports=false
except ImportError as err:
    IBMWatsonEngine = DummyEngine
    _log.warning('Error importing dependencies for IBMWatsonEngine, setting fallback as DummyEngine')
    _log.exception(err)
else:
    class IBMWatsonEngine(BaseSTTEngine):
        CONTENT_TYPE: str = 'audio/l16; rate={:d}; channels={:d}; endianness=little-endian'

        def __init__(self, engine_params: Dict):
            super().__init__(engine_params)
            self._client = watson_speechtotext(
                iam_apikey=engine_params['api_key'],
                url=engine_params['service_url'],
            )
            self._hint_phrases: List[str] = engine_params['hint_phrases']
            self._model: str = engine_params['model']
            self._content_type = self.CONTENT_TYPE.format(SAMPLE_FRAME_RATE, AUDIO_CHANNELS)

        def _transcribe(self, buf: bytes, phrases: List[str] = []) -> Optional[TranscriptionResult]:
            resp = self._client.recognize(
                audio=io.BytesIO(buf),
                content_type=self._content_type,
                model=self._model,
                keywords=list(set(phrases + self._hint_phrases)),
                keywords_threshold=0.8,
                profanity_filter=False,
            )
            try:
                results = resp.get_result()['results']
            except KeyError as err:
                self._log.exception(err)
                return None

            value = '. '.join(alt['transcript'].replace('%HESITATION', '...').strip() \
                for result in results \
                    for alt in result['alternatives']).strip()

            if value:
                conf = [alt['confidence'] \
                    for result in results
                        for alt in result['alternatives'] \
                        ]
                return TranscriptionResult(value, sum(conf)/len(conf))
            return None

ENGINE_MAP: Dict[str,BaseSTTEngine] = {
    'dummy': DummyEngine,
    'coqui-stt': CoquiSTTEngine,
    'vosk': VoskSTTEngine,
    'ibm-watson': IBMWatsonEngine,
}

class TranscriberWorker(BaseWorker):
    PROC_NAME: str = 'transcriber'

    def _on_pre_loop(self) -> None:
        super()._on_pre_loop()

        try:
            engine_type = ENGINE_MAP[self._config['engine']]
            self._engine: BaseSTTEngine = engine_type(self._config['engine_params'])
        except Exception as err:
            self._keep_running = False
            self._log.error('Failed to load transcription engine: %s', self._config['engine'])
            self._log.exception(err)

    def _on_recv_packet(self, packet: Packet) -> None:
        super()._on_recv_packet(packet)

        if packet.type in (MessageType.TRANSCRIBE_MESSAGE_REQUEST, MessageType.MMBL_RECV_CHANNEL_AUDIO_BUFFERED):
            if result := self._engine.transcribe(packet.data['buffer'], packet.data['phrases']):
                self._log.debug('Transcription result: txid=%s actor=%d result=%r',
                    packet.metadata['txid'], packet.data['actor'], result)
                self._conn.send(Packet.forward(packet, MessageType.TRANSCRIBE_MESSAGE_RESPONSE, {
                    'actor': packet.data['actor'],
                    'result': result._asdict(),
                }).serialize())
            else:
                self._log.debug('No transcription result for: txid=%s, actor=%d',
                    packet.metadata['txid'], packet.data['actor'])

TranscriberWorkerProcess = multiprocessify(TranscriberWorker)
