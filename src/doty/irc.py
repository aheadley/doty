from typing import (
    Any,
    Dict,
)

import ssl

import irc.client
import irc.connection

from .constants import (
    SHORT_POLL,
)
from .ipc import (
    MessageType,
    Packet,
)
from .mp_utils import (
    BaseWorker,
    multiprocessify,
)
from .utils import undenotify

def event2dict(ev: Any) -> Dict:
    return {
        'type': ev.type,
        'source': ev.source.split('!')[0] if '!' in ev.source else ev.source,
        'target': ev.target,
        'args': ev.arguments,
    }

class IRCWorker(BaseWorker):
    PROC_NAME: str = 'irc'

    IRC_EVENT_MAP: Dict[str, MessageType] = {
        'pubmsg': MessageType.IRC_RECV_CHANNEL_TEXT,
        'privmsg': MessageType.IRC_RECV_USER_TEXT,
        'action': MessageType.IRC_RECV_CHANNEL_ACTION,
        'join': MessageType.IRC_USER_CREATE,
        'kick': MessageType.IRC_USER_REMOVE,
        'part': MessageType.IRC_USER_REMOVE,
        'quit': MessageType.IRC_USER_REMOVE,
        'nick': MessageType.IRC_USER_UPDATE,
    }
    
    def _on_pre_loop(self) -> None:
        super()._on_pre_loop()

        self._client = irc.client.Reactor()
        self._server = self._client.server()

        for ev_type in self.IRC_EVENT_MAP.keys():
            self._client.add_global_handler(ev_type, self._handle_irc_event)

    def _on_loop(self) -> None:
        super()._on_loop()

        if not self._server.connected:
            if self._config['ssl']:
                ssl_ctx = ssl.create_default_context()
                if not self._config['ssl_verify']:
                    ssl_ctx.verify_mode = ssl.CERT_NONE
                conn_factory = irc.connection.Factory(
                    wrapper=lambda s: ssl_ctx.wrap_socket(s, server_hostname=self._config['server']),
                    ipv6=self._config['allow_ipv6'],
                )
            else:
                conn_factory = irc.connection.Factory(ipv6=self._config['allow_ipv6'])

            self._server.connect(self._config['server'], self._config['port'],
                self._config['username'], connect_factory=conn_factory)
            self._server.join(self._config['channel'])

        self._client.process_once(SHORT_POLL)

    def _on_recv_packet(self, packet: Packet) -> None:
        super()._on_recv_packet(packet)

        if packet.type is MessageType.IRC_SEND_CHANNEL_TEXT:
            self._log.info('Sending message to channel: %s <- "%s"',
                self._config['channel'], undenotify(packet.data['msg']))
            self._server.privmsg(self._config['channel'], packet.data['msg'])
        elif packet.type is MessageType.IRC_SEND_USER_TEXT:
            self._log.info('Sending message to user: %s <- "%s"',
                packet.data['user'], undenotify(packet.data['msg']))
            self._server.privmsg(packet.data['user'], packet.data['msg'])
        elif packet.type is MessageType.IRC_SEND_CHANNEL_ACTION:
            self._log.info('Sending action to channel: %s <- "%s"',
                self._config['channel'], undenotify(packet.data['msg']))
            self._server.action(self._config['channel'], packet.data['msg'])

    def _handle_irc_event(self, conn, event) -> None:
        self._log.debug('Recieved event: %s', event)
        pkt_data = event2dict(event)
        pkt_type = self.IRC_EVENT_MAP.get(event.type, None)

        if pkt_type is None:
            self._log.warning('Unrecognized event type: %s', event.type)
        elif pkt_data['source'] == self._config['username']:
            self._log.debug('Ignoring event from ourself: %r', pkt_data)
        else:
            self._conn.send(Packet.new(pkt_type, pkt_data).serialize())

IRCWorkerProcess = multiprocessify(IRCWorker)
