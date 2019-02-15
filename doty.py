#!/usr/bin/env python3

import collections
import logging
import subprocess
import multiprocessing
import threading
import functools
import hashlib
import html
import os
import os.path
import queue
import signal
import ssl
import tempfile
import time
import uuid
from enum import (
    Enum,
    auto,
)

import yaml
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
from google.cloud import speech as gcloud_speech
from google.cloud import texttospeech as gcloud_texttospeech
import irc.client
import irc.connection
from bs4 import BeautifulSoup
import contexttimer


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
SOUNDCHUNK_SIZE = 1920

signal.signal(signal.SIGINT, signal.SIG_IGN)
logging.basicConfig(level=LOG_LEVEL)

def multiprocessify(func):
    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        proc = multiprocessing.Process(target=func, args=pargs, kwargs=kwargs)
        proc.start()
        return proc
    return wrapper

def getWorkerLogger(worker_name, level=logging.DEBUG):
    log = logging.getLogger('{}.{}-{:05d}'.format(APP_NAME, worker_name, os.getpid()))
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

generate_uuid = lambda: str(uuid.uuid1())
sha1sum = lambda data: hashlib.sha1(data if type(data) is bytes else data.encode('utf-8')).hexdigest()

def denotify_username(username):
    if len(username) > 1:
        return ZW_SPACE.join([username[0], username[1:-1], username[-1]]).replace(ZW_SPACE * 2, ZW_SPACE)
    else:
        return username

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

class MumbleControlCommand(Enum):
    EXIT = auto()
    SEND_CHANNEL_TEXT_MSG = auto()
    SEND_USER_TEXT_MSG = auto()
    SEND_AUDIO_MSG = auto()
    MOVE_TO_CHANNEL = auto()

class IrcControlCommand(Enum):
    EXIT = auto()
    SEND_CHANNEL_TEXT_MSG = auto()
    SEND_CHANNEL_ACTION = auto()
    RECV_CHANNEL_TEXT_MSG = auto()

class DialogflowControlCommand(Enum):
    EXIT = auto()
    DETECT_INTENT_TEXT = auto()
    DETECT_INTENT_TEXT_RESPONSE = auto()
    DETECT_INTENT_AUDIO = auto()
    DETECT_INTENT_AUDIO_RESPONSE = auto()

class TranscriberControlCommand(Enum):
    EXIT = auto()
    TRANSCRIBE_MESSAGE = auto()
    TRANSCRIBE_MESSAGE_RESPONSE = auto()

class SpeakerControlCommand(Enum):
    EXIT = auto()
    SPEAK_MESSAGE = auto()
    SPEAK_MESSAGE_RESPONSE = auto()

class RouterControlCommand(Enum):
    EXIT = auto()

@multiprocessify
def proc_irc(irc_config, router_conn):
    log = getWorkerLogger('irc', level=LOG_LEVEL)
    keep_running = True

    log.debug('Starting irc process')

    client = irc.client.Reactor()
    server = client.server()
    if irc_config['ssl']:
        ssl_ctx = ssl.create_default_context()
        if not irc_config['ssl_verify']:
            ssl_ctx.verify_mode = ssl.CERT_NONE
        conn_factory = irc.connection.Factory(wrapper=lambda s:
            ssl_ctx.wrap_socket(s, server_hostname=irc_config['server']))
    else:
        conn_factory = irc.connection.Factory()

    e2d = lambda ev: {'type': ev.type, 'source': ev.source, 'target': ev.target, 'arguments': ev.arguments, 'tags': ev.tags}
    def handle_irc_message(conn, event):
        log.debug('Recieved IRC event: %s', event)
        if event.type == 'pubmsg':
            cmd_data = e2d(event)
            cmd_data['cmd'] = IrcControlCommand.RECV_CHANNEL_TEXT_MSG
            router_conn.send(cmd_data)

    client.add_global_handler('pubmsg', handle_irc_message)
    client.add_global_handler('privmsg', handle_irc_message)
    # client.add_global_handler('action', handle_irc_message)

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
                log.info('Sending message: %s', cmd_data['msg'])
                server.privmsg(irc_config['channel'], cmd_data['msg'])
            elif cmd_data['cmd'] == IrcControlCommand.SEND_CHANNEL_ACTION:
                log.info('Sending action: %s', cmd_data['msg'])
                server.action(irc_config['channel'], cmd_data['msg'])
            else:
                log.warning('Unrecognized command: %r', cmd_data)
    log.debug('IRC process exiting')

