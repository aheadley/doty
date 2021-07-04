from __future__ import annotations
from collections.abc import (
    Mapping,
)
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    List,
    Mapping,
    NewType,
    Optional,
    TypeVar,
    Union,
)

import fnmatch
import time
from typing import NamedTuple

from .utils import generate_uuid

from enum import (
    Enum,
    auto,
    unique,
)


@unique
class MessageType(Enum):
    PROC_EXIT = auto()

    MMBL_CONNECTED = auto()
    MMBL_DISCONNECTED = auto()
    MMBL_USER_CREATE = auto()
    MMBL_USER_UPDATE = auto()
    MMBL_USER_REMOVE = auto()
    MMBL_CHANNEL_CREATE = auto()
    MMBL_CHANNEL_UPDATE = auto()
    MMBL_CHANNEL_REMOVE = auto()

    MMBL_RECV_CHANNEL_TEXT = auto()
    MMBL_SEND_CHANNEL_TEXT = auto()
    MMBL_RECV_USER_TEXT = auto()
    MMBL_SEND_USER_TEXT = auto()
    MMBL_RECV_CHANNEL_AUDIO = auto()
    MMBL_RECV_CHANNEL_AUDIO_BUFFERED = auto()
    MMBL_SEND_CHANNEL_AUDIO_APPEND = auto()
    MMBL_SEND_CHANNEL_AUDIO_MIX = auto()
    MMBL_RECV_USER_AUDIO = auto()
    MMBL_RECV_USER_AUDIO_BUFFERED = auto()
    MMBL_SEND_USER_AUDIO_APPEND = auto()
    MMBL_SEND_USER_AUDIO_MIX = auto()

    MMBL_MOVE_TO_CHANNEL = auto()
    MMBL_DROP_CHANNEL_AUDIO_BUFFER = auto()
    MMBL_DROP_USER_AUDIO_BUFFER = auto()

    IRC_CONNECTED = auto()
    IRC_DISCONNECTED = auto()
    IRC_USER_CREATE = auto()
    IRC_USER_UPDATE = auto()
    IRC_USER_REMOVE = auto()

    IRC_RECV_CHANNEL_TEXT = auto()
    IRC_SEND_CHANNEL_TEXT = auto()
    IRC_RECV_USER_TEXT = auto()
    IRC_SEND_USER_TEXT = auto()
    IRC_RECV_CHANNEL_ACTION = auto()
    IRC_SEND_CHANNEL_ACTION = auto()

    TRANSCRIBE_MESSAGE_REQUEST = auto()
    TRANSCRIBE_MESSAGE_RESPONSE = auto()

    SYNTHESIZE_MESSAGE_REQUEST = auto()
    SYNTHESIZE_MESSAGE_RESPONSE = auto()

    PLUGIN_INIT = auto()
    PLUGIN_CMD_AUDIO = auto()
    PLUGIN_CMD_TEXT = auto()

PacketData = NewType('PacketData', Dict[str, Any])
PacketMetadata = NewType('PacketMetadata', Dict[str, Union[str, float]])
SerializedPacket = NewType(
    'Packet', Dict[str, Union[int, Optional[PacketData], PacketMetadata]])


class Packet:
    @classmethod
    def _build(cls, packet_type: MessageType,
               data: Optional[PacketData] = None,
               metadata: Optional[PacketMetadata] = None) -> Packet:
        return cls(packet_type, data, metadata)

    @classmethod
    def new(cls, packet_type: MessageType, data: Optional[PacketData] = None) -> Packet:
        return cls._build(packet_type, data)

    @classmethod
    def copy(cls, packet: Packet, as_type: MessageType) -> Packet:
        return cls._build(
            packet_type=as_type,
            data=packet.data,
            metadata=packet.metadata,
        )

    @classmethod
    def forward(cls, packet: Packet, as_type: MessageType, data: Optional[PacketData] = None) -> Packet:
        return cls._build(packet_type=as_type, data=data, metadata=packet.metadata)

    @classmethod
    def parse(cls, packet: SerializedPacket) -> Packet:
        return cls(**packet)

    def __init__(self, type: Union[MessageType, int], data: Optional[PacketData] = None, metadata: Optional[PacketMetadata] = None):
        if isinstance(type, int):
            type = MessageType[type]
        self.type = type
        self.data = data
        if metadata is None:
            metadata = self._generate_metadata()
        self.metadata = metadata

    def _generate_metadata(self) -> PacketMetadata:
        return {
            'timestamp': time.time(),
            'txid': generate_uuid(),
        }

    def serialize(self) -> SerializedPacket:
        return {
            'type': self.type.value,
            'data': self.data,
            'metadata': self.metadata,
        }

def MatchPacketType(packet: Packet, params: List[str]) -> bool:
    return any(fnmatch.fnmatch(packet.id.name, pattern) for pattern in params)

def MatchAlways(packet: Packet, params: Any) -> bool:
    return True

T = TypeVar('T')
U = TypeVar('U')
class RouterRule(NamedTuple):
    name: str
    destination: U
    params: T = None
    type: Callable[[Packet, T], bool] = MatchPacketType
    stop_on_match: bool = True

class PacketRouter:
    def __init__(self, rules: List[RouterRule], require_default_route: bool = True):
        if require_default_route and not self._rules_have_default_route(rules):
            raise ValueError('No default rule found')
        self._rules = rules

    def _rules_have_default_route(rules: List[RouterRule]) -> bool:
        return any(r.type is MatchAlways for r in rules)

    def route(self, packet: Packet) -> Generator[U, None, None]:
        for rule in self._rules:
            if rule.type(packet, rule.params):
                yield rule.destination
                if rule.stop_on_match:
                    break
