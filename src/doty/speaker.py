import logging
from collections.abc import (
    Iterable,
    Mapping,
)
from typing import (
    Optional,
)

import functools
import json
import os.path

import contexttimer
import gruut
import larynx

from .audio_utils import (
    audio_duration,
    audio_resample,
)
from .constants import (
    SAMPLE_FRAME_SIZE,
    SAMPLE_FRAME_RATE,
    WAV_HEADER_LEN,
)
from .ipc import (
    MessageType,
    Packet,
)
from .mp_utils import (
    multiprocessify,
    BaseWorker,
)
from .utils import undenotify

class BaseTTSEngine:
    def __init__(self, engine_params: Mapping, logger: logging.Logger):
        self._log = logger

    def _speak(self, text: str) -> Optional[bytes]:
        raise NotImplementedError()

    @functools.lru_cache(maxsize=16)
    def speak(self, text: str) -> Optional[bytes]:
        text = text.strip()
        if text:
            return self._speak(text)
        else:
            return None

class DummyEngine(BaseTTSEngine):
    def _speak(self, text: str) -> Optional[bytes]:
        return None

# class CoquiTTSEngine(BaseTTSEngine):
#     def __init__(self, engine_params, logger):
#         super().__init__(engine_params, logger)
#         from TTS.utils.manage import ModelManager
#         from TTS.utils.synthesizer import Synthesizer

#         self._resample_method = engine_params['resample_method']
#         self._model_manager = ModelManager(
#             os.path.join(os.path.dirname(coqui_texttospeech.__file__), '.models.json'))
#         model_path, model_config_path, model_item = self._model_manager.download_model(
#             'tts_models/' + engine_params['model'])
#         vocoder_path, vocoder_config_path, _ = self._model_manager.download_model(
#             'vocoder_models/' + engine_params['vocoder'] if engine_params['vocoder'] else model_item['default_vocoder'])

#         self._synth = Synthesizer(
#             model_path,
#             model_config_path,
#             None, # speakers_file_path
#             vocoder_path,
#             vocoder_config_path,
#             None, # encoder_path
#             None, # encoder_config_path
#             False, # use_cuda
#         )

#     def _speak(self, text):
#         text = text.strip()
#         if text[-1] not in '.!?':
#             # coqui can have a stroke if it doesn't get a input end token
#             text += '.'
#         audio = numpy.array(self._synth.tts(text, None, None))
#         norm_audio = audio * (32767 / max(0.01, numpy.max(numpy.abs(audio))))
#         resampled_audio = audio_resample(norm_audio,
#             self._synth.output_sample_rate, PYMUMBLE_SAMPLERATE, self._resample_method)
#         return bytes(resampled_audio)

class LarynxTTSEngine(BaseTTSEngine):
    def __init__(self, engine_params: Mapping, logger: logging.Logger):
        super().__init__(engine_params, logger)

        self._resample_method = engine_params['resample_method']
        with open(os.path.join(engine_params['model_path'], 'config.json')) as model_config_file:
            model_config = json.load(model_config_file)
        self._model_samplerate = model_config['audio']['sample_rate']
        self._tts_config = {
            'gruut_lang': gruut.Language.load('en-us'),
            'tts_model': larynx.load_tts_model(
                model_type=larynx.constants.TextToSpeechType.GLOW_TTS,
                model_path=engine_params['model_path'],
            ),
            'tts_settings': {
                'noise_scale': engine_params['noise_scale'],
                'length_scale': engine_params['length_scale'],
            },
            'vocoder_model': larynx.load_vocoder_model(
                model_type=larynx.constants.VocoderType.HIFI_GAN,
                model_path=engine_params['vocoder_path'],
                denoiser_strength=engine_params['denoiser_strength'],
            ),
            'audio_settings': larynx.audio.AudioSettings(**model_config['audio']),
        }

    def _speak(self, text: str) -> Optional[bytes]:
        results = larynx.text_to_speech(text=text, **self._tts_config)
        combined_audio = bytes().join(
            bytes(audio_resample(audio,
                self._model_samplerate, SAMPLE_FRAME_RATE, self._resample_method))
            for _, audio in results
        )
        return combined_audio

try:
    from google.cloud import texttospeech as gcloud_texttospeech # pyright: reportMissingImports=false
except ImportError:
    GoogleTTSEngine = None
else:
    class GoogleTTSEngine(BaseTTSEngine):
        def __init__(self, engine_params: Mapping, logger: logging.Logger):
            super().__init__(engine_params, logger)
            self._client = gcloud_texttospeech.TextToSpeechClient.from_service_account_json(engine_params['credentials_path'])
            self._voice = gcloud_texttospeech.VoiceSelectionParams(
                language_code=engine_params['language'],
                name=engine_params['voice'],
            )
            self._config = gcloud_texttospeech.AudioConfig(
                audio_encoding=gcloud_texttospeech.AudioEncoding.LINEAR16,
                sample_rate_hertz=SAMPLE_FRAME_RATE,
                effects_profile_id=engine_params['effect_profiles'],
                speaking_rate=engine_params['speed'],
                pitch=engine_params['pitch'],
                volume_gain_db=engine_params['volume'],
            )

        def _speak(self, text: str) -> Optional[bytes]:
            text_input = gcloud_texttospeech.SynthesisInput(text=text)
            response = self._client.synthesize_speech(input=text_input,
                voice=self._voice, audio_config=self._config)
            return response.audio_content[WAV_HEADER_LEN:]

class SpeakerWorker(BaseWorker):
    PROC_NAME: str = 'speaker'

    def _on_pre_loop(self) -> None:
        super()._on_pre_loop()

        os.nice(self._config['process_priority'])
        try:
            self._engine = {
                # 'coqui-tts': CoquiTTSEngine,
                'larynx-tts': LarynxTTSEngine,
                'gcloud-tts': GoogleTTSEngine,
            }.get(self._config['engine'])(self._config['engine_params'], self._log)
        except Exception as err:
            self._keep_running = False
            self._log.error('Failed to load speaker engine')
            self._log.exception(err)

    def _on_recv_packet(self, packet: Packet) -> None:
        super()._on_recv_packet(packet)

        if packet.type is MessageType.SYNTHESIZE_MESSAGE_REQUEST:
            self._log.debug('Recieved synthesis request: txid=%s session=%d msg=%s',
                packet.metadata['txid'], packet.data['actor'], packet.data['msg'])
            try:
                with contexttimer.Timer(output=self._log.debug, prefix='engine.speak()'):
                    audio = self._engine.speak(undenotify(packet.data['msg']))
                if audio is None:
                    return
                self._log.debug('Generated audio duration: %0.2f seconds',
                    audio_duration(audio, SAMPLE_FRAME_RATE))
                c = self._engine.speak.cache_info()
                self._log.debug('speak() LRU hit rate: %0.2f', c.hits / (c.hits + c.misses))
            except Exception as err:
                self._log.exception(err)
            else:
                self._log.debug('Synthesis result: txid=%s session=%d buffer[]=%d',
                    packet.metadata['txid'], packet.data['actor'], len(audio))
                self._conn.send(Packet.forward(packet, MessageType.SYNTHESIZE_MESSAGE_RESPONSE, {
                    'buffer': audio,
                    'msg': packet.data['msg'],
                    'actor': packet.data['actor'],
                }).serialize())

SpeakerWorkerProcess = multiprocessify(SpeakerWorker)
