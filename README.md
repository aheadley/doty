# doty
>[Doty, take this down...](https://www.youtube.com/watch?v=JiG_tOoqfIM)

Doty is (primarily) a Mumble <> IRC bridge/transcription bot. It will transcribe any speech
in a Mumble channel and output it to IRC, while copying or speaking any IRC
messages into Mumble.

The transcription uses [IBM Watson Speech-to-Text](https://www.ibm.com/watson/services/speech-to-text/)
and the quality is... ok. Automated transcription does not seem to be geared 
toward casual conversation so it can
make a lot of mistakes, but generally you can reasonably follow a conversation in
Mumble just from the transcription.

## Pre-requisites

  - An [IBM Watson Cloud](https://www.ibm.com/watson/developercloud/) account
    with an IAM API key authorized to use the Speech-to-Text service.
  - A [Google Cloud](https://cloud.google.com/) account with a
    [JSON blob of credentials](https://cloud.google.com/iam/docs/creating-managing-service-account-keys)
    to use the Text-to-Speech service.

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

Once running, you can use `!help` (in IRC or Mumble) to see the available commands.
