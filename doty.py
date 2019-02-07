#!/usr/bin/env python3

import collections
import logging
import subprocess
import multiprocessing
import threading
import functools
import os
import os.path
import signal
import tempfile
import time
from enum import (
    Enum,
    auto,
)

from google.cloud import speech
from google.cloud.speech import (
    enums,
    types,
)
import pymumble_py3 as pymumble
import yaml
from pymumble_py3.constants import *
from pymumble_py3.errors import (
    UnknownChannelError,
)
import irc.client


APP_NAME = 'doty'
LOG_LEVEL = logging.INFO

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

log = getWorkerLogger('main')

def normalize_callback_args(callback_type, args):
    if callback_type == PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:
        return ({
            'actor': args[0].actor,
            'channel_id': list(args[0].channel_id),
            'message': args[0].message,
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

class MumbleControlCommand(Enum):
    EXIT = auto()
    SEND_CHANNEL_TEXT_MSG = auto()
    SEND_USER_TEXT_MSG = auto()
    SEND_AUDIO_MSG = auto()
    MOVE_TO_CHANNEL = auto()

class IrcControlCommand(Enum):
    EXIT = auto()
    SEND_CHANNEL_TEXT_MSG = auto()

@multiprocessify
def proc_irc(irc_config, worker_conn):
    log = getWorkerLogger('irc', level=LOG_LEVEL)
    keep_running = True

    log.debug('Starting irc process')

    client = irc.client.Reactor()
    server = client.server()

    def handle_irc_message(conn, event):
        log.debug('Handling event: %s', event)

    client.add_global_handler('pubmsg', handle_irc_message)
    client.add_global_handler('privmsg', handle_irc_message)

    log.info('IRC client running')
    while keep_running:
        if not server.connected:
            server.connect(irc_config['server'], irc_config['port'], irc_config['username'])
            server.join(irc_config['channel'])

        client.process_once(0.5)

        if worker_conn.poll(0.5):
            cmd_data = worker_conn.recv()
            log.debug('Recieved control command: %r', cmd_data)
            if cmd_data['cmd'] == IrcControlCommand.EXIT:
                server.disconnect()
                keep_running = False
            elif cmd_data['cmd'] == IrcControlCommand.SEND_CHANNEL_TEXT_MSG:
                server.privmsg(irc_config['channel'], cmd_data['msg'])
    log.debug('IRC process exiting')

@multiprocessify
def proc_mmbl(mmbl_config, worker_conn):
    log = getWorkerLogger('mmbl', level=LOG_LEVEL)
    keep_running = True

    log.debug('Starting mumble process')
    mmbl = pymumble.Mumble(mmbl_config['server'],
        mmbl_config['username'], password=mmbl_config['password'],
        debug=False,
    )
    mmbl.set_receive_sound(True)

    log.debug('Setting up callbacks')
    def handle_callback(clbk_type, *args):
        args = normalize_callback_args(clbk_type, args)
        if clbk_type != PYMUMBLE_CLBK_SOUNDRECEIVED:
            log.debug('Callback event: %s => %r', clbk_type, args)
        worker_conn.send((clbk_type, args))

    for callback_type in (v for k, v in globals().items() if k.startswith('PYMUMBLE_CLBK_')):
        clbk = denote_callback(callback_type)(handle_callback)
        mmbl.callbacks.add_callback(callback_type, clbk)

    log.debug('Starting mumble connection')
    mmbl.start()
    mmbl.is_ready()
    log.info('Connected to mumble server: %s', mmbl_config['server'])

    log.debug('Entering control loop')
    while keep_running:
        if worker_conn.poll(1):
            cmd_data = worker_conn.recv()
            log.debug('Recieved control command: %r', cmd_data)
            if cmd_data['cmd'] == MumbleControlCommand.EXIT:
                log.info('Recieved exit command')
                keep_running = False
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_CHANNEL_TEXT_MSG:
                log.debug('Sending text message to channel: %s => %s',
                    mmbl.channels[cmd_data['channel_id']]['name'], cmd_data['msg'])
                mmbl.channels[cmd_data['channel_id']].send_text_message(cmd_data['msg'])
            elif cmd_data['cmd'] == MumbleControlCommand.SEND_USER_TEXT_MSG:
                log.debug('Sending text message to user: %s => %s',
                    mmbl.users[cmd_data['session_id']]['name'], cmd_data['msg'])
                mmbl.users[cmd_data['session_id']].send_message(cmd_data['msg'])
            elif cmd_data['cmd'] == MumbleControlCommand.MOVE_TO_CHANNEL:
                log.info('Joining channel: %s', cmd_data['channel_name'])
                try:
                    target_channel = mmbl.channels.find_by_name(cmd_data['channel_name'])
                except UnknownChannelError:
                    log.debug('Channel doesnt exist, attempting to create: %s', cmd_data['channel_name'])
                    mmbl.channels.new_channel(0, cmd_data['channel_name'])
                else:
                    mmbl.users.myself.move_in(target_channel['channel_id'])
    log.debug('Mumble process exiting')

@multiprocessify
def proc_worker(worker_config, mmbl_conn, irc_conn, master_conn):
    log = getWorkerLogger('worker', level=LOG_LEVEL)
    keep_running = True
    log.debug('Worker starting up')

    MMBL_CHANNELS = {}
    MMBL_USERS = {}
    AUDIO_BUFFERS = {}

    speech_client = speech.SpeechClient.from_service_account_json(worker_config['google_cloud_auth'])

    def transcribe(buf):
        speech_content = types.RecognitionAudio(content=buf)
        speech_config = types.RecognitionConfig(
            encoding=enums.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=48000,
            language_code=worker_config['speech_lang'],
            speech_contexts=[types.SpeechContext(phrases=[u['name'] for u in MMBL_USERS.values()] \
                + worker_config['hint_phrases'])],
        )
        response = speech_client.recognize(speech_config, speech_content)
        for result in response.results:
            for alternative in result.alternatives:
                return {
                    'transcript': alternative.transcript,
                    'confidence': alternative.confidence,
                }
        return None                

    log.info('Worker running')
    while keep_running:
        if mmbl_conn.poll(worker_config['wait_time']):
            (event_type, event_args) = mmbl_conn.recv()
            if event_type != PYMUMBLE_CLBK_SOUNDRECEIVED:
                # sound events are way too noisy
                log.debug('Recieved event from mumble: %s => %r', event_type, event_args)

            if event_type == PYMUMBLE_CLBK_USERCREATED:
                user_data = event_args[0]
                MMBL_USERS[user_data['session']] = user_data
                AUDIO_BUFFERS[user_data['session']] = {
                    'buffer': collections.deque(maxlen=(worker_config['buffer_time'] * 1000) // 20),
                    'ping': None
                }
            elif event_type == PYMUMBLE_CLBK_USERUPDATED:
                (user_data, changes) = event_args
                MMBL_USERS[user_data['session']].update(changes)
            elif event_type == PYMUMBLE_CLBK_USERREMOVED:
                (user_data, session_data) = event_args
                del MMBL_USERS[user_data['session']]
                del AUDIO_BUFFERS[user_data['session']]
            elif event_type == PYMUMBLE_CLBK_CHANNELCREATED:
                channel_data = event_args[0]
                MMBL_CHANNELS[channel_data['channel_id']] = channel_data
            elif event_type == PYMUMBLE_CLBK_CHANNELUPDATED:
                (channel_data, changes) = event_args
                MMBL_CHANNELS[channel_data['channel_id']].update(changes)
            elif event_type == PYMUMBLE_CLBK_CHANNELREMOVED:
                channel_data = event_args[0]
                del MMBL_CHANNELS[channel-data['channel_id']]
            elif event_type == PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:
                msg_data = event_args[0]
                sender = MMBL_USERS[msg_data['actor']]

                if msg_data['channel_id']:
                    irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                        'msg': '<{}:text> {}'.format(sender['name'], msg_data['message'])})

                # !moveto command
                if msg_data['message'].startswith('!moveto'):
                    target_name = msg_data['message'].strip().split(' ', 1)[1]
                    mmbl_conn.send({'cmd': MumbleControlCommand.MOVE_TO_CHANNEL, 'channel_name': target_name})
            elif event_type == PYMUMBLE_CLBK_SOUNDRECEIVED:
                (sender, sound_chunk) = event_args
                AUDIO_BUFFERS[sender['session']]['buffer'].append(sound_chunk)
                AUDIO_BUFFERS[sender['session']]['ping'] = time.time()

        for session_id, data in AUDIO_BUFFERS.items():
            user = MMBL_USERS[session_id]
            if data['ping'] is not None and time.time() - data['ping'] > worker_config['wait_time']:
                log.debug('Triggering flush on user: %s', user['name'])
                if (len(data['buffer']) * 20) / 1000 > worker_config['min_speech_length']:
                    audio_buffer = b''.join(c.pcm for c in sorted(data['buffer'], key=lambda c: c.time))
                    log.debug('Transcribing audio buffer of %d bytes', len(audio_buffer))
                    result = transcribe(audio_buffer)
                    if result:
                        log.info('Transcription result: %s ~%1.3f => %s', user['name'], result['confidence'], result['transcript'])
                        # mmbl_conn.send({'cmd': MumbleControlCommand.SEND_CHANNEL_TEXT_MSG,
                        #     'channel_id': user['channel_id'],
                        #     'msg': '{} said: {}'.format(user['name'], result['transcript'],
                        # )})
                        irc_conn.send({'cmd': IrcControlCommand.SEND_CHANNEL_TEXT_MSG,
                            'msg': '<{}:audio> {}'.format(user['name'], result['transcript'])})
                    else:
                        log.warning('Failed to transcribe audio from: %s', user['name'])
                else:
                    log.debug('Flushing buffer that is too short to transcribe')

                data['buffer'].clear()
                data['ping'] = None

        if master_conn.poll(0.1):
            if master_conn.recv():
                keep_running = False
    log.debug('Worker process exiting')

def main(args):
    stop_running = threading.Event()

    def handle_sigint(*args):
        log.debug('Caught ^C, shutting down')
        stop_running.set()
    signal.signal(signal.SIGINT, handle_sigint)

    with open(args.config) as config_handle:
        config = yaml.safe_load(config_handle)

    l, r = multiprocessing.Pipe()
    u, d = multiprocessing.Pipe()
    i, o = multiprocessing.Pipe()

    try:
        mmbl = proc_mmbl(config['mumble'], l)
        mmbl.run()
    except AttributeError: pass
    try:
        irc = proc_irc(config['irc'], u)
        irc.run()
    except AttributeError: pass
    try:
        worker = proc_worker(config['worker'], r, d, o)
        worker.run()
    except AttributeError: pass

    log.debug('Entering sleep loop')
    while not stop_running.is_set():
        time.sleep(1)

    r.send({'cmd': MumbleControlCommand.EXIT})
    d.send({'cmd': IrcControlCommand.EXIT})
    i.send(True)

    mmbl.join()
    irc.join()
    worker.join()

    log.info('Shutdown complete')

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config')
    parser.add_argument('-v', '--verbose',
        help='Log more messages',
        action='count', default=0,
    )
    parser.add_argument('-q', '--quiet',
        help='Log fewer messages',
        action='count', default=0,
    )
    args = parser.parse_args()

    LOG_LEVEL = min(logging.CRITICAL, max(logging.DEBUG,
        logging.INFO + (args.quiet * 10) - (args.verbose * 10)
    ))
    log.setLevel(LOG_LEVEL)
    logging.getLogger().setLevel(LOG_LEVEL)

    main(args)
