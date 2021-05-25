# doty
>[Doty, take this down...](https://www.youtube.com/watch?v=JiG_tOoqfIM)

Doty is (primarily) a Mumble <> IRC bridge/transcription bot. It will transcribe any speech
in a Mumble channel and output it to IRC, while copying or speaking any IRC
messages into Mumble. Currently only English is supported, though the various
libraries doty uses could support other languages so that might be improved in
the future.

Doty provides a couple of options for Speech-to-Text but the quality of the
transcription regardless of engine is merely passable. Automated transcription
does not seem to be geared toward casual conversation so it can
make a lot of mistakes, but generally you can reasonably follow a conversation in
Mumble just from the transcription.

## Pre-requisites

Doty no longer requires any 3rd party web service accounts for speech, but still requires
some preparation:

### General Commands

  - `WolframAlpha`: A [WolframAlpha API key](https://products.wolframalpha.com/api/)
    is required for the WolframAlpha query intent
  - `YouTube API`: A [YouTube API key](https://developers.google.com/youtube/registering_an_application)
    is required to search videos on YouTube

### Speech to Text Engines

  - `vosk`: A [model](https://alphacephei.com/vosk/models)
    will need to be downloaded and pointed to in the configuration. The transcription
    is relatively good, but does not support hint phrases so recognition on names
    can be very spotty.
  - `coqui-stt`: A [model and scorer](https://stt.readthedocs.io/en/latest/DEPLOYMENT.html#download-models)
    will need to be downloaded and pointed to in the configuration. Not recommended,
    the transcription is very poor and slow.
  - `ibm-watson`: An [IBM Watson Cloud](https://www.ibm.com/watson/developercloud/) account
    with an IAM API key authorized to use the Speech-to-Text service. The transcription
    is reasonably good but even casual use will probably be rather expensive.

### Text to Speech Engines

  - `coqui-tts`: The configured model will be automatically downloaded when doty
    starts up. The quality of the speech is (usually) quite good but can have some
    occasional hiccups, and is very slow.
  - `google-tts`: A [Google Cloud](https://cloud.google.com/) account with a
    [JSON blob of credentials](https://cloud.google.com/iam/docs/creating-managing-service-account-keys)
    to use the Text-to-Speech service. The quality of the speech is good, and
    casual use will probably fall under the free-tier.

## Installation

Build/run requirements:

  - `python-devel`
  - `libsamplerate`
  - `rust`
  - `cargo`
  - `gcc-c++`
  - `ffmpeg`
  - `youtube-dl`

To install (recommend using a virtualenv):

~~~~bash
pip install -Ur requirements.pre-install.txt
pip install -Ur requirements.txt
snips-nlu download en
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

### Commands

Doty supports commands through voice or text (in mumble or IRC), there are examples
of the available voice commands in `command-dataset.yml`, though they will need to be
prefixed by a configured activation word (ex: "robot, help").

> "robot, what can you do?"
>> You can tell me to: make an audio clip or replay audio, change to a different
channel, or just ask a question. You can also tell me to play radio stations
and change the volume
> !help
>> Available commands: !help, !say, !play-ytdl, !play
