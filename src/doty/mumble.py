from collections.abc import (
    Callable,
)
from typing import (
    Any,
    List,
    Optional,
    Dict,
    Tuple,
)
import pymumble_py3.users

from collections import deque
import functools
import html
import queue
import threading
import time
from typing import NamedTuple

from bs4 import BeautifulSoup
import pymumble_py3
from pymumble_py3.constants import (
    PYMUMBLE_AUDIO_PER_PACKET,
    PYMUMBLE_SAMPLERATE,
    PYMUMBLE_CLBK_CONNECTED,
    PYMUMBLE_CLBK_DISCONNECTED,
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
from pymumble_py3.soundqueue import SoundChunk

from .audio_utils import silent_chunk
from .constants import (
    SHORT_POLL,
    LONG_POLL,
)
from .ipc import (
    MessageType,
    Packet,
)
from .mp_utils import (
    BaseWorker,
    multiprocessify,
)
from .utils import (
    extra_debugging_enabled,
    undenotify,
)


def normalize_callback_args(callback_type: str, args: List[Any]) -> Tuple[Dict]:
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

# pymumble doesn't tell you what the event type is inside the callback, because
# i guess they expect you to use different handlers for each one, so we wrap our
# single handler with a decorator that makes the event the first arg passed in to
# the handler
def denote_callback(clbk_type: str, func: Callable) -> Callable:
    @functools.wraps(func)
    def wrapper(*args):
        return func(clbk_type, *args)
    return wrapper

class UserAudioBuffer(NamedTuple):
    buffer: deque[SoundChunk]
    last_sample: Optional[float] = None

class MumbleWorker(BaseWorker):
    PROC_NAME = 'mumble'
    ROUTER_POLL_TIMEOUT = SHORT_POLL
    MUMBLE_EVENT_MAP = {
        PYMUMBLE_CLBK_CONNECTED:            MessageType.MMBL_CONNECTED,
        PYMUMBLE_CLBK_DISCONNECTED:         MessageType.MMBL_DISCONNECTED,
        PYMUMBLE_CLBK_USERCREATED:          MessageType.MMBL_USER_CREATE,
        PYMUMBLE_CLBK_USERUPDATED:          MessageType.MMBL_USER_UPDATE,
        PYMUMBLE_CLBK_USERREMOVED:          MessageType.MMBL_USER_REMOVE,
        PYMUMBLE_CLBK_CHANNELCREATED:       MessageType.MMBL_CHANNEL_CREATE,
        PYMUMBLE_CLBK_CHANNELUPDATED:       MessageType.MMBL_CHANNEL_UPDATE,
        PYMUMBLE_CLBK_CHANNELREMOVED:       MessageType.MMBL_CHANNEL_REMOVE,
        PYMUMBLE_CLBK_TEXTMESSAGERECEIVED:  MessageType.MMBL_RECV_CHANNEL_TEXT,
        PYMUMBLE_CLBK_SOUNDRECEIVED:        MessageType.MMBL_RECV_CHANNEL_AUDIO,
    }

    def _handle_callback(self, ev_type: str, *args: List[Any]) -> None:
        args = normalize_callback_args(ev_type, args)
        if ev_type == PYMUMBLE_CLBK_SOUNDRECEIVED:
            # avoid memory leak in pymumble
            try:
                with self._clbk_lock:
                    self._mmbl.users[args[0]['session']].sound.get_sound()
            except Exception:
                pass
        else:
            self._log.debug('Callback event: %s -> %r', ev_type, args)

        pkt_type = self.MUMBLE_EVENT_MAP.get(ev_type, None)
        pkt_data = {}
        if pkt_type == MessageType.MMBL_RECV_CHANNEL_AUDIO:
            if not args[0]['channel_id']:
                # it's a private message
                pkt_type = MessageType.MMBL_RECV_USER_AUDIO
            pkt_data.update(args[0])
            pkt_data['chunk'] = args[1]
        elif pkt_type in (MessageType.MMBL_USER_CREATE, MessageType.MMBL_USER_UPDATE, MessageType.MMBL_USER_REMOVE):
            pkt_data['user'] = args[0]
            if pkt_type == MessageType.MMBL_USER_UPDATE:
                pkt_data['changes'] = args[1]
            elif pkt_type == MessageType.MMBL_USER_REMOVE:
                pkt_data['session'] = args[1]
        elif pkt_type in (MessageType.MMBL_CHANNEL_CREATE, MessageType.MMBL_CHANNEL_UPDATE, MessageType.MMBL_CHANNEL_REMOVE):
            pkt_data['channel'] = args[0]
            if pkt_type == MessageType.MMBL_CHANNEL_UPDATE:
                pkt_data['changes'] = args[1]
        elif pkt_type == MessageType.MMBL_RECV_CHANNEL_TEXT:
            if not args[0]['channel_id']:
                # it's a private message
                pkt_type = MessageType.MMBL_RECV_USER_TEXT
            pkt_data.update(args[0])
        elif pkt_type == MessageType.MMBL_CONNECTED:
            pkt_data['server'] = self._config['server']
            pkt_data['port'] = self._config['port']

        if pkt_type is not None:
            # SimpleQueue is supposedly threadsafe/reentrant safe so theoretically
            # we don't need a lock
            # with self._clbk_lock:
            self._clbk_queue.put((pkt_type, pkt_data))
        else:
            self._log.warning('Unrecognized callback event: %s', ev_type)

    def _on_pre_loop(self) -> None:
        super()._on_pre_loop()

        self._clbk_lock = threading.Lock()
        self._clbk_queue = queue.SimpleQueue()

        self._mmbl = pymumble_py3.Mumble(
            self._config['server'],
            self._config['username'],
            password=self._config['password'],
            port=self._config['port'],
            debug=extra_debugging_enabled(),
            reconnect=False,
        )
        self._mmbl.set_receive_sound(True)

        self._audio_buffers: Dict[int, UserAudioBuffer] = {}

        # register callbacks
        for callback_type in self._mmbl.callbacks.get_callbacks_list():
            clbk = denote_callback(callback_type, self._handle_callback)
            self._mmbl.callbacks.add_callback(callback_type, clbk)

        self._log.debug('Connecting to mumble server: %s:%d',
                        self._config['server'], self._config['port'])
        self._mmbl.start()
        self._mmbl.is_ready()

    def _fill_audio_buffers(self, packet: Packet) -> None:
        if packet.type is MessageType.MMBL_RECV_CHANNEL_AUDIO:
            sender = self._audio_buffers[packet.data['session']]
            if sender.last_sample is None and len(sender.buffer) == 0:
                self._log.debug('Started buffering audio for: %s', packet.data['name'])
            elif len(sender.buffer) > 0:
                # we've already gotten audio for this user
                last_chunk = sender.buffer[-1]
                if sender.last_sample is None:
                    # wait_time has already expired, so cap silence
                    #   length at that
                    dt = self._config['buffer_wait_time']
                else:
                    dt = min(packet.data['chunk'].time - last_chunk.time, self._config['buffer_wait_time'])

                # check if we need to insert silence
                if dt > (PYMUMBLE_AUDIO_PER_PACKET * 2):
                    missing_packets = int(dt / PYMUMBLE_AUDIO_PER_PACKET)
                    if missing_packets > 0:
                        self._log.debug('Inserting silence: pkt_count=%d dt=%1.3fs len=%1.3s',
                            missing_packets, dt, missing_packets * PYMUMBLE_AUDIO_PER_PACKET)
                        sender.buffer.extend(
                            silent_chunk(last_chunk, i, missing_packets) for i in range(missing_packets))
            sender.buffer.append(packet.data['chunk'])
            sender.last_sample = time.monotonic()

        elif packet.type is MessageType.MMBL_USER_CREATE:
            self._audio_buffers[packet.data['user']['session']] = UserAudioBuffer(deque())
        elif packet.type is MessageType.MMBL_USER_REMOVE:
            self._audio_buffers[packet.data['session']].clear()
            del self._audio_buffers[packet.data['session']]

    def _get_mmbl_usernames(self) -> List[str]:
        with self._clbk_lock:
            return [u['name'] for u in self._mmbl.users.values()]

    def _drain_audio_buffers(self) -> None:
        for session_id, sender in self._audio_buffers.items():
            with self._clbk_lock:
                try:
                    user: pymumble_py3.users.User = self._mmbl.users[session_id]
                except KeyError:
                    # because the queued events are processed before the audio
                    # buffers, it's possible for us to be processing a buffer of
                    # a user that no longer exists, but that we haven't recieved
                    # the disconnect event for yet
                    user = {
                        'session': session_id,
                        'username': 'ghost-{:02d}'.format(session_id),
                    }
            if sender.last_sample is not None:
                buf_dur = sum(c.duration for c in sender.buffer)
                if len(sender.buffer) * PYMUMBLE_AUDIO_PER_PACKET > self._config['max_buffer_time']:
                    self._log.debug('Buffer is full, flushing: %s dur=%1.2fs',
                              user['name'], buf_dur)
                    audio_buffer = b''.join(c.pcm for c in sorted(
                        sender.buffer, key=lambda c: c.sequence))
                    packet = Packet.new(MessageType.MMBL_RECV_CHANNEL_AUDIO_BUFFERED, {
                        'actor': user['session'],
                        'username': user['name'],
                        'buffer': audio_buffer,
                        'phrases': self._get_mmbl_usernames(),
                    })
                    self._log.debug('Queueing partial buffer: txid=%s len=%d bytes dur=%1.2fs',
                        packet.metadata['txid'], len(audio_buffer), buf_dur)
                    self._conn.send(packet.serialize())
                    sender.buffer.clear()
                elif (time.monotonic() - sender.last_sample) > self._config['buffer_wait_time']:
                    self._log.debug('Buffer has expired, flushing: %s dur=%1.2fs',
                              user['name'], buf_dur)
                    if buf_dur < self._config['min_buffer_time']:
                        self._log.debug(
                            'Buffer is too short to transcribe: %1.2fs', buf_dur)
                    else:
                        audio_buffer = b''.join(c.pcm for c in sorted(
                            sender.buffer, key=lambda c: c.time))
                        packet = Packet.new(MessageType.MMBL_RECV_CHANNEL_AUDIO_BUFFERED, {
                            'actor': user['session'],
                            'username': user['name'],
                            'buffer': audio_buffer,
                            'phrases': self._get_mmbl_usernames(),
                        })
                        self._log.debug('Queueing buffer: txid=%s len=%d bytes dur=%1.2fs',
                            packet.metadata['txid'], len(audio_buffer), buf_dur)
                        self._conn.send(packet.serialize())
                        sender.buffer.clear()
                    sender.last_sample = None

    def _on_loop(self) -> None:
        super()._on_loop()

        while True:
            try:
                pkt_type, pkt_data = self._clbk_queue.get_nowait()
            except queue.Empty: break
            packet = Packet.new(pkt_type, pkt_data)
            self._fill_audio_buffers(packet)
            self._conn.send(packet.serialize())
        self._drain_audio_buffers()

        if not self._mmbl.is_alive():
            self._log.warning('Connection has died')
            pkt = Packet.new(MessageType.MMBL_DISCONNECTED).serialize()
            with self._clbk_lock:
                self._conn.send(pkt)
            self._keep_running = False

    def _on_recv_packet(self, packet) -> None:
        super()._on_recv_packet(packet)

        if packet.type == MessageType.MMBL_SEND_CHANNEL_AUDIO_APPEND:
            # this is real noisy, only uncomment if you really need it
            # self._log.debug('Sending audio message: %d bytes', len(packet.data['buffer']))
            self._mmbl.sound_output.add_sound(packet.data['buffer'])
        elif packet.type == MessageType.MMBL_SEND_CHANNEL_AUDIO_MIX:
            pass
        elif packet.type == MessageType.MMBL_SEND_CHANNEL_TEXT:
            if 'channel_id' not in packet.data:
                packet.data['channel_id'] = self._mmbl.users.myself['channel_id']
            self._log.info('Sending text message to channel: %s <- "%s"',
                           self._mmbl.channels[packet.data['channel_id']]['name'], undenotify(packet.data['msg']))
            self._mmbl.channels[packet.data['channel_id']].send_text_message(
                html.escape(packet.data['msg']))
        elif packet.type == MessageType.MMBL_SEND_USER_TEXT:
            self._log.info('Sending text message to user: %s <- "%s"',
                           self._mmbl.users[packet.data['session_id']]['name'], undenotify(packet.data['msg']))
            self._mmbl.users[packet.data['session_id']].send_message(
                html.escape(packet.data['msg']))
        elif packet.type == MessageType.MMBL_MOVE_TO_CHANNEL:
            self._log.info('Joining channel: %s', packet.data['channel_name'])
            try:
                target_channel = self._mmbl.channels.find_by_name(
                    packet.data['channel_name'])
            except UnknownChannelError:
                self._log.debug(
                    'Channel doesnt exist, attempting to create: %s', packet.data['channel_name'])
                self._mmbl.channels.new_channel(0, packet.data['channel_name'])
            else:
                self._mmbl.users.myself.move_in(target_channel['channel_id'])
        elif packet.type == MessageType.MMBL_DROP_CHANNEL_AUDIO_BUFFER:
            self._log.debug('Dropping outgoing audio buffer')
            self._mmbl.sound_output.clear_buffer()

MumbleWorkerProcess = multiprocessify(MumbleWorker)
