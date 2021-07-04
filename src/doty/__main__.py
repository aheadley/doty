#!/usr/bin/env python3

import argparse
import logging
import signal

from .core import (
    RouterWorker,
    WorkerManager,
    WorkerType,
)

def handle_sigint(*args) -> None:
    router.stop_running.set()
signal.signal(signal.SIGINT, handle_sigint)
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s.%(msecs)03d [%(process)07d] %(levelname)-7s %(name)s: %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%S',
)
logging.captureWarnings(True)

LOG_LEVEL = logging.INFO
EXTRA_DEBUG = False


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
