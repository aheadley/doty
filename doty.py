#!/usr/bin/env python3

import collections
import datetime
import logging
import subprocess
import multiprocessing
import threading
import functools
import hashlib
import html
import io
import itertools
import json
import os
import os.path
import queue
import random
import signal
import ssl
import struct
import subprocess
import tempfile
import time
import wave
import uuid
from enum import (
    Enum,
    auto,
)

import contexttimer
import googleapiclient.discovery
import irc.client
import irc.connection
import jsonschema
import numpy
import requests
import samplerate
import setproctitle
import snips_nlu
import wolframalpha
import yaml

from bs4 import BeautifulSoup
from fuzzywuzzy import process
from google.cloud import texttospeech as gcloud_texttospeech
from numpy_ringbuffer import RingBuffer
import pymumble_py3 as pymumble
from pymumble_py3.constants import (
    PYMUMBLE_AUDIO_PER_PACKET,
    PYMUMBLE_SAMPLERATE,
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_USERCREATED,
    PYMUMBLE_CLBK_USERUPDATED,
    PYMUMBLE_CLBK_USERREMOVED,
    PYMUMBLE_CLBK_CHANNELCREATED,
    PYMUMBLE_CLBK_CHANNELUPDATED,
    PYMUMBLE_CLBK_CHANNELREMOVED,
    PYMUMBLE_CLBK_TEXTMESSAGERECEIVED,
    PYMUMBLE_CLBK_SOUNDRECEIVED,
)
from pymumble_py3.errors import (
    UnknownChannelError,
)
import stt as coqui_speechtotext
import TTS as coqui_texttospeech
from watson_developer_cloud import SpeechToTextV1 as watson_speechtotext

APP_NAME = 'doty'
LOG_LEVEL = logging.INFO
# this must be less than PYMUMBLE_AUDIO_PER_PACKET (0.02)
SHORT_POLL = PYMUMBLE_AUDIO_PER_PACKET / 2
LONG_POLL = 0.1
ZW_SPACE = u'\u200B'
# https://www.isip.piconepress.com/projects/speech/software/tutorials/production/fundamentals/v1.0/section_02/s02_01_p05.html
WAV_HEADER_LEN = 44
# https://cloud.google.com/speech-to-text/quotas
MAX_TRANSCRIPTION_TIME = 60.0
BASE_CONFIG_FILENAME = 'config.yml.example'
SCHEMA_FILENAME = 'config.schema'
SOUNDCHUNK_SIZE = 1920
AUDIO_CHANNELS = 1
SAMPLE_FRAME_FORMAT = '<h'
SAMPLE_FRAME_SIZE = struct.calcsize(SAMPLE_FRAME_FORMAT)
VOLUME_ADJUSTMENT_STEP = 0.2
DEFAULT_MEDIA_VOLUME = 0.8
COMMAND_HELP_MESSAGE = """
I know the following commands: !help | !moveto <mumble channel> | !say <text to speak> | !replay [seconds] | !clip [seconds]
""".strip()

signal.signal(signal.SIGINT, signal.SIG_IGN)
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s.%(msecs)03d [%(process)07d] %(levelname)-7s %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logging.captureWarnings(True)

def multiprocessify(func):
    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        proc = multiprocessing.Process(target=func, args=pargs, kwargs=kwargs)
        proc.start()
        return proc
    return wrapper

def getWorkerLogger(worker_name, level=logging.DEBUG):
    log = logging.getLogger('{}.{}'.format(APP_NAME, worker_name))
    log.setLevel(level)
    return log

def deep_merge_dict(a, b):
    for k, v in b.items():
        if isinstance(b[k], dict):
            if k in a:
                if not isinstance(a[k], dict):
                    raise ValueError('Non-matching types for key: {}'.format(k))
                else:
                    a[k] = deep_merge_dict(a[k], b[k])
                    continue
        a[k] = b[k]
    return a

log = getWorkerLogger('main')

def normalize_callback_args(callback_type, args):
    if callback_type == PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:
        return ({
            'actor': args[0].actor,
            'channel_id': list(args[0].channel_id),
            'message': BeautifulSoup(args[0].message.strip(), 'html.parser').get_text(),
        },)
    elif callback_type == PYMUMBLE_CLBK_USERREMOVED:
        return (dict(args[0]), {'session': args[1].session})
    else:
        return tuple([dict(e) if isinstance(e, dict) else e for e in args])

def denote_callback(clbk_type):
    def outer_wrapper(func):
        @functools.wraps(func)
        def inner_wrapper(*args):
            return func(clbk_type, *args)
        return inner_wrapper
    return outer_wrapper

generate_uuid = lambda: str(uuid.uuid4())
sha1sum = lambda data: hashlib.sha1(data if type(data) in (bytes, bytearray) else data.encode('utf-8')).hexdigest()

def denotify_username(username):
    if len(username) > 1:
        return ZW_SPACE.join([username[0], username[1:-1], username[-1]]).replace(ZW_SPACE * 2, ZW_SPACE)
    else:
        return username

def audio_resample(buf, src_sr, dest_sr, method='sinc_best'):
    sr_ratio = float(dest_sr) / src_sr
    return samplerate.resample(buf, sr_ratio, converter_type=method).astype(numpy.int16)

def halt_process(proc):
    try:
        # only available in 3.7+
        proc.kill()
    except:
        proc.terminate()

class QueuePipe:
    @staticmethod
    def wait(*queue_pipes, timeout=None):
        return [qp for qp in queue_pipes if qp._in._reader in \
            multiprocessing.connection.wait([qp._in._reader for qp in queue_pipes], timeout)]

    def __init__(self):
        self._in = multiprocessing.Queue()
        self._out = multiprocessing.Queue()

        self._buffer = []

        self._in.cancel_join_thread()
        self._out.cancel_join_thread()

    def __setstate__(self, state):
        super().__setstate__(state)
        self._swap()

    def send(self, data):
        return self._out.put(data)

    def recv(self):
        try:
            return self._buffer.pop()
        except IndexError:
            return self._in.get()

    def poll(self, timeout=None):
        try:
            self._buffer.insert(0, self._in.get(timeout=timeout))
            return True
        except queue.Empty:
            return False

    def _swap(self):
        self._in, self._out = self._out, self._in

    def join(self):
        try:
            self._out.close()
            self._out.join_thread()
        except Exception: pass

class AssignableRingBuffer(RingBuffer):
    def _normalize_slice(self, idx):
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

    def __setitem__(self, idx, value):
        if isinstance(idx, slice):
            idx = self._normalize_slice(idx)
            assert (idx.stop - idx.start) == len(value), 'len(slice) != len(value): idx.start={} idx.stop={} diff={} len(value)={}'.format(idx.start, idx.stop, idx.stop - idx.start, len(value))
            assert len(value) < self._capacity, 'slice too large: len(value)={}'.format(len(value))

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
            raise IndexError('Invalid index type: %s'.format(idx))

