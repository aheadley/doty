from __future__ import annotations
from collections.abc import Iterable
from typing import (
    Dict,
    List,
)

import enum
import multiprocessing
import threading
import time

import setproctitle

from .constants import (
    APP_NAME,
    SHORT_POLL,
    LONG_POLL,
)
from .ipc import (
    MessageType,
    Packet,
    RouterRule,
    PacketRouter,
    MatchAlways,
)
from .irc import IRCWorkerProcess
from .mp_utils import (
    get_worker_logger,
    halt_process,
)
from .mumble import MumbleWorkerProcess
from .plugin_host import PluginHostWorkerProcess
from .speaker import SpeakerWorkerProcess
from .transcriber import TranscriberWorkerProcess

@enum.unique
class WorkerType(enum.Enum):
    IRC = IRCWorkerProcess.PROC_NAME
    MUMBLE = MumbleWorkerProcess.PROC_NAME
    PLUGIN_HOST = PluginHostWorkerProcess.PROC_NAME
    SPEAKER = SpeakerWorkerProcess.PROC_NAME
    TRANSCRIBER = TranscriberWorkerProcess.PROC_NAME

class WorkerManager:
    WORKER_MAP = {
        WorkerType.IRC: IRCWorkerProcess,
        WorkerType.MUMBLE: MumbleWorkerProcess,
        WorkerType.PLUGIN_HOST: PluginHostWorkerProcess,
        WorkerType.SPEAKER: SpeakerWorkerProcess,
        WorkerType.TRANSCRIBER: TranscriberWorkerProcess,
    }

    def __init__(self, full_config: Dict, run_workers: Iterable = WorkerType):
        self._full_config = full_config
        self._workers_to_run = run_workers
        self._processes = {}
        self._connections = {}
        self._last_proc_restart = time.monotonic()
        self._log = get_worker_logger('worker-manager')

    def __getitem__(self, key: WorkerType) -> multiprocessing.connection.Connection:
        return self._connections[key]

    @property
    def connections(self) -> List[multiprocessing.connection.Connection]:
        return self._connections.values()

    @property
    def processes(self) -> List[multiprocessing.Process]:
        return self._processes.values()

    @property
    def _config(self) -> Dict:
        return self._full_config['main']

    @property
    def _restart_factor(self) -> float:
        return self._config['restart_factor']

    @property
    def _restart_max(self) -> float:
        return self._config['restart_max']

    def set_config(self, full_config):
        self._full_config = full_config
        self._restart_wait = self._config['restart_wait']

    def was_started(self, worker_type: WorkerType) -> bool:
        return worker_type in self._connections \
            and worker_type in self._processes

    def is_running(self, worker_type: WorkerType) -> bool:
        return self.was_started(worker_type) \
            and self._processes[worker_type].exitcode is None

    def start(self, worker_type: WorkerType, config: Dict) -> None:
        if not self.was_started(worker_type):
            self._log.info('Starting worker: %s', worker_type.value)
            left, right = multiprocessing.Pipe(duplex=True)
            self._connections[worker_type] = right
            proc = self.WORKER_MAP[worker_type](left, config)
            proc.start_time = time.monotonic()
            proc.stop_time = None
            self._processes[worker_type] = proc

            self._last_proc_restart = proc.start_time

    def start_all(self) -> None:
        for t in self._workers_to_run:
            self.start(t, self._full_config[t.value])

    def stop(self, worker_type: WorkerType) -> None:
        if self.was_started(worker_type):
            self._log.info('Stopping worker: %s', worker_type.value)
            self._connections[worker_type].send(
                Packet.new(MessageType.PROC_EXIT).serialize())
            self._connections[worker_type].close()
            self._processes[worker_type].join(LONG_POLL)
            halt_process(self._processes[worker_type])
            del self._connections[worker_type]
            del self._processes[worker_type]

    def stop_all(self) -> None:
        for t in self._workers_to_run:
            self.stop(t)

    def check_and_restart(self):
        if (time.monotonic() - self._last_proc_restart) > (self._restart_wait * self._restart_wait):
            if self._restart_wait != self._config['restart_wait']:
                self._restart_wait = self._config['restart_wait']
                self._log.debug(
                    'Reset process restart wait time to: %1.1fs', self._restart_wait)

        for t in self._workers_to_run:
            if self.was_started(t) and not self.is_running(t):
                proc = self._processes[t]
                if proc.stop_time is None:
                    proc.stop_time = time.monotonic()
                    self._log.warning('Process exit: type=%s exitcode=%d runtime=%1.2fs',
                                      t.value, proc.exitcode,
                                      (proc.stop_time - proc.start_time))

                if (time.monotonic() - self._last_proc_restart) > self._restart_wait:
                    self._restart_wait = min(
                        self._restart_wait * self._restart_factor,
                        self._restart_max)
                    self._log.debug(
                        'Increased restart_wait to %1.1fs', self._restart_wait)
                    self.stop(t)
                    self.start(t, self._full_config[t.value])

    def wait(self, timeout=None) -> List[multiprocessing.connection.Connection]:
        ready = multiprocessing.connection.wait(
            self.connections, timeout=timeout)
        for c in ready:
            yield c

