#!/usr/bin/env python3

import collections
import ctypes
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
import larynx
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

LOG_LEVEL = logging.INFO

def getWorkerLogger(worker_name, level=logging.DEBUG):
    log = logging.getLogger('{}.{}'.format(APP_NAME, worker_name))
    log.setLevel(level)
    return log