class MixingBuffer:
    MIN_SILENCE_EXTENSION = 0.5
    DTYPE_SHORT = numpy.dtype(SAMPLE_FRAME_FORMAT)
    DTYPE_FLOAT = numpy.float32

    @staticmethod
    def _mix_frames(left, right):
        SCALE_VALUE = (2 ** ((SAMPLE_FRAME_SIZE * 8) - 1)) - 1
        scale_factor = 1/SCALE_VALUE
        left = left.astype(MixingBuffer.DTYPE_FLOAT) * scale_factor
        right = right.astype(MixingBuffer.DTYPE_FLOAT) * scale_factor
        out = (left + right) - (left * right)
        out *= SCALE_VALUE
        out = numpy.clip(out, -SCALE_VALUE, SCALE_VALUE).astype(MixingBuffer.DTYPE_SHORT)
        return out

    def __init__(self, buffer_len, log=None):
        if log is None:
            log = getWorkerLogger('mixer', level=LOG_LEVEL)
        self._log = log
        self._buffer_len = buffer_len
        self._reset()

    def add_chunk(self, chunk):
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
            log.warning('Failed to mix audio: [%d:%d]', mix_start_pos, mix_end_pos)
            log.debug('Mix stats: _buffer._left_index=%d _buffer._right_index=%d len(buf)=%d',
                self._buffer._left_index, self._buffer._right_index, len(buf))
            log.exception(err)

    def add_buffer(self, buf):
        now = time.time()
        if self._current_head > now:
            fake_chunk = pymumble.soundqueue.SoundChunk(
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

    def _extend_with_silence(self, silence_length=None, exact=False):
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

    def _reset(self):
        self._buffer = AssignableRingBuffer(self._seconds_to_frames(self._buffer_len), dtype=self.DTYPE_SHORT)
        self._buffer.extend(numpy.zeros(self._buffer._capacity, dtype=self.DTYPE_SHORT))
        self._current_head = time.time()

    def get_last(self, seconds):
        seconds = min(self._frames_to_seconds(len(self._buffer) - 1), seconds)
        frame_count = self._seconds_to_frames(seconds)
        buf = self._buffer[-frame_count:].tobytes()
        # round to PYMUMBLE_AUDIO_PER_PACKET
        return buf[len(buf) % SOUNDCHUNK_SIZE:]

    def get_all(self):
        buf = self._buffer[:].tobytes()
        # round to PYMUMBLE_AUDIO_PER_PACKET
        return buf[len(buf) % SOUNDCHUNK_SIZE:]

    def _seconds_to_frames(self, seconds):
        return int(round(seconds * PYMUMBLE_SAMPLERATE))

    def _frames_to_seconds(self, frames):
        return frames / PYMUMBLE_SAMPLERATE

class MumbleControlCommand(Enum):
    EXIT = auto()
    USER_CREATE = auto()
    USER_UPDATE = auto()
    USER_REMOVE = auto()
    CHANNEL_CREATE = auto()
    CHANNEL_UPDATE = auto()
    CHANNEL_REMOVE = auto()
    MOVE_TO_CHANNEL = auto()

    RECV_CHANNEL_TEXT_MSG = auto()
    SEND_CHANNEL_TEXT_MSG = auto()
    RECV_USER_TEXT_MSG = auto()
    SEND_USER_TEXT_MSG = auto()

    RECV_CHANNEL_AUDIO = auto()
    SEND_CHANNEL_AUDIO_MSG = auto()
    RECV_USER_AUDIO = auto()
    SEND_USER_AUDIO_MSG = auto()

    DROP_CHANNEL_AUDIO_BUFFER = auto()
    DROP_USER_AUDIO_BUFFER = auto()

class IrcControlCommand(Enum):
    EXIT = auto()
    USER_CREATE = auto()
    USER_UPDATE = auto()
    USER_REMOVE = auto()

    RECV_CHANNEL_TEXT_MSG = auto()
    SEND_CHANNEL_TEXT_MSG = auto()
    RECV_USER_TEXT_MSG = auto()
    SEND_USER_TEXT_MSG = auto()
    RECV_CHANNEL_ACTION = auto()
    SEND_CHANNEL_ACTION = auto()

class TranscriberControlCommand(Enum):
    EXIT = auto()
    TRANSCRIBE_MESSAGE = auto()
    TRANSCRIBE_MESSAGE_RESPONSE = auto()

class SpeakerControlCommand(Enum):
    EXIT = auto()
    SPEAK_MESSAGE = auto()
    SPEAK_MESSAGE_RESPONSE = auto()

class MediaControlCommand(Enum):
    EXIT = auto()
    PLAY_AUDIO_URL = auto()
    PLAY_VIDEO_URL = auto()
    AUDIO_CHUNK_READY = auto()
    STOP = auto()
    SET_VOLUME = auto()
    SET_VOLUME_LOWER = auto()
    SET_VOLUME_HIGHER = auto()

class RouterControlCommand(Enum):
    EXIT = auto()

@multiprocessify
def proc_irc(irc_config, router_conn):
    setproctitle.setproctitle('doty: irc worker')
    log = getWorkerLogger('irc', level=LOG_LEVEL)
    keep_running = True

    log.debug('Starting irc process')

    client = irc.client.Reactor()
    server = client.server()
    if irc_config['ssl']:
        ssl_ctx = ssl.create_default_context()
        if not irc_config['ssl_verify']:
            ssl_ctx.verify_mode = ssl.CERT_NONE
        conn_factory = irc.connection.Factory(
            wrapper=lambda s: ssl_ctx.wrap_socket(s, server_hostname=irc_config['server']),
            ipv6=irc_config['allow_ipv6'],
        )
    else:
        conn_factory = irc.connection.Factory(ipv6=irc_config['allow_ipv6'])

    e2d = lambda ev: {
        'type': ev.type,
        'source': ev.source.split('!')[0] if '!' in ev.source else ev.source,
        'target': ev.target,
        'args': ev.arguments,
    }
    def handle_irc_message(conn, event):
        log.debug('Recieved IRC event: %s', event)
        cmd_data = e2d(event)
        if event.type == 'pubmsg':
            cmd_data['cmd'] = IrcControlCommand.RECV_CHANNEL_TEXT_MSG
        elif event.type == 'privmsg':
            cmd_data['cmd'] = IrcControlCommand.RECV_USER_TEXT_MSG
        elif event.type == 'action':
            cmd_data['cmd'] = IrcControlCommand.RECV_CHANNEL_ACTION
        else:
            log.warning('Unrecognized event type: %s', event.type)
            return
        if cmd_data['source'] == irc_config['username']:
            # it's something we sent or triggered, ignore it
            log.debug('Ignoring event from ourself: %r', cmd_data)
        else:
            router_conn.send(cmd_data)
    for ev_type in ['pubmsg', 'privmsg', 'action']:
        client.add_global_handler(ev_type, handle_irc_message)

    def handle_membership_change(conn, event):
        log.debug('Recieved IRC join/part: %s', event)
        cmd_data = e2d(event)
        if event.type == 'join':
            cmd_data['cmd'] = IrcControlCommand.USER_CREATE
        elif event.type in ['kick', 'part', 'quit']:
            cmd_data['cmd'] = IrcControlCommand.USER_REMOVE
        elif event.type == 'nick':
            cmd_data['cmd'] = IrcControlCommand.USER_UPDATE
        else:
            log.warning('Unrecognized event type: %s', event.type)
            return
        if cmd_data['source'] == irc_config['username']:
            # it's something we sent or triggered, ignore it
            log.debug('Ignoring event from ourself: %r', cmd_data)
        else:
            router_conn.send(cmd_data)
    for ev_type in ['join', 'kick', 'part', 'quit', 'nick']:
        client.add_global_handler(ev_type, handle_membership_change)

    log.info('IRC client running')
    while keep_running:
        if not server.connected:
            server.connect(irc_config['server'], irc_config['port'],
                irc_config['username'], connect_factory=conn_factory)
            server.join(irc_config['channel'])

        client.process_once(SHORT_POLL)

        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            log.debug('Recieved control command: %r', cmd_data)
            if cmd_data['cmd'] == IrcControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                server.disconnect()
                keep_running = False
            elif cmd_data['cmd'] == IrcControlCommand.SEND_CHANNEL_TEXT_MSG:
                log.info('Sending message: %s', cmd_data['msg'].replace(ZW_SPACE, ''))
                server.privmsg(irc_config['channel'], cmd_data['msg'])
            elif cmd_data['cmd'] == IrcControlCommand.SEND_USER_TEXT_MSG:
                log.info('Sending message to user: %s -> %s',
                    cmd_data['user'], cmd_data['msg'])
                server.privmsg(cmd_data['user'], cmd_data['msg'])
            elif cmd_data['cmd'] == IrcControlCommand.SEND_CHANNEL_ACTION:
                log.info('Sending action: %s', cmd_data['msg'].replace(ZW_SPACE, ''))
                server.action(irc_config['channel'], cmd_data['msg'])
            else:
                log.warning('Unrecognized command: %r', cmd_data)
    log.debug('IRC process exiting')

@multiprocessify
def proc_mmbl(mmbl_config, router_conn, pymumble_debug=False):
    setproctitle.setproctitle('doty: mumble worker')
    log = getWorkerLogger('mmbl', level=LOG_LEVEL)
    keep_running = True
    clbk_lock = threading.Lock()

    EVENT_MAP = {
        PYMUMBLE_CLBK_USERCREATED:          MumbleControlCommand.USER_CREATE,
        PYMUMBLE_CLBK_USERUPDATED:          MumbleControlCommand.USER_UPDATE,
        PYMUMBLE_CLBK_USERREMOVED:          MumbleControlCommand.USER_REMOVE,
        PYMUMBLE_CLBK_CHANNELCREATED:       MumbleControlCommand.CHANNEL_CREATE,
        PYMUMBLE_CLBK_CHANNELUPDATED:       MumbleControlCommand.CHANNEL_UPDATE,
        PYMUMBLE_CLBK_CHANNELREMOVED:       MumbleControlCommand.CHANNEL_REMOVE,
        PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:  MumbleControlCommand.RECV_CHANNEL_TEXT_MSG,
        PYMUMBLE_CLBK_SOUNDRECEIVED:        MumbleControlCommand.RECV_CHANNEL_AUDIO,
    }

    log.debug('Starting mumble process')
    mmbl = pymumble.Mumble(mmbl_config['server'],
        mmbl_config['username'], password=mmbl_config['password'],
        port=mmbl_config['port'],
        debug=pymumble_debug,
        reconnect=False,
    )
    mmbl.set_receive_sound(True)

    log.debug('Setting up callbacks')
    def handle_callback(ev_type, *args):
        args = normalize_callback_args(ev_type, args)
        if ev_type == PYMUMBLE_CLBK_SOUNDRECEIVED:
            # avoid memory leak in pymumble
            try:
                mmbl.users[args[0]['session']].sound.get_sound()
            except Exception: pass
        else:
            log.debug('Callback event: %s => %r', ev_type, args)

        cmd_data = {'cmd': EVENT_MAP.get(ev_type, None)}
        if cmd_data['cmd'] in (MumbleControlCommand.USER_CREATE, MumbleControlCommand.USER_UPDATE, MumbleControlCommand.USER_REMOVE):
            cmd_data['user'] = args[0]
            if cmd_data['cmd'] == MumbleControlCommand.USER_UPDATE:
                cmd_data['changes'] = args[1]
            elif cmd_data['cmd'] == MumbleControlCommand.USER_REMOVE:
                cmd_data['session'] = args[1]
        elif cmd_data['cmd'] in (MumbleControlCommand.CHANNEL_CREATE, MumbleControlCommand.CHANNEL_UPDATE, MumbleControlCommand.CHANNEL_REMOVE):
            cmd_data['channel'] = args[0]
            if cmd_data['cmd'] == MumbleControlCommand.CHANNEL_UPDATE:
                cmd_data['changes'] = args[1]
        elif cmd_data['cmd'] == MumbleControlCommand.RECV_CHANNEL_TEXT_MSG:
            if not args[0]['channel_id']:
                # it's a private message
                cmd_data['cmd'] = MumbleControlCommand.RECV_USER_TEXT_MSG
            cmd_data.update(args[0])
        elif cmd_data['cmd'] == MumbleControlCommand.RECV_CHANNEL_AUDIO:
            if not args[0]['channel_id']:
                # it's a private message
                cmd_data['cmd'] = MumbleControlCommand.RECV_USER_AUDIO
            cmd_data.update(args[0])
            cmd_data['chunk'] = args[1]
        else:
            log.warning('Unrecognized event type: %s', ev_type)
            return
        with clbk_lock:
            router_conn.send(cmd_data)

    for callback_type in mmbl.callbacks.get_callbacks_list():
        clbk = denote_callback(callback_type)(handle_callback)
        mmbl.callbacks.add_callback(callback_type, clbk)

    log.debug('Starting mumble connection')
    mmbl.start()
    mmbl.is_ready()
    log.info('Connected to mumble server: %s:%d',
        mmbl_config['server'], mmbl_config['port'])

    log.debug('Entering control loop')
    while keep_running:
        if router_conn.poll(SHORT_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == MumbleControlCommand.EXIT:
                log.debug('Recieved exit command from router')
                keep_running = False
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_CHANNEL_TEXT_MSG:
                if 'channel_id' not in cmd_data:
                    cmd_data['channel_id'] = mmbl.users.myself['channel_id']
                log.info('Sending text message to channel: %s => %s',
                    mmbl.channels[cmd_data['channel_id']]['name'], cmd_data['msg'])
                mmbl.channels[cmd_data['channel_id']].send_text_message(html.escape(cmd_data['msg']))
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_USER_TEXT_MSG:
                log.info('Sending text message to user: %s => %s',
                    mmbl.users[cmd_data['session_id']]['name'], cmd_data['msg'])
                mmbl.users[cmd_data['session_id']].send_message(html.escape(cmd_data['msg']))
            elif cmd_data['cmd'] == MumbleControlCommand.MOVE_TO_CHANNEL:
                log.info('Joining channel: %s', cmd_data['channel_name'])
                try:
                    target_channel = mmbl.channels.find_by_name(cmd_data['channel_name'])
                except UnknownChannelError:
                    log.debug('Channel doesnt exist, attempting to create: %s', cmd_data['channel_name'])
                    mmbl.channels.new_channel(0, cmd_data['channel_name'])
                else:
                    mmbl.users.myself.move_in(target_channel['channel_id'])
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_CHANNEL_AUDIO_MSG:
                # log.debug('Sending audio message: %d bytes', len(cmd_data['buffer']))
                mmbl.sound_output.add_sound(cmd_data['buffer'])
            elif cmd_data['cmd'] == MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER:
                log.debug('Dropping outgoing audio buffer')
                mmbl.sound_output.clear_buffer()
            else:
                log.warning('Unrecognized command: %r', cmd_data)
        if not mmbl.is_alive():
            log.warning('Mumble connection has died')
            keep_running = False
    log.debug('Mumble process exiting')

class CoquiSTTEngine:
    def __init__(self, engine_params, logger):
        self._log = logger
        self._model = coqui_speechtotext.Model(engine_params['model_path'])
        self._model.enableExternalScorer(engine_params['scorer_path'])
        self._resample_method = engine_params['resample_method']

    def transcribe(self, buf, phrases=[]):
        audio_data = numpy.frombuffer(buf, dtype=numpy.int16)
        model_samplerate = self._model.sampleRate()
        if model_samplerate != PYMUMBLE_SAMPLERATE:
            audio_data = audio_resample(audio_data, PYMUMBLE_SAMPLERATE,
                model_samplerate, self._resample_method)

        try:
            result = self._model.sttWithMetadata(audio_data)
        except Exception as err:
            self._log.exception(err)
        else:
            for transcript in result.transcripts:
                value = ''.join(t.text for t in transcript.tokens).strip()
                if value:
                    return {
                        'transcript': value,
                        'confidence': transcript.confidence / len(transcript.tokens),
                    }
        return None

class VoskSTTEngine:
    KALDI_SAMPLERATE = 16000
    def __init__(self, engine_params, logger):
        from vosk import Model, KaldiRecognizer, SetLogLevel

        self._log = logger
        self._resample_method = engine_params['resample_method']
        SetLogLevel(0 if EXTRA_DEBUG else -1000)
        model = Model(engine_params['model_path'])
        self._recognizer = KaldiRecognizer(model, self.KALDI_SAMPLERATE)

    def transcribe(self, buf, phrases=[]):
        audio = bytes(audio_resample(
            numpy.frombuffer(buf, dtype=numpy.int16),
            PYMUMBLE_SAMPLERATE, self.KALDI_SAMPLERATE, 
            self._resample_method))
        self._recognizer.AcceptWaveform(audio)
        result = json.loads(self._recognizer.FinalResult())

        try:
            if result['text'].strip():
                return {
                    'transcript': result['text'].strip(),
                    'confidence': sum(t['conf'] for t in result['result']) / len(result['result']) \
                        if 'result' in result else 0.0,
                }
        except Exception as err:
            log.exception(err)
        return None

class IBMWatsonEngine:
    def __init__(self, engine_params, logger):
        self._log = logger
        self._client = watson_speechtotext(
            iam_apikey=engine_params['api_key'],
            url=engine_params['service_url'],
        )
        self._hint_phrases = engine_params['hint_phrases']
        self._model = engine_params['model']

    def transcribe(self, buf, phrases=[]):
        resp = self._client.recognize(
            audio=io.BytesIO(buf),
            content_type='audio/l16; rate={:d}; channels=1; endianness=little-endian'.format(PYMUMBLE_SAMPLERATE),
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
            return {
                'transcript': value,
                'confidence': sum(conf) / len(conf),
            }
        return None

@multiprocessify
def proc_transcriber(transcription_config, router_conn):
    setproctitle.setproctitle('doty: transcriber worker')
    log = getWorkerLogger('transcriber', level=LOG_LEVEL)
    keep_running = True
    log.debug('Transcribing starting up')

    try:
        engine = {
            'coqui-stt': CoquiSTTEngine,
            'vosk': VoskSTTEngine,
            'ibm-watson': IBMWatsonEngine,
        }.get(transcription_config['engine'])(transcription_config['engine_params'], log)
    except Exception as err:
        keep_running = False
        log.error('Failed to load transcription engine')
        log.exception(err)

    log.info('Transcriber running: %s', transcription_config['engine'])
    while keep_running:
        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == TranscriberControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                keep_running = False
            elif cmd_data['cmd'] == TranscriberControlCommand.TRANSCRIBE_MESSAGE:
                if transcription_config['save_only']:
                    result = {'transcript': 'NO_DATA', 'confidence': 0.0}
                else:
                    with contexttimer.Timer(output=log.debug, prefix='engine.transcribe()'):
                        result = engine.transcribe(cmd_data['buffer'], cmd_data['phrases'])
                if result:
                    if not transcription_config['save_only']:
                        log.debug('Transcription result: txid=%s actor=%d result=%r',
                            cmd_data['txid'], cmd_data['actor'], result)
                        router_conn.send({
                            'cmd': TranscriberControlCommand.TRANSCRIBE_MESSAGE_RESPONSE,
                            'actor': cmd_data['actor'],
                            'result': result,
                            'txid': cmd_data['txid'],
                        })
                    if transcription_config['save_to']:
                        base_path = os.path.join(transcription_config['save_to'], cmd_data['username'])
                        if not os.path.exists(os.path.join(base_path, 'wavs')):
                            os.makedirs(os.path.join(base_path, 'wavs'), mode=0o755)
                        wav_fname = cmd_data['txid'] + '.wav'
                        map_fname = 'metadata.csv'
                        with wave.open(os.path.join(base_path, 'wavs', wav_fname), 'wb') as wav_file:
                            wav_file.setnchannels(AUDIO_CHANNELS)
                            wav_file.setsampwidth(2)
                            wav_file.setframerate(PYMUMBLE_SAMPLERATE)
                            wav_file.writeframes(cmd_data['buffer'])
                        with open(os.path.join(base_path, map_fname), 'a') as map_file:
                            map_file.write('{},{}\n'.format(wav_fname, result['transcript']))
                else:
                    log.debug('No transcription result for: txid=%s, actor=%d',
                        cmd_data['txid'], cmd_data['actor'])
            else:
                log.warning('Unrecognized command: %r', cmd_data)
    log.debug('Transcriber process exiting')

class CoquiTTSEngine:
    def __init__(self, engine_params, logger):
        self._log = logger
        from TTS.utils.manage import ModelManager
        from TTS.utils.synthesizer import Synthesizer

        self._resample_method = engine_params['resample_method']
        self._model_manager = ModelManager(
            os.path.join(os.path.dirname(coqui_texttospeech.__file__), '.models.json'))
        model_path, model_config_path, model_item = self._model_manager.download_model(
            'tts_models/' + engine_params['model'])
        vocoder_path, vocoder_config_path, _ = self._model_manager.download_model(
            'vocoder_models/' + engine_params['vocoder'] if engine_params['vocoder'] else model_item['default_vocoder'])

        self._synth = Synthesizer(
            model_path,
            model_config_path,
            None, # speakers_file_path
            vocoder_path,
            vocoder_config_path,
            None, # encoder_path
            None, # encoder_config_path
            False, # use_cuda
        )

    def speak(self, text):
        audio = numpy.array(self._synth.tts(text, None, None))
        norm_audio = audio * (32767 / max(0.01, numpy.max(numpy.abs(audio))))
        resampled_audio = audio_resample(norm_audio,
            self._synth.output_sample_rate, PYMUMBLE_SAMPLERATE, self._resample_method)
        return bytes(resampled_audio)

class GoogleTTSEngine:
    def __init__(self, engine_params, logger):
        self._log = logger
        self._client = gcloud_texttospeech.TextToSpeechClient.from_service_account_json(engine_params['credentials_path'])
        self._voice = gcloud_texttospeech.VoiceSelectionParams(
            language_code=engine_params['language'],
            name=engine_params['voice'],
        )
        self._config = gcloud_texttospeech.AudioConfig(
            audio_encoding=gcloud_texttospeech.AudioEncoding.LINEAR16,
            sample_rate_hertz=PYMUMBLE_SAMPLERATE,
            effects_profile_id=engine_params['effect_profiles'],
            speaking_rate=engine_params['speed'],
            pitch=engine_params['pitch'],
            volume_gain_db=engine_params['volume'],
        )

    def speak(self, text):
        text_input = gcloud_texttospeech.SynthesisInput(text=text)
        response = self._client.synthesize_speech(input=text_input,
            voice=self._voice, audio_config=self._config)
        return response.audio_content[WAV_HEADER_LEN:]

@multiprocessify
def proc_speaker(speaker_config, router_conn):
    setproctitle.setproctitle('doty: speaker worker')
    log = getWorkerLogger('speaker', level=LOG_LEVEL)
    keep_running = True
    log.debug('Speaker starting up')
    os.nice(5)

    try:
        engine = {
            'coqui-tts': CoquiTTSEngine,
            'gcloud-tts': GoogleTTSEngine,
        }.get(speaker_config['engine'])(speaker_config['engine_params'], log)
    except Exception as err:
        keep_running = False
        log.error('Failed to load speaker engine')
        log.exception(err)

    @functools.lru_cache
    def speak(msg):
        return engine.speak(msg)

    log.info('Speaker running: %s', speaker_config['engine'])
    while keep_running:
        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == SpeakerControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                keep_running = False
            elif cmd_data['cmd'] == SpeakerControlCommand.SPEAK_MESSAGE:
                log.debug('Recieved speaker request: txid=%s session=%d msg=%s',
                    cmd_data['txid'], cmd_data['actor'], cmd_data['msg'])
                try:
                    with contexttimer.Timer(output=log.debug, prefix='engine.speak()'):
                        audio = speak(cmd_data['msg'].replace(ZW_SPACE, ''))
                    c = speak.cache_info()
                    log.debug('speak() LRU hit rate: %0.2f', c.hits / (c.hits + c.misses))
                except Exception as err:
                    log.exception(err)
                else:
                    log.debug('Speaker result: txid=%s session=%d buffer[]=%d',
                        cmd_data['txid'], cmd_data['actor'], len(audio))
                    router_conn.send({
                        'cmd': SpeakerControlCommand.SPEAK_MESSAGE_RESPONSE,
                        'buffer': audio,
                        'msg': cmd_data['msg'],
                        'actor': cmd_data['actor'],
                        'txid': cmd_data['txid'],
                    })
            else:
                log.warning('Unrecognized command: %r', cmd_data)
    log.debug('Speaker process exiting')

@multiprocessify
def proc_media(media_config, router_conn):
    setproctitle.setproctitle('doty: media worker')
    log = getWorkerLogger('media', level=LOG_LEVEL)
    keep_running = True
    log.debug('Media worker starting up')

    FFMPEG_CMD = lambda input_arg: [
        'ffmpeg',
        '-loglevel',    'quiet',
        '-hide_banner',
        '-y',
        '-re',
        '-i',           input_arg,
        '-ar',          str(PYMUMBLE_SAMPLERATE),
        '-ac',          '1',
        '-f',           's16le',
        '-'
    ]
    YOUTUBE_DL_CMD = lambda video_url: [
        'youtube-dl',
        '--quiet',
        '--no-warnings',
        '--no-progress',
        '--prefer-free-formats',
        '--output',     '-',
        video_url,
    ]

    ctx = {
        'youtube-dl': None,
        'ffmpeg': None,
        'txid': None,
    }
    volume = DEFAULT_MEDIA_VOLUME
    constrain_volume = lambda v: max(0.1, min(1.0, v))
    def adjust_volume(chunk, volume_ratio):
        d = numpy.frombuffer(chunk, dtype=numpy.int16) * volume_ratio
        return d.astype(dtype=numpy.int16, casting='unsafe').tobytes()

    def reset():
        if ctx['txid'] is not None:
            log.debug('Stopping txid=%s', ctx['txid'])
        if ctx['youtube-dl'] is not None:
            halt_process(ctx['youtube-dl'])
            ctx['youtube-dl'] = None
        if ctx['ffmpeg'] is not None:
            halt_process(ctx['ffmpeg'])
            ctx['ffmpeg'] = None
        ctx['txid'] = None


    log.info('Media worker running')
    while keep_running:
        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == MediaControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                reset()
                keep_running = False
            elif cmd_data['cmd'] == MediaControlCommand.STOP:
                log.debug('Recieved STOP command from router')
                reset()
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME:
                volume = constrain_volume(cmd_data['value'])
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME_LOWER:
                volume = constrain_volume(volume - VOLUME_ADJUSTMENT_STEP)
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.SET_VOLUME_HIGHER:
                volume = constrain_volume(volume + VOLUME_ADJUSTMENT_STEP)
                log.debug('Volume set to: %0.2f', volume)
            elif cmd_data['cmd'] == MediaControlCommand.PLAY_AUDIO_URL:
                reset()
                ctx['txid'] = generate_uuid()
                log.info('PLAY_AUDIO_URL: txid=%s url=%s', ctx['txid'], cmd_data['url'])
                cmd = FFMPEG_CMD(cmd_data['url'])
                log.debug('Running cmd: %r', cmd)
                ctx['ffmpeg'] = subprocess.Popen(FFMPEG_CMD(cmd_data['url']),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            elif cmd_data['cmd'] == MediaControlCommand.PLAY_VIDEO_URL:
                reset()
                ctx['txid'] = generate_uuid()
                log.info('PLAY_VIDEO_URL: txid=%s url=%s', ctx['txid'], cmd_data['url'])
                cmd = YOUTUBE_DL_CMD(cmd_data['url'])
                log.debug('Running cmd: %r', cmd)
                ctx['youtube-dl'] = subprocess.Popen(cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                cmd = FFMPEG_CMD('-')
                log.debug('Running cmd: %r', cmd)
                ctx['ffmpeg'] = subprocess.Popen(cmd,
                    stdin=ctx['youtube-dl'].stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            else:
                log.warning('Unrecognized command: %r', cmd_data)

        if ctx['youtube-dl'] is not None:
            if ctx['youtube-dl'].poll() is not None:
                log.debug('youtube-dl proc finished: txid=%s', ctx['txid'])
                ctx['youtube-dl'] = None

        chunk = None
        if ctx['ffmpeg'] is not None:
            chunk = ctx['ffmpeg'].stdout.read(PYMUMBLE_SAMPLERATE * SAMPLE_FRAME_SIZE)
            if ctx['ffmpeg'].poll() is not None:
                log.debug('ffmpeg proc finished: txid=%s', ctx['txid'])
                ctx['ffmpeg'] = None

            if chunk:
                router_conn.send({
                    'cmd': MediaControlCommand.AUDIO_CHUNK_READY,
                    'buffer': adjust_volume(chunk, volume),
                    'txid': ctx['txid'],
                })

    log.debug('Media worker process exiting')

@multiprocessify
def proc_router(router_config, mmbl_conn, irc_conn, trans_conn, speak_conn, media_conn, master_conn):
    setproctitle.setproctitle('doty: router worker')
    log = getWorkerLogger('router', level=LOG_LEVEL)
    keep_running = True
    log.debug('Router starting up')

    MMBL_CHANNELS = {}
    MMBL_USERS = {}
    IRC_USERS = []
    AUDIO_BUFFERS = {}
    MIXED_BUFFER = MixingBuffer(router_config['mixed_buffer_len'], log)

    mmbl_conn._swap()
    irc_conn._swap()
    trans_conn._swap()
    speak_conn._swap()
    media_conn._swap()

    def say(msg, source_id=0):
        log.debug('Speaking message: %s', msg)
        return speak_conn.send({
            'cmd': SpeakerControlCommand.SPEAK_MESSAGE,
            'msg': msg,
            'actor': source_id,
            'txid': generate_uuid(),
            })
    def text(msg):
        irc_conn.send({
            'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
            'msg': msg,
        })
        mmbl_conn.send({
            'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
            'msg': msg,
        })

    def silent_chunk(last_packet, offset):
        dt = offset * (PYMUMBLE_AUDIO_PER_PACKET / missing_packets)
        return pymumble.soundqueue.SoundChunk(
            bytes(SOUNDCHUNK_SIZE),
            last_chunk.sequence + SHORT_POLL + (offset * SHORT_POLL),
            SOUNDCHUNK_SIZE,
            last_chunk.time + SHORT_POLL + dt,
            last_chunk.type,
            last_chunk.target,
            timestamp=last_chunk.timestamp + SHORT_POLL + dt,
        )

    def buffer2wav(buf):
        output = io.BytesIO()
        with io.BytesIO() as output:
            with wave.open(output, 'wb') as wav_output:
                wav_output.setnchannels(1)
                wav_output.setsampwidth(2)
                wav_output.setframerate(PYMUMBLE_SAMPLERATE)
                wav_output.writeframes(buf)
                return output.getvalue()

    def upload(handle):
        resp = requests.post('https://0x0.st/', files={'file': handle})
        return resp.text.strip()

    class CommandParser:
        DATASET_FILENAMES = [
            'command-dataset.yml',
        ]
        UNKNOWN_INTENT_RESPONSES = [
            'Sorry, I didn\'t understand that.',
            'What was that?',
            'Sorry, could you say that again?',
            'What did you say?',
            'Would you repeat that?',
            'I don\'t know what you mean.',
        ]
        INTERSTITIAL_RESPONSES = [
            'Uhmm',
            'Let me see',
            'Hhmm',
        ]
        YOUTUBE_URL_FMT = 'https://www.youtube.com/watch?v={vid}'
        BANG_CHARACTER = '!'

        _intent_registry = {}
        _bang_registry = {}

        def handle_intent(intent):
            def wrapper(func):
                try:
                    func.intents.append(intent)
                except AttributeError:
                    func.intents = [intent]
                return func
            return wrapper

        def handle_bang(bang):
            def wrapper(func):
                try:
                    func.bangs.append(bang)
                except AttributeError:
                    func.bangs = [bang]
                return func
            return wrapper

        def __init__(self, config):
            self._setup_registry()
            self._prepare_radio_stations(config['icecast_base_url'])
            self._engine = self._setup_parse_engine()
            self._wa_client = self._setup_wolfram_alpha(config['wolframalpha_api_key'])
            self._yt_client = self._setup_youtube(config['youtube_api_key'])

        def _setup_registry(self):
            for member in self.__class__.__dict__.values():
                try:
                    for intent in member.intents:
                        self.__class__._intent_registry[intent] = member
                        log.debug('Set handler for intent: %s -> %s', intent, member.__name__)
                except AttributeError: pass
                try:
                    for bang in member.bangs:
                        self.__class__._bang_registry[bang] = member
                        log.debug('Set handler for bang: %s -> %s', bang, member.__name__)
                except AttributeError: pass

        def _prepare_radio_stations(self, icecast_base_url):
            station_index = requests.get(icecast_base_url + '/status-json.xsl').json()
            self._station_map = {
                s['title']: icecast_base_url + s['listenurl'].strip('.')
                for s in station_index['icestats']['source']
            }

        def _fixup_dataset(self, dataset):
            for e in dataset.entities:
                if e.name == 'radio_station':
                    e.utterances = [snips_nlu.dataset.entity.EntityUtterance(k.lower())
                        for k in self._station_map]
                    break
            return dataset

        def _setup_wolfram_alpha(self, api_key):
            return wolframalpha.Client(api_key)

        def _setup_parse_engine(self):
            engine = snips_nlu.SnipsNLUEngine(config=snips_nlu.default_configs.CONFIG_EN)
            log.debug('Training model on datasets: %s', ', '.join(self.DATASET_FILENAMES))
            with contexttimer.Timer() as t:
                dataset = snips_nlu.dataset.Dataset.from_yaml_files('en',
                    [os.path.join(os.path.dirname(__file__), ds) for ds in self.DATASET_FILENAMES])
                dataset = self._fixup_dataset(dataset)
                engine.fit(dataset.json)
            log.debug('Model trained in %0.3fs', t.elapsed)
            return engine

        def _setup_youtube(self, api_key):
            client = googleapiclient.discovery.build(
                'youtube', 'v3', developerKey=api_key)
            return client

        def _duration_to_dt(self, duration):
            dt = datetime.timedelta(
                days=duration['days'],
                seconds=duration['seconds'],
                minutes=duration['minutes'],
                hours=duration['hours'],
                weeks=duration['weeks'])
            return dt

        def _buy_time_to_answer(self):
            say(random.choice(self.INTERSTITIAL_RESPONSES))

        def dispatch_intent(self, src, msg):
            result = self._engine.parse(msg)
            log.debug('Intent parse result: %s', result)
            intent = result['intent']['intentName'] or '__UNKNOWN__'
            try:
                handler = self.__class__._intent_registry[intent]
            except KeyError:
                log.warning('No registered handler for intent: %s', intent)
            else:
                slots = {s['slotName']: s['value'] for s in result['slots']}
                for k in slots:
                    try:
                        slots[k] = slots[k]['value']
                    except (TypeError, KeyError, IndexError):
                        continue
                return handler(self, src, result['input'], **slots)

        def dispatch_bang(self, src, msg):
            try:
                bang, args = msg.split(' ', 1)
            except ValueError:
                bang = msg
                args = None
            try:
                handler = self.__class__._bang_registry[bang[1:]]
            except KeyError:
                pass
            else:
                return handler(self, src, args)

        @handle_bang('help')
        def bang_help(self, src, input):
            help_msg = ', '.join(self.BANG_CHARACTER + bang for bang in self._bang_registry.keys())
            text('Available commands: ' + help_msg)

        @handle_bang('say')
        def bang_say(self, src, input):
            if input is None:
                text('{}: {}say <message>'.format(src['name'], self.BANG_CHARACTER))
            else:
                say('{} said: {}'.format(denotify_username(src['name']), input),
                    source_id=src.get('actor', 0))

        @handle_bang('play-ytdl')
        def bang_play_yt(self, src, input):
            if input is None:
                text('{}: {}play-ytdl <url to play>'.format(src['name'], self.BANG_CHARACTER))
            else:
                text('Attempting to play: {}'.format(input))
                media_conn.send({
                    'cmd': MediaControlCommand.PLAY_VIDEO_URL,
                    'url': input,
                })

        @handle_bang('play')
        def bang_play_raw(self, src, input):
            if input is None:
                text('{}: {}play <url to play>'.format(src['name'], self.BANG_CHARACTER))
            else:
                text('Attempting to play: {}'.format(input))
                media_conn.send({
                    'cmd': MediaControlCommand.PLAY_AUDIO_URL,
                    'url': input,
                })

        @handle_intent('__UNKNOWN__')
        def unknown_intent(self, src, input):
            say(random.choice(self.UNKNOWN_INTENT_RESPONSES))

        @handle_intent('greeting')
        def cmd_greeting(self, src, input):
            say('Hello {}'.format(src['name']))

        @handle_intent('help')
        def cmd_help(self, src, input):
            say('You can tell me to: make an audio clip or replay audio, change '
                'to a different channel, or just ask a question. You can also '
                'tell me to play radio stations and change the volume')

        @handle_intent('cmd_generate_clip')
        def cmd_generate_clip(self, src, input, duration=None):
            if duration is None:
                seconds = router_config['mixed_buffer_len'] / 2
            else:
                seconds = min(self._duration_to_dt(duration).seconds, router_config['mixed_buffer_len'])

            with io.BytesIO(buffer2wav(MIXED_BUFFER.get_last(seconds))) as f:
                url = upload(f)
            if url:
                resp = 'Clip of the last {} seconds: {}'.format(
                    seconds, url)
                text(resp)
            else:
                say('I wasn\'t able to upload the audio clip')
                log.warning('Failed to upload audio clip')

        @handle_intent('cmd_replay_audio')
        def cmd_replay_audio(self, src, input, duration=None):
            if duration is None:
                say('You need to include how long you want me to replay')
                return
            seconds = min(self._duration_to_dt(duration).seconds, router_config['mixed_buffer_len'])

            buf = MIXED_BUFFER.get_last(seconds)
            MIXED_BUFFER.add_buffer(buf)
            mmbl_conn.send({
                'cmd': MumbleControlCommand.SEND_CHANNEL_AUDIO_MSG,
                'buffer': buf,
            })

        @handle_intent('cmd_change_channel')
        def cmd_change_channel(self, src, input, destination=None):
            if destination is None:
                say('Include the channel I should move to')
                return

            channel = process.extractOne(destination, [c['name'] for c in MMBL_CHANNELS.values()])[0]
            log.info('Attempting to move to channel: %s', channel)
            mmbl_conn.send({
                'cmd': MumbleControlCommand.MOVE_TO_CHANNEL,
                'channel_name': channel,
            })

        @handle_intent('cmd_wolfram_alpha_query')
        def cmd_wolfram_alpha_query(self, src, input, query=None):
            if query is None:
                say(random.choice(self.UNKNOWN_INTENT_RESPONSES))
                return
            self._buy_time_to_answer()
            result = self._wa_client.query(input)
            try:
                answer = ' '.join(next(result.results).text.split('\n')).replace('|', '').strip()
            except (Exception, StopIteration):
                say('I don\'t know')
            else:
                say(answer)

        @handle_intent('cmd_silence')
        def cmd_silence(self, src, input):
            media_conn.send({
                'cmd': MediaControlCommand.STOP,
            })
            mmbl_conn.send({
                'cmd': MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER,
            })

        @handle_intent('cmd_set_volume')
        def cmd_set_volume(self, src, input, value=None):
            if value is None:
                say('Tell me what to set the volume to, between 10 and 100')
                return
            value = max(10, min(100, int(value))) / 100
            media_conn.send({
                'cmd': MediaControlCommand.SET_VOLUME,
                'value': value,
            })
            text('Media volume set to {:02d}%'.format(int(value * 100)))

        @handle_intent('cmd_lower_volume')
        def cmd_lower_volume(self, src, input):
            media_conn.send({
                'cmd': MediaControlCommand.SET_VOLUME_LOWER,
            })
            text('Media volume decreased by {:02d}%'.format(int(VOLUME_ADJUSTMENT_STEP * 100)))

        @handle_intent('cmd_raise_volume')
        def cmd_raise_volume(self, src, input):
            media_conn.send({
                'cmd': MediaControlCommand.SET_VOLUME_HIGHER,
            })
            text('Media volume increased by {:02d}%'.format(int(VOLUME_ADJUSTMENT_STEP * 100)))

        @handle_intent('cmd_play_radio')
        def cmd_play_radio(self, src, input, station=None):
            if station is None:
                say('Tell me what radio station to play')
                return
            key, _ = process.extractOne(station, self._station_map.keys())
            media_conn.send({
                'cmd': MediaControlCommand.PLAY_AUDIO_URL,
                'url': self._station_map[key],
            })
            mmbl_conn.send({
                'cmd': MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER,
            })
            text('Playing radio station: {}'.format(key))

        @handle_intent('cmd_list_stations')
        def cmd_list_stations(self, src, input):
            text('Available radio stations: ' + ', '.join(self._station_map.keys()))

        @handle_intent('cmd_play_media')
        def cmd_play_media(self, src, input, source=None, track=None, artist=None, album=None):
            if source is None:
                # only youtube is supported for now
                source = 'youtube'
            if track is None:
                say('Tell me the name of a song to play')
                return
            query = track
            if artist is not None:
                query += ' - {}'.format(artist)
            elif album is not None:
                query += ' - {}'.format(album)

            try:
                for video in self._yt_client.search().list(
                            part='snippet,id', q=query, type='video',
                            videoDimension='2d',
                            regionCode='US',
                            safeSearch='moderate',
                            # videoDuration='short',
                        ).execute()['items']:
                    selected_video = video
                    break
            except Exception as err:
                log.exception(err)
                say('I couldn\'t find anything to play for that')
                return
            url = self.YOUTUBE_URL_FMT.format(vid=selected_video['id']['videoId'])
            log.debug('Selected video: id=%s title=%s', selected_video['id']['videoId'], selected_video['snippet']['title'])
            media_conn.send({
                'cmd': MediaControlCommand.PLAY_VIDEO_URL,
                'url': url,
            })
            text('Playing media: "{}" <{}>'.format(selected_video['snippet']['title'], url))

    if router_config['enable_commands']:
        cmd_engine = CommandParser(router_config['command_params'])

    if router_config['startup_message']:
        say(router_config['startup_message'])

    log.info('Router running')
    while keep_running:
        ready = QueuePipe.wait(mmbl_conn, irc_conn, trans_conn, speak_conn, media_conn, master_conn, timeout=SHORT_POLL)
        if irc_conn in ready:
            cmd_data = irc_conn.recv()
            if cmd_data['cmd'] in (IrcControlCommand.RECV_CHANNEL_TEXT_MSG, IrcControlCommand.RECV_USER_TEXT_MSG):
                if cmd_data['source'] not in IRC_USERS:
                    IRC_USERS.append(cmd_data['source'])
                if cmd_data['source'] not in router_config['ignore']:
                    for msg in cmd_data['args']:
                        mmbl_conn.send({
                            'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                            'msg': '<{}> {}'.format(cmd_data['source'], msg),
                        })
                        if router_config['enable_commands']:
                            if any(msg.startswith(w) for w in router_config['command_params']['activation_words']):
                                try:
                                    _, cmd = msg.split(' ', 1)
                                except ValueError: pass
                                else:
                                    cmd_engine.dispatch_intent({'name': cmd_data['source']}, cmd)
                            elif msg.startswith(cmd_engine.BANG_CHARACTER):
                                cmd_engine.dispatch_bang({'name': cmd_data['source']}, msg)
            elif cmd_data['cmd'] == IrcControlCommand.USER_CREATE:
                if cmd_data['source'] not in IRC_USERS:
                    IRC_USERS.append(cmd_data['source'])
                mmbl_conn.send({
                    'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': '* {} has joined the IRC channel'.format(denotify_username(cmd_data['source']))
                })
            elif cmd_data['cmd'] == IrcControlCommand.USER_UPDATE:
                if cmd_data['source'] in IRC_USERS:
                    IRC_USERS.remove(cmd_data['source'])
                IRC_USERS.append(cmd_data['target'])
                mmbl_conn.send({
                    'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': '* {} is now known as {} in IRC'.format(
                        denotify_username(cmd_data['source']),
                        denotify_username(cmd_data['target']),
                )})
            elif cmd_data['cmd'] == IrcControlCommand.USER_REMOVE:
                if cmd_data['source'] in IRC_USERS:
                    IRC_USERS.remove(cmd_data['source'])
                mmbl_conn.send({
                    'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': '* {} has left the IRC channel'.format(denotify_username(cmd_data['source']))
                })
            else:
                log.warning('Recieved unknown command from IRC: %r', cmd_data)

        if mmbl_conn in ready:
            cmd_data = mmbl_conn.recv()
            if cmd_data['cmd'] == MumbleControlCommand.USER_CREATE:
                MMBL_USERS[cmd_data['user']['session']] = cmd_data['user']
                AUDIO_BUFFERS[cmd_data['user']['session']] = {
                    'buffer': collections.deque(
                        maxlen=int(MAX_TRANSCRIPTION_TIME / PYMUMBLE_AUDIO_PER_PACKET) - 1),
                    'last_sample': None,
                }
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                    'msg': '{} has connected in channel: {}'.format(
                        denotify_username(cmd_data['user']['name']),
                        MMBL_CHANNELS[cmd_data['user']['channel_id']]['name'],
                )})
            elif cmd_data['cmd'] == MumbleControlCommand.USER_UPDATE:
                MMBL_USERS[cmd_data['user']['session']].update(cmd_data['changes'])
                if 'channel_id' in cmd_data['changes']:
                    irc_conn.send({
                        'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                        'msg': '{} has joined channel: {}'.format(
                            denotify_username(cmd_data['user']['name']),
                            MMBL_CHANNELS[cmd_data['user']['channel_id']]['name'],
                    )})
            elif cmd_data['cmd'] == MumbleControlCommand.USER_REMOVE:
                AUDIO_BUFFERS[cmd_data['user']['session']]['buffer'].clear()
                del MMBL_USERS[cmd_data['user']['session']]
                del AUDIO_BUFFERS[cmd_data['user']['session']]
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                    'msg': '{} has disconnected'.format(
                        denotify_username(cmd_data['user']['name']),
                )})
            elif cmd_data['cmd'] == MumbleControlCommand.CHANNEL_CREATE:
                MMBL_CHANNELS[cmd_data['channel']['channel_id']] = cmd_data['channel']
            elif cmd_data['cmd'] == MumbleControlCommand.CHANNEL_UPDATE:
                MMBL_CHANNELS[cmd_data['channel']['channel_id']].update(cmd_data['changes'])
            elif cmd_data['cmd'] == MumbleControlCommand.CHANNEL_REMOVE:
                del MMBL_CHANNELS[cmd_data['channel']['channel_id']]
            elif cmd_data['cmd'] in (MumbleControlCommand.RECV_CHANNEL_TEXT_MSG, MumbleControlCommand.RECV_USER_TEXT_MSG):
                sender = MMBL_USERS[cmd_data['actor']]
                if sender['name'] in router_config['ignore']:
                    log.info('Ignoring text message from user: %s', sender['name'])
                else:
                    if cmd_data['channel_id']:
                        irc_conn.send({
                            'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                            'msg': '<{}> {}'.format(
                                denotify_username(sender['name']),
                                cmd_data['message'],
                        )})
                    if router_config['enable_commands']:
                        if any(cmd_data['message'].startswith(w) for w in router_config['command_params']['activation_words']):
                            try:
                                _, cmd = cmd_data['message'].split(' ', 1)
                            except ValueError: pass
                            else:
                                cmd_engine.dispatch_intent(sender, cmd)
                        elif cmd_data['message'].startswith(cmd_engine.BANG_CHARACTER):
                            cmd_engine.dispatch_bang(sender, cmd_data['message'])
            elif cmd_data['cmd'] in (MumbleControlCommand.RECV_CHANNEL_AUDIO, MumbleControlCommand.RECV_USER_AUDIO):
                sender_data = AUDIO_BUFFERS[cmd_data['session']]
                if cmd_data['name'] not in router_config['ignore']:
                    MIXED_BUFFER.add_chunk(cmd_data['chunk'])
                    if sender_data['last_sample'] is None and len(sender_data['buffer']) == 0:
                        log.debug('Started recieving audio for: %s', cmd_data['name'])
                    elif len(sender_data['buffer']) > 0:
                        # we've already gotten audio for this user
                        last_chunk = sender_data['buffer'][-1]
                        if sender_data['last_sample'] is None:
                            # wait_time has already expired, so cap silence
                            #   length at that
                            dt = router_config['wait_time']
                        else:
                            dt = min(cmd_data['chunk'].time - last_chunk.time, router_config['wait_time'])

                        # check if we need to insert silence
                        if dt > (PYMUMBLE_AUDIO_PER_PACKET * 2):
                            missing_packets = int(dt // PYMUMBLE_AUDIO_PER_PACKET)
                            if missing_packets > 0:
                                log.debug('Inserting silence: pkt_count=%d dt=%1.3fs len=%1.3s',
                                    missing_packets, dt, missing_packets * PYMUMBLE_AUDIO_PER_PACKET)
                                sender_data['buffer'].extend(
                                    silent_chunk(last_chunk, i) for i in range(missing_packets))
                    sender_data['buffer'].append(cmd_data['chunk'])
                    sender_data['last_sample'] = time.time()
            else:
                log.warning('Unrecognized command from mumble: %r', cmd_data)

        if trans_conn in ready:
            cmd_data = trans_conn.recv()
            if cmd_data['cmd'] == TranscriberControlCommand.TRANSCRIBE_MESSAGE_RESPONSE:
                log.debug('Recieved transcription result for: txid=%s', cmd_data['txid'])
                try:
                    sender = MMBL_USERS[cmd_data['actor']]
                except KeyError:
                    log.debug('Sender has disappeared: actor=%d', cmd_data['actor'])
                    sender = {'name': 'ghost:{:d}'.format(cmd_data['actor'])}
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': '<{}> {}'.format(
                        denotify_username(sender['name']),
                        cmd_data['result']['transcript'],
                        ),
                })
                cmd_msg = cmd_data['result']['transcript'].strip()
                if any(cmd_msg.lower().startswith(actword.lower()) \
                        for actword in router_config['command_params']['activation_words']):
                    try:
                        activation_word, voice_cmd = cmd_msg.split(' ', 1)
                    except ValueError:
                        pass
                    else:
                        log.debug('Found possible voice command: @%s %s', activation_word, voice_cmd)
                        if router_config['enable_commands']:
                            try:
                                cmd_engine.dispatch_intent(sender, voice_cmd)
                            except Exception as err:
                                log.error('Exception while processing voice command')
                                log.exception(err)
            else:
                log.warning('Unrecognized command from transcriber: %r', cmd_data)

        if speak_conn in ready:
            cmd_data = speak_conn.recv()
            if cmd_data['cmd'] == SpeakerControlCommand.SPEAK_MESSAGE_RESPONSE:
                log.debug('Got speaker response: txid=%s actor=%d len=%d b',
                    cmd_data['txid'], cmd_data['actor'], len(cmd_data['buffer']))
                cmd_data['cmd'] = MumbleControlCommand.SEND_CHANNEL_AUDIO_MSG
                mmbl_conn.send({
                    'cmd': MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER,
                })
                mmbl_conn.send(cmd_data)
                MIXED_BUFFER.add_buffer(cmd_data['buffer'])
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': cmd_data['msg'],
                })
            else:
                log.warning('Unrecognized command from speaker: %r', cmd_data)

        if media_conn in ready:
            cmd_data = media_conn.recv()
            if cmd_data['cmd'] == MediaControlCommand.AUDIO_CHUNK_READY:
                cmd_data['cmd'] = MumbleControlCommand.SEND_CHANNEL_AUDIO_MSG
                mmbl_conn.send(cmd_data)
            else:
                log.warning('Unrecognized command from media: %r', cmd_data)

        if master_conn in ready:
            cmd_data = master_conn.recv()
            if cmd_data['cmd'] == RouterControlCommand.EXIT:
                log.debug('Recieved EXIT command from master')
                mmbl_conn.send({'cmd': MumbleControlCommand.EXIT})
                irc_conn.send({'cmd': IrcControlCommand.EXIT})
                trans_conn.send({'cmd': TranscriberControlCommand.EXIT})
                speak_conn.send({'cmd': SpeakerControlCommand.EXIT})
                keep_running = False
            else:
                log.warning('Unrecognized command from master: %r', cmd_data)

        for session_id, data in AUDIO_BUFFERS.items():
            user = MMBL_USERS[session_id]
            if data['last_sample'] is not None:
                buf_dur = sum(c.duration for c in data['buffer'])
                if len(data['buffer']) == data['buffer'].maxlen:
                    log.debug('Buffer is full, flushing: %s dur=%1.2fs',
                        user['name'], buf_dur)
                    txid = generate_uuid()
                    audio_buffer = b''.join(c.pcm for c in sorted(data['buffer'], key=lambda c: c.sequence))
                    log.debug('Queueing partial buffer: txid=%s len=%d bytes dur=%1.2fs', txid, len(audio_buffer), buf_dur)
                    trans_conn.send({'cmd': TranscriberControlCommand.TRANSCRIBE_MESSAGE,
                        'actor': user['session'],
                        'username': user['name'],
                        'buffer': audio_buffer,
                        'phrases': [u['name'] for u in MMBL_USERS.values()] \
                            + router_config['command_params']['activation_words'] \
                            + IRC_USERS,
                        'txid': txid,
                    })
                    data['buffer'].clear()

                elif (time.time() - data['last_sample']) > router_config['wait_time']:
                    log.debug('Buffer has expired, flushing: %s dur=%1.2fs',
                        user['name'], buf_dur)
                    if buf_dur < router_config['min_buffer_len']:
                        log.debug('Buffer is too short to transcribe: %1.2fs', buf_dur)
                    else:
                        txid = generate_uuid()
                        audio_buffer = b''.join(c.pcm for c in sorted(data['buffer'], key=lambda c: c.time))
                        log.debug('Queueing buffer: txid=%s len=%d bytes dur=%1.2fs', txid, len(audio_buffer), buf_dur)
                        trans_conn.send({'cmd': TranscriberControlCommand.TRANSCRIBE_MESSAGE,
                            'actor': user['session'],
                            'username': user['name'],
                            'buffer': audio_buffer,
                            'phrases': [u['name'] for u in MMBL_USERS.values()] + router_config['command_params']['activation_words'],
                            'txid': txid,
                        })
                        data['buffer'].clear()
                    data['last_sample'] = None
    log.debug('Router process exiting')

