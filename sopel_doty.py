#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import print_function

import tempfile
import subprocess
import os
import os.path
import logging
import re
import multiprocessing
import functools

import sopel.module
import sopel.tools
import sopel.logger
from sopel.config.types import StaticSection, ValidatedAttribute, ChoiceAttribute, ListAttribute

CONFIG_NAME = 'doty'

log = sopel.logger.get_logger(CONFIG_NAME)
log.setLevel(logging.DEBUG)

def multiprocessify(func):
    @functools.wraps(func)
    def wrapper(*pargs, **kwargs):
        return multiprocessing.Process(target=func, args=pargs, kwargs=kwargs)
    return wrapper

def getWorkerLogger(worker_name, level=logging.DEBUG):
    logging.basicConfig()
    log = logging.getLogger('sopel.modules.{}.{}-{:05d}'.format(CONFIG_NAME, worker_name, os.getpid()))
    log.setLevel(level)
    return log

class DotySection(StaticSection):
	socket_path		= ValidatedAttribute('socket_path', str)

def setup(bot):
    bot.config.define_section(CONFIG_NAME, DotySection)

@sopel.module.commands('animeme')
def cmd_animeme(bot, trigger):
    words = trigger.group(2).strip()
    bot.reply('Something went wrong, no url found')
cmd_animeme.priority = 'medium'
