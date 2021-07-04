from typing import (
    Optional,
    TypeVar,
)
from collections.abc import (
    Mapping,
    Callable,
)

import functools
import logging
import multiprocessing
import signal

import setproctitle

from .constants import (
    APP_NAME,
    LONG_POLL,
)
from .ipc import (
    MessageType,
    Packet,
)

T = TypeVar('T')

def halt_process(proc: multiprocessing.Process) -> None:
    if proc.exitcode is None:
        try:
            # only available in 3.7+
            proc.kill()
        except AttributeError:
            proc.terminate()

def get_worker_logger(worker_name: str) -> logging.Logger:
    return logging.getLogger({'{}.{}'.format(APP_NAME, worker_name)})

def _run_worker(cls: T, router_conn: multiprocessing.connection.Connection, config: Mapping) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    cls(router_conn, config).run_forever()

def multiprocessify(cls: T) -> Callable[[multiprocessing.connection.Connection, Mapping], multiprocessing.Process]:
    @functools.wraps(cls)
    def wrapper(router_conn: multiprocessing.connection.Connection, config: Mapping ={}) -> multiprocessing.Process:
        proc = multiprocessing.Process(target=_run_worker, args=(cls, router_conn, config))
        proc.PROC_NAME = cls.PROC_NAME
        proc.start()
        return proc
    wrapper.PROC_NAME = cls.PROC_NAME
    return wrapper

class BaseWorker:
    PROC_NAME: str = 'worker'
    ROUTER_POLL_TIMEOUT: float = LONG_POLL

    def __init__(self, router_conn: multiprocessing.connection.Connection, config: Mapping ={}):
        self._conn: multiprocessing.connection.Connection = router_conn
        self._config: Mapping = config
        self._log: logging.Logger = get_worker_logger(self.PROC_NAME)

    def _on_pre_loop(self) -> None:
        setproctitle.setproctitle('{}: {}'.format(APP_NAME, self.PROC_NAME))
        self._log.debug('%s worker starting', self.APP_NAME)

    def _on_loop(self) -> None:
        pass

    def _on_recv_packet(self, packet: Packet) -> None:
        if packet.type is MessageType.PROC_EXIT:
            self._log.debug('Recieved PROC_EXIT packet, exiting...')
            self._keep_running = False

    def _on_post_loop(self) -> None:
        self._log.debug('%s worker stopping', self.APP_NAME)
        self._conn.close()

    def run_once(self) -> None:
        if self._conn.poll(self.ROUTER_POLL_TIMEOUT):
            has_data_in_conn = True
            while has_data_in_conn:
                pkt = Packet.parse(self._conn.recv())
                self._on_recv_packet(pkt)
                has_data_in_conn = self._conn.poll()

        self._on_loop()

    def run_forever(self) -> None:
        self._keep_running = True

        self._on_pre_loop()
        self._log.debug('Setup done, entering loop')
        while self._keep_running:
            self.run_once()
        self._on_post_loop()
