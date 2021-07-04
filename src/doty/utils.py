from typing import (
    Any,
    BinaryIO,
    Iterable,
    Mapping,
    Union,
)

from .constants import (
    APP_NAME,
    DENOTIFY_CHAR,
    EXTRA_DEBUG_ENV_VAR,
    UPLOAD_TARGET_URL,
)

import hashlib
import logging
import os
import time
import uuid

import requests

def generate_uuid() -> str:
    return str(uuid.uuid4())

def sha1sum(data: Union[str, bytes, bytearray]) -> str:
    if type(data) is str:
        data = data.encode('utf-8')
    return hashlib.sha1(data).hexdigest()

def batch(i: Iterable[Any], batch_size: int) -> Iterable[Iterable[Any]]:
    return zip(*[iter(i)] * batch_size)

def deep_merge_dict(into: Mapping[Any, Any], from_: Mapping[Any, Any]) -> Mapping[Any, Any]:
    for k, v in from_.items():
        if isinstance(from_[k], dict):
            if k in into:
                if not isinstance(into[k], dict):
                    raise ValueError('Non-matching types for key: {}\ninto: {}\nfrom: {}'.format(
                        k, into[k], from_[k]))
                else:
                    into[k] = deep_merge_dict(into[k], from_[k])
                    continue
        into[k] = from_[k]
    return into

def denotify(text: str) -> str:
    if len(text) > 1:
        # insert DENOTIFY_CHAR after the first letter and before the second
        # then remove any double DENOTIFY_CHARs
        text = DENOTIFY_CHAR.join([
            text[0],
            text[1:-1],
            text[-1],
        ]).replace(DENOTIFY_CHAR * 2, DENOTIFY_CHAR)
    return text

def undenotify(text: str) -> str:
    return text.replace(DENOTIFY_CHAR, '')

def upload(handle: BinaryIO) -> str:
    resp = requests.post(UPLOAD_TARGET_URL, files={'file': handle})
    return resp.text.strip()

def get_worker_logger(name: str, level: Any=logging.DEBUG) -> logging.Logger:
    log = logging.getLogger('{}.{}'.format(APP_NAME, name))
    log.setLevel(level)
    return log

def extra_debugging_enabled() -> bool:
    return bool(os.environ.get(EXTRA_DEBUG_ENV_VAR, False))
