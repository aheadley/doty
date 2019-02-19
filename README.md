# doty
>[Doty, take this down...](https://www.youtube.com/watch?v=JiG_tOoqfIM)

Doty is (primarily) a Mumble <> IRC bridge/transcription bot. It will transcribe any speech
in a Mumble channel and output it to IRC, while copying or speaking any IRC
messages into Mumble.

The transcription uses Google Cloud Speech-to-Text and the quality is... ok. Google's
transcription does not seem to be geared toward casual conversation so it can
make a lot of mistakes, but generally you can reasonably follow a conversation in
Mumble just from the transcription.

## Pre-requisites

You will need a [Google Cloud](https://cloud.google.com/) account, along with
a JSON blob of credentials authorized to use the transcription and text-to-speech
services.

Note that because of the way Google's billing for the transcription works
(audio less than 15 seconds is rounded up to 15 seconds) it can be very expensive to
run Doty. It is recommended to increase `router.min_buffer_len` in the config to
somewhere in the 10-15 second range to be more cost effective, though this comes
at the cost of responsiveness in IRC.

## Installation

To install:

~~~~bash
pip install -r requirements.txt
~~~~

## Usage

Edit a copy of the example config file and set it up to your tastes:

~~~~bash
cp -a config.yml.example my-config.yml
$EDITOR my-config.yml
~~~~

Then you can just run doty against that config file:

~~~~bash
./doty.py -c my-config.yml
~~~~

Doty has several commandline options, though most of them are only useful for
debugging:

~~~~
usage: doty.py [-h] [-c CONFIG] [-v] [-q] [-M] [-I] [-T] [-S] [-A]

optional arguments:
  -h, --help            show this help message and exit
  -c CONFIG, --config CONFIG
                        Path to config file
  -v, --verbose         Log more messages
  -q, --quiet           Log fewer messages
  -M, --no-mumble       Do not start the mumble process
  -I, --no-irc          Do not start the irc process
  -T, --no-transcriber  Do not start the transcriber process
  -S, --no-speaker      Do not start the speaker process
  -A, --no-autorestart  Don't automatically restart crashed processes
~~~~