def main(args, **kwargs):
    stop_running = threading.Event()

    def handle_sigint(*args):
        stop_running.set()
    signal.signal(signal.SIGINT, handle_sigint)

    if args.config is None:
        log.error('-c/--config is required')
        raise SystemExit()

    setproctitle.setproctitle('doty: master')

    cfg_fn = os.path.join(os.path.dirname(__file__), BASE_CONFIG_FILENAME)
    log.debug('Loading base config: %s', cfg_fn)
    with open(cfg_fn) as config_handle:
        config = yaml.safe_load(config_handle)
    log.debug('Loading user config: %s', args.config)
    with open(args.config) as config_handle:
        config = deep_merge_dict(config, yaml.safe_load(config_handle))
    schema_fn = os.path.join(os.path.dirname(__file__), SCHEMA_FILENAME)
    log.debug('Loading validation schema: %s', schema_fn)
    with open(schema_fn) as schema_handle:
        schema = yaml.safe_load(schema_handle)

    try:
        jsonschema.validate(config, schema)
    except jsonschema.exceptions.ValidationError as err:
        log.error('Error in config structure: %s', err.message)
        raise SystemExit()
    log.debug('Config validation passed')

    # this is a placeholder as there is no actual media proc config atm
    config['media'] = {}

    mmbl_conn = QueuePipe()
    irc_conn = QueuePipe()
    trans_conn = QueuePipe()
    speak_conn = QueuePipe()
    media_conn = QueuePipe()
    router_conn = QueuePipe()

    running_procs = {}
    if not args.no_mumble:
        running_procs['mumble'] = proc_mmbl(
            config['mumble'], mmbl_conn, pymumble_debug=kwargs['pymumble_debug'])
    if not args.no_irc:
        running_procs['irc'] = proc_irc(config['irc'], irc_conn)
    if not args.no_transcriber:
        running_procs['transcriber'] = proc_transcriber(config['transcriber'], trans_conn)
    if not args.no_speaker:
        running_procs['speaker'] = proc_speaker(config['speaker'], speak_conn)
    running_procs['media'] = proc_media(config['media'], media_conn)
    running_procs['router'] = proc_router(config['router'],
        mmbl_conn, irc_conn, trans_conn, speak_conn, media_conn, router_conn)
    proc_start_times = {k: time.time() for k in running_procs.keys()}
    last_restart = time.time()
    restart_wait = config['main']['restart_wait']

    log.debug('Entering sleep loop')
    while not stop_running.is_set():
        time.sleep(LONG_POLL)
        if (time.time() - last_restart) > (restart_wait * restart_wait):
            if restart_wait != config['main']['restart_wait']:
                restart_wait = config['main']['restart_wait']
                log.debug('Reset restart wait to: %1.1fs', restart_wait)
        for proc_name, proc in running_procs.items():
            if proc.exitcode is not None:
                if proc_start_times[proc_name] is not None:
                    log.warning('Detected process exit: %s exitcode=%s runtime=%1.2fs',
                        proc_name, proc.exitcode, (time.time() - proc_start_times[proc_name]))
                    proc_start_times[proc_name] = None
                if not args.no_autorestart:                
                    if (time.time() - last_restart) > restart_wait:
                        log.info('Restarting proc: %s', proc_name)
                        if proc_name == 'mumble':
                            running_procs[proc_name] = proc_mmbl(
                                config[proc_name], mmbl_conn, pymumble_debug=kwargs['pymumble_debug'])
                        elif proc_name == 'irc':
                            running_procs[proc_name] = proc_irc(config[proc_name], irc_conn)
                        elif proc_name == 'transcriber':
                            running_procs[proc_name] = proc_transcriber(config[proc_name], trans_conn)
                        elif proc_name == 'speaker':
                            running_procs[proc_name] = proc_speaker(config[proc_name], speak_conn)
                        elif proc_name == 'media':
                            running_procs[proc_name] = proc_media(config[proc_name], speak_conn)
                        elif proc_name == 'router':
                            log.error('Not attempting to restart router, will now exit')
                            stop_running.set()
                        else:
                            log.warning('Ignoring unknown proc name: %s', proc_name)
                            continue
                        last_restart = time.time()
                        proc_start_times[proc_name] = last_restart
                        restart_wait = min(
                            restart_wait * config['main']['restart_factor'],
                            config['main']['restart_max'])
                        log.debug('Increased restart wait to: %1.1fs', restart_wait)
                else:
                    log.debug('Running without autorestart, bailing out')
                    stop_running.set()
                    break
    log.debug('Exited sleep loop')
    router_conn.send({'cmd': RouterControlCommand.EXIT})

    for p in running_procs.values():
        p.join(LONG_POLL)
        halt_process(p)

    log.info('Shutdown complete')

LOG_LEVEL = logging.INFO
EXTRA_DEBUG = False
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config',
        help='Path to config file',
        default=None,
    )
    parser.add_argument('-v', '--verbose',
        help='Log more messages',
        action='count', default=0,
    )
    parser.add_argument('-q', '--quiet',
        help='Log fewer messages',
        action='count', default=0,
    )
    for proc_type in ['mumble', 'irc', 'transcriber', 'speaker']:
        parser.add_argument('-{}'.format(proc_type[0].upper()), '--no-{}'.format(proc_type),
            help='Do not start the {} process'.format(proc_type),
            action='store_true', default=False,
        )
    parser.add_argument('-A', '--no-autorestart',
        help='Don\'t automatically restart crashed processes',
        action='store_true', default=False,
    )
    args = parser.parse_args()

    LOG_LEVEL = min(logging.CRITICAL, max(logging.DEBUG,
        logging.INFO + (args.quiet * 10) - (args.verbose * 10)
    ))
    log.setLevel(LOG_LEVEL)
    EXTRA_DEBUG = (args.verbose - args.quiet) > 1
    if EXTRA_DEBUG:
        logging.getLogger().setLevel(LOG_LEVEL)

    main(args, pymumble_debug=EXTRA_DEBUG)