@multiprocessify
def proc_mmbl(mmbl_config, router_conn, pymumble_debug=False):
    log = getWorkerLogger('mmbl', level=LOG_LEVEL)
    keep_running = True
    clbk_lock = threading.Lock()

    log.debug('Starting mumble process')
    mmbl = pymumble.Mumble(mmbl_config['server'],
        mmbl_config['username'], password=mmbl_config['password'],
        port=mmbl_config['port'],
        debug=pymumble_debug,
        reconnect=False,
    )
    mmbl.set_receive_sound(True)

    log.debug('Setting up callbacks')
    def handle_callback(clbk_type, *args):
        args = normalize_callback_args(clbk_type, args)
        if clbk_type == PYMUMBLE_CLBK_SOUNDRECEIVED:
            # avoid memory leak in pymumble
            try:
                mmbl.users[args[0]['session']].sound.get_sound()
            except Exception: pass
        else:
            log.debug('Callback event: %s => %r', clbk_type, args)
        with clbk_lock:
            router_conn.send((clbk_type, args))

    for callback_type in mmbl.callbacks.get_callbacks_list():
        clbk = denote_callback(callback_type)(handle_callback)
        mmbl.callbacks.add_callback(callback_type, clbk)

    log.debug('Starting mumble connection')
    mmbl.start()
    mmbl.is_ready()
    log.info('Connected to mumble server: %s', mmbl_config['server'])

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
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_AUDIO_MSG:
                log.info('Sending audio message: %d bytes', len(cmd_data['buffer']))
                mmbl.sound_output.add_sound(cmd_data['buffer'])
            else:
                log.warning('Unrecognized command: %r', cmd_data)
        if not mmbl.is_alive():
            log.error('Mumble connection has died')
            keep_running = False
    log.debug('Mumble process exiting')

@multiprocessify
def proc_transcriber(transcription_config, router_conn):
    log = getWorkerLogger('transcriber', level=LOG_LEVEL)
    keep_running = True
    log.debug('Transcribing starting up')

    speech_client = gcloud_speech.SpeechClient.from_service_account_json(transcription_config['google_cloud_auth'])

    @contexttimer.timer(logger=log, level=logging.DEBUG)
    def transcribe(buf, phrases=[]):
        speech_content = gcloud_speech.types.RecognitionAudio(content=buf)
        speech_config = gcloud_speech.types.RecognitionConfig(
            encoding=gcloud_speech.enums.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=PYMUMBLE_SAMPLERATE,
            language_code=transcription_config['language'],
            speech_contexts=[gcloud_speech.types.SpeechContext(phrases=phrases \
                + transcription_config['hint_phrases'])],
        )
        response = speech_client.recognize(speech_config, speech_content)
        for result in response.results:
            for alternative in result.alternatives:
                return {
                    'transcript': alternative.transcript,
                    'confidence': alternative.confidence,
                }
        return None

    log.info('Transcriber running')
    while keep_running:
        if router_conn.poll(LONG_POLL):
            cmd_data = router_conn.recv()
            if cmd_data['cmd'] == TranscriberControlCommand.EXIT:
                log.debug('Recieved EXIT command from router')
                keep_running = False
            elif cmd_data['cmd'] == TranscriberControlCommand.TRANSCRIBE_MESSAGE:
                result = transcribe(cmd_data['buffer'], cmd_data['phrases'])
                if result:
                    log.debug('Transcription result: txid=%s actor=%d result=%r',
                        cmd_data['txid'], cmd_data['actor'], result)
                    router_conn.send({
                        'cmd': TranscriberControlCommand.TRANSCRIBE_MESSAGE_RESPONSE,
                        'actor': cmd_data['actor'],
                        'result': result,
                        'txid': cmd_data['txid'],
                    })
                else:
                    log.debug('No transcription result for: txid=%s, actor=%d',
                        cmd_data['txid'], cmd_data['actor'])
            else:
                log.warning('Unrecognized command: %r', cmd_data)
    log.debug('Transcriber process exiting')