class RouterWorker:
    PROC_NAME: str = 'router'

    ROUTER_RULES = [
        RouterRule('mmbl output',
            destination=WorkerType.MUMBLE,
            params=[
                'MMBL_SEND_*',
                'MMBL_MOVE_TO_CHANNEL',
                'MMBL_DROP_*_AUDIO_BUFFER',
            ],
        ),
        RouterRule('irc output',
            destination=WorkerType.IRC,
            params=['IRC_SEND_*'],
        ),
        RouterRule('transcribe buffered mmbl audio',
            destination=WorkerType.TRANSCRIBER,
            params=['MMBL_RECV_*_AUDIO_BUFFERED'],
            stop_on_match=False,
        ),
        RouterRule('transcriber request',
            destination=WorkerType.TRANSCRIBER,
            params=['TRANSCRIBE_MESSAGE_REQUEST'],
        ),
        RouterRule('synthesizer request',
            destination=WorkerType.SPEAKER,
            params=['SYNTHESIZE_MESSAGE_REQUEST'],
        ),
        RouterRule('plugin host fallback',
            destination=WorkerType.PLUGIN_HOST,
            type=MatchAlways,
        ),
    ]

    def __init__(self, proc_manager: WorkerManager, config: Dict):
        self.stop_running = threading.Event()
        self._config = config
        self._manager = proc_manager
        self._router = PacketRouter(self.ROUTER_RULES)
        self._log = get_worker_logger(self.PROC_NAME)

    def _on_pre_loop(self) -> None:
        setproctitle.setproctitle('{}: {}'.format(APP_NAME, self.PROC_NAME))
        self._log.debug('%s worker starting', self.PROC_NAME)

        self._manager.start_all()

    def _on_loop(self) -> None:
        if not self.stop_running.is_set():
            self._manager.check_and_restart()

    def _on_recv_packet(self, conn: multiprocessing.connection.Connection, packet: Packet) -> None:
        if packet.type is MessageType.PROC_EXIT:
            self._log.debug('Recieved PROC_EXIT packet, exiting...')
            for c in self._manager.connections:
                c.send(packet.serialize())
            self.stop_running.set()
        else:
            self._dispatch_packet(packet)

    def _dispatch_packet(self, packet: Packet):
        for dest in self._router.route(packet):
            try:
                self._manager[dest].send(packet.serialize())
            except KeyError:
                continue

    def _on_post_loop(self) -> None:
        self._log.debug('%s worker stopping', self.PROC_NAME)
        self._manager.stop()

    def run_once(self) -> None:
        ready_conns = self._manager.wait(SHORT_POLL)
        for conn in ready_conns:
            has_data_in_conn = True
            while has_data_in_conn:
                packet = Packet.parse(conn.recv())
                self._on_recv_packet(conn, packet)
                has_data_in_conn = conn.poll()

        self._on_loop()

    def run_forever(self) -> None:
        self._on_pre_loop()
        self._log.debug('Setup done, entering loop')
        while not self.stop_running.is_set():
            self.run_once()
        self._on_post_loop()