@multiprocessify
def proc_speaker(speaker_config, router_conn):
    log = getWorkerLogger('speaker', level=LOG_LEVEL)
    keep_running = True
    log.debug('Speaker starting up')

    tts_client = gcloud_texttospeech.TextToSpeechClient.from_service_account_json(speaker_config['google_cloud_auth'])
    tts_voice = gcloud_texttospeech.types.VoiceSelectionParams(
        language_code=speaker_config['language'],
        name=speaker_config['voice'],

    )
    tts_config = gcloud_texttospeech.types.AudioConfig(
        audio_encoding=gcloud_texttospeech.enums.AudioEncoding.LINEAR16,
        sample_rate_hertz=PYMUMBLE_SAMPLERATE,
        effects_profile_id=speaker_config['effect_profiles'],
    )

    @contexttimer.timer(logger=log, level=logging.DEBUG)
    def speak(text):
        text_input = gcloud_texttospeech.types.SynthesisInput(text=text)
        response = tts_client.synthesize_speech(text_input, tts_voice, tts_config)
        return response.audio_content[WAV_HEADER_LEN:]

    log.info('Speaker running')
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
                    audio = speak(cmd_data['msg'].replace(ZW_SPACE, ''))
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
def proc_router(router_config, mmbl_conn, irc_conn, trans_conn, speak_conn, master_conn):
    log = getWorkerLogger('router', level=LOG_LEVEL)
    keep_running = True
    log.debug('Router starting up')

    MMBL_CHANNELS = {}
    MMBL_USERS = {}
    AUDIO_BUFFERS = {}

    mmbl_conn._swap()
    irc_conn._swap()
    trans_conn._swap()
    speak_conn._swap()

    def say(msg, source_id=0):
        return speak_conn.send({
            'cmd': SpeakerControlCommand.SPEAK_MESSAGE,
            'msg': msg,
            'actor': source_id,
            'txid': generate_uuid(),
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

    if router_config['startup_message']:
        say(router_config['startup_message'])

    log.info('Router running')
    while keep_running:
        ready = QueuePipe.wait(mmbl_conn, irc_conn, trans_conn, speak_conn, master_conn, timeout=SHORT_POLL)
        if irc_conn in ready:
            cmd_data = irc_conn.recv()
            if cmd_data['cmd'] == IrcControlCommand.RECV_CHANNEL_TEXT_MSG:
                clean_nick = cmd_data['source'].split('!')[0]
                if clean_nick not in router_config['ignore']:
                    for msg in cmd_data['arguments']:
                        # !moveto command
                        if msg.startswith('!moveto'):
                            target_name = msg.split(' ', 1)[1].strip()
                            mmbl_conn.send({
                                'cmd': MumbleControlCommand.MOVE_TO_CHANNEL,
                                'channel_name': target_name,
                            })
                        # !say command
                        elif msg.startswith('!say'):
                            say_msg = '{} said: {}'.format(
                                denotify_username(clean_nick),
                                msg.split(' ', 1)[1].strip(),
                            )
                            say(say_msg)
                        else:
                            mmbl_conn.send({
                                'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                                'msg': '<{}> {}'.format(clean_nick, msg),
                            })
            else:
                log.warning('Recieved unknown command from IRC: %r', cmd_data)

        if mmbl_conn in ready:
            (event_type, event_args) = mmbl_conn.recv()
            if event_type != PYMUMBLE_CLBK_SOUNDRECEIVED:
                # sound events are way too noisy
                log.debug('Recieved event from mumble: %s => %r', event_type, event_args)

            if event_type == PYMUMBLE_CLBK_USERCREATED:
                user_data = event_args[0]
                MMBL_USERS[user_data['session']] = user_data
                AUDIO_BUFFERS[user_data['session']] = {
                    'buffer': collections.deque(
                        maxlen=int(MAX_TRANSCRIPTION_TIME / PYMUMBLE_AUDIO_PER_PACKET) - 1),
                    'last_sample': None,
                }
                irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                    'msg': '{} has connected in channel: {}'.format(
                        denotify_username(user_data['name']),
                        MMBL_CHANNELS[user_data['channel_id']]['name'],
                        )})
            elif event_type == PYMUMBLE_CLBK_USERUPDATED:
                (user_data, changes) = event_args
                MMBL_USERS[user_data['session']].update(changes)
                if 'channel_id' in changes:
                    irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                        'msg': '{} has joined channel: {}'.format(
                            denotify_username(user_data['name']),
                            MMBL_CHANNELS[user_data['channel_id']]['name'],
                            )})
            elif event_type == PYMUMBLE_CLBK_USERREMOVED:
                (user_data, session_data) = event_args
                AUDIO_BUFFERS[user_data['session']]['buffer'].clear()
                del MMBL_USERS[user_data['session']]
                del AUDIO_BUFFERS[user_data['session']]
                irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_ACTION,
                    'msg': '{} has disconnected'.format(
                        denotify_username(user_data['name']),
                        )})
            elif event_type == PYMUMBLE_CLBK_CHANNELCREATED:
                channel_data = event_args[0]
                MMBL_CHANNELS[channel_data['channel_id']] = channel_data
            elif event_type == PYMUMBLE_CLBK_CHANNELUPDATED:
                (channel_data, changes) = event_args
                MMBL_CHANNELS[channel_data['channel_id']].update(changes)
            elif event_type == PYMUMBLE_CLBK_CHANNELREMOVED:
                channel_data = event_args[0]
                del MMBL_CHANNELS[channel_data['channel_id']]
            elif event_type == PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:
                msg_data = event_args[0]
                sender = MMBL_USERS[msg_data['actor']]
                if sender['name'] in router_config['ignore']:
                    log.info('Ignoring text message from user: %s', sender['name'])
                else:
                    if msg_data['channel_id']:
                        irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                            'msg': '<{}> {}'.format(
                                denotify_username(sender['name']),
                                msg_data['message'])})

                    # !moveto command
                    if msg_data['message'].startswith('!moveto'):
                        target_name = msg_data['message'].split(' ', 1)[1].strip()
                        mmbl_conn.send({'cmd': MumbleControlCommand.MOVE_TO_CHANNEL, 'channel_name': target_name})
                    # !say command
                    if msg_data['message'].startswith('!say'):
                        say_msg = msg_data['message'].split(' ', 1)[1].strip()
                        say(say_msg, source_id=msg_data['actor'])
            elif event_type == PYMUMBLE_CLBK_SOUNDRECEIVED:
                (sender, sound_chunk) = event_args
                sender_data = AUDIO_BUFFERS[sender['session']]
                if sender['name'] not in router_config['ignore']:
                    if sender_data['last_sample'] is None and len(sender_data['buffer']) == 0:
                        log.debug('Started recieving audio for: %s', sender['name'])
                    elif len(sender_data['buffer']) > 0:
                        # we've already gotten audio for this user
                        last_chunk = sender_data['buffer'][-1]
                        if sender_data['last_sample'] is None:
                            # wait_time has already expired, so cap silence
                            #   length at that
                            dt = router_config['wait_time']
                        else:
                            dt = min(sound_chunk.time - last_chunk.time, dt)

                        # check if we need to insert silence
                        if dt > (PYMUMBLE_AUDIO_PER_PACKET * 2):
                            missing_packets = int(dt // PYMUMBLE_AUDIO_PER_PACKET)
                            if missing_packets > 0:
                                log.debug('Inserting silence: pkt_count=%d dt=%1.3fs len=%1.3s',
                                    missing_packets, dt, missing_packets * PYMUMBLE_AUDIO_PER_PACKET)
                                sender_data['buffer'].extend(
                                    silent_chunk(last_chunk, i) for i in range(missing_packets))
                    sender_data['buffer'].append(sound_chunk)
                    sender_data['last_sample'] = time.time()
            elif event_type == PYMUMBLE_CLBK_CONNECTED:
                pass
            else:
                log.warning('Unrecognized mumble event type: %s => %r', event_type, event_args)

        if trans_conn in ready:
            cmd_data = trans_conn.recv()
            if cmd_data['cmd'] == TranscriberControlCommand.TRANSCRIBE_MESSAGE_RESPONSE:
                log.debug('Recieved transcription result for: txid=%s', cmd_data['txid'])
                try:
                    sender = MMBL_USERS[cmd_data['actor']]
                except KeyError:
                    log.warning('Sender has disappeared: actor=%d', cmd_data['actor'])
                    sender = {'name': 'ghost:{:d}'.format(cmd_data['actor'])}
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': '<{}> {}'.format(
                        denotify_username(sender['name']),
                        cmd_data['result']['transcript'],
                        ),
                })
                cmd_msg = cmd_data['result']['transcript'].strip()
                if cmd_msg.lower().startswith(router_config['activation_word'].lower()):
                    log.debug('Found possible voice command: %s', cmd_msg)
            else:
                log.warning('Unrecognized command from transcriber: %r', cmd_data)

        if speak_conn in ready:
            cmd_data = speak_conn.recv()
            if cmd_data['cmd'] == SpeakerControlCommand.SPEAK_MESSAGE_RESPONSE:
                log.debug('Got speaker response: txid=%s actor=%d len=%d b',
                    cmd_data['txid'], cmd_data['actor'], len(cmd_data['buffer']))
                cmd_data['cmd'] = MumbleControlCommand.SEND_AUDIO_MSG
                mmbl_conn.send(cmd_data)
                irc_conn.send({
                    'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                    'msg': cmd_data['msg'],
                })
            else:
                log.warning('Unrecognized command from speaker: %r', cmd_data)

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
                        'buffer': audio_buffer,
                        'phrases': [u['name'] for u in MMBL_USERS.values()] + [router_config['activation_word']],
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
                            'buffer': audio_buffer,
                            'phrases': [u['name'] for u in MMBL_USERS.values()] + [router_config['activation_word']],
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

    cfg_fn = os.path.join(os.path.dirname(__file__), BASE_CONFIG_FILENAME)
    log.debug('Loading base config: %s', cfg_fn)
    with open(cfg_fn) as config_handle:
        config = yaml.safe_load(config_handle)
    log.debug('Loading user config: %s', args.config)
    with open(args.config) as config_handle:
        config = deep_merge_dict(config, yaml.safe_load(config_handle))

    mmbl_conn = QueuePipe()
    irc_conn = QueuePipe()
    trans_conn = QueuePipe()
    speak_conn = QueuePipe()
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
    running_procs['router'] = proc_router(config['router'],
        mmbl_conn, irc_conn, trans_conn, speak_conn, router_conn)
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
        if p.exitcode is None:
            try:
                # only available in 3.7+
                p.kill()
            except AttributeError:
                p.terminate()

    log.info('Shutdown complete')

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
