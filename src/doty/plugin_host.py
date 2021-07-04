import contexttimer
import snips_nlu

from .constants import (
    STATIC_DATA_DIR,
)
from .ipc import (
    Packet,
    PacketRouter,
    RouterRule,
)
from .mp_utils import (
    multiprocessify,
    BaseWorker,
)

class AbstractPlugin(BaseWorker):
    pass
class TextCommandMixin:
    pass
class VerbalCommandMixin:
    pass

class PluginHostWorker(BaseWorker):
    PROC_NAME: str = 'plugin-host'

    def _on_pre_loop(self) -> None:
        super()._on_pre_loop()

    def _on_recv_packet(self, packet: Packet) -> None:
        super()._on_recv_packet(packet)

PluginHostWorkerProcess = multiprocessify(PluginHostWorker)


# class CommandParser:
#     DATASET_FILENAMES = [
#         'command-dataset.yml',
#     ]
#     UNKNOWN_INTENT_RESPONSES = [
#         'Sorry, I didn\'t understand that.',
#         'What was that?',
#         'Sorry, could you say that again?',
#         'What did you say?',
#         'Would you repeat that?',
#         'I don\'t know what you mean.',
#         'I didn\'t catch that.',
#     ]
#     INTERSTITIAL_RESPONSES = [
#         'Uhmm.',
#         'Let me see.',
#         'Hhmm.',
#         'One sec.',
#     ]
#     YOUTUBE_URL_FMT = 'https://www.youtube.com/watch?v={vid}'
#     BANG_CHARACTER = '!'
#     ICECAST_STATUS_URL = '/status-json.xsl'

#     _intent_registry = {}
#     _bang_registry = {}

#     def handle_intent(intent): # pylint: disable=no-self-argument
#         def wrapper(func):
#             try:
#                 func.intents.append(intent)
#             except AttributeError:
#                 func.intents = [intent]
#             return func
#         return wrapper

#     def handle_bang(bang): # pylint: disable=no-self-argument
#         def wrapper(func):
#             try:
#                 func.bangs.append(bang)
#             except AttributeError:
#                 func.bangs = [bang]
#             return func
#         return wrapper

#     def __init__(self, config):
#         self._use_interstitials = config['use_interstitials']
#         self._setup_registry()
#         self._prepare_radio_stations(config['icecast_base_url'])
#         self._engine = self._setup_parse_engine()
#         self._wa_client = self._setup_wolfram_alpha(config['wolframalpha_api_key'])
#         self._yt_client = self._setup_youtube(config['youtube_api_key'])

#     def _setup_registry(self):
#         for member in self.__class__.__dict__.values():
#             try:
#                 for intent in member.intents:
#                     self.__class__._intent_registry[intent] = member
#                     log.debug('Set handler for intent: %s -> %s', intent, member.__name__)
#             except AttributeError: pass
#             try:
#                 for bang in member.bangs:
#                     self.__class__._bang_registry[bang] = member
#                     log.debug('Set handler for bang: %s -> %s', bang, member.__name__)
#             except AttributeError: pass

#     def _prepare_radio_stations(self, icecast_base_url):
#         if icecast_base_url is None:
#             self._station_map = {}
#         else:
#             station_index = requests.get(icecast_base_url + self.ICECAST_STATUS_URL).json()
#             self._station_map = {
#                 s['title']: icecast_base_url + s['listenurl'].strip('.')
#                 for s in station_index['icestats']['source']
#             }

#     def _fixup_dataset(self, dataset):
#         for e in dataset.entities:
#             if e.name == 'radio_station':
#                 e.utterances = [snips_nlu.dataset.entity.EntityUtterance(k.lower())
#                     for k in self._station_map]
#                 break
#         return dataset

#     def _setup_wolfram_alpha(self, api_key):
#         if api_key is None:
#             return None
#         return wolframalpha.Client(api_key)

#     def _setup_parse_engine(self):
#         engine = snips_nlu.SnipsNLUEngine(config=snips_nlu.default_configs.CONFIG_EN)
#         log.debug('Training model on datasets: %s', ', '.join(self.DATASET_FILENAMES))
#         with contexttimer.Timer() as t:
#             dataset = snips_nlu.dataset.Dataset.from_yaml_files('en',
#                 [os.path.join(os.path.dirname(__file__), STATIC_DATA_DIR, ds) for ds in self.DATASET_FILENAMES])
#             dataset = self._fixup_dataset(dataset)
#             engine.fit(dataset.json)
#         log.debug('Model trained in %0.3fs', t.elapsed)
#         return engine

#     def _setup_youtube(self, api_key):
#         if api_key is None:
#             return None
#         client = googleapiclient.discovery.build(
#             'youtube', 'v3', developerKey=api_key)
#         return client

#     def _duration_to_dt(self, duration):
#         dt = datetime.timedelta(
#             days=duration['days'],
#             seconds=duration['seconds'],
#             minutes=duration['minutes'],
#             hours=duration['hours'],
#             weeks=duration['weeks'])
#         return dt

#     def _buy_time_to_answer(self):
#         if self._use_interstitials:
#             say(random.choice(self.INTERSTITIAL_RESPONSES))

#     def dispatch_intent(self, src, msg):
#         result = self._engine.parse(msg)
#         log.debug('Intent parse result: %s', result)
#         intent = result['intent']['intentName'] or '__UNKNOWN__'
#         try:
#             handler = self.__class__._intent_registry[intent]
#         except KeyError:
#             log.warning('No registered handler for intent: %s', intent)
#         else:
#             slots = {s['slotName']: s['value'] for s in result['slots']}
#             for k in slots:
#                 try:
#                     slots[k] = slots[k]['value']
#                 except (TypeError, KeyError, IndexError):
#                     continue
#             return handler(self, src, result['input'], **slots)

#     def dispatch_bang(self, src, msg):
#         try:
#             bang, args = msg.split(' ', 1)
#         except ValueError:
#             bang = msg
#             args = None
#         try:
#             handler = self.__class__._bang_registry[bang[1:]]
#         except KeyError:
#             pass
#         else:
#             return handler(self, src, args)

#     @handle_bang('help')
#     def bang_help(self, src, input):
#         help_msg = ', '.join(self.BANG_CHARACTER + bang for bang in self._bang_registry.keys())
#         text('Available commands: ' + help_msg)

#     @handle_bang('say')
#     def bang_say(self, src, input):
#         if input is None:
#             text('{}: {}say <message>'.format(src['name'], self.BANG_CHARACTER))
#         else:
#             say('{} said: {}'.format(denotify_username(src['name']), input),
#                 source_id=src.get('actor', 0))

#     @handle_bang('play-ytdl')
#     def bang_play_yt(self, src, input):
#         if input is None:
#             text('{}: {}play-ytdl <url to play>'.format(src['name'], self.BANG_CHARACTER))
#         else:
#             text('Attempting to play: {}'.format(input))
#             media_conn.send({
#                 'cmd': MediaControlCommand.PLAY_VIDEO_URL,
#                 'url': input,
#             })

#     @handle_bang('play')
#     def bang_play_raw(self, src, input):
#         if input is None:
#             text('{}: {}play <url to play>'.format(src['name'], self.BANG_CHARACTER))
#         else:
#             text('Attempting to play: {}'.format(input))
#             media_conn.send({
#                 'cmd': MediaControlCommand.PLAY_AUDIO_URL,
#                 'url': input,
#             })

#     @handle_intent('__UNKNOWN__')
#     def unknown_intent(self, src, input):
#         say(random.choice(self.UNKNOWN_INTENT_RESPONSES))

#     @handle_intent('greeting')
#     def cmd_greeting(self, src, input):
#         say('Hello {}'.format(src['name']))

#     @handle_intent('help')
#     def cmd_help(self, src, input):
#         say('You can tell me to: make an audio clip or replay audio, change '
#             'to a different channel, or just ask a question. You can also '
#             'tell me to play radio stations and change the volume')

#     @handle_intent('cmd_generate_clip')
#     def cmd_generate_clip(self, src, input, duration=None):
#         if duration is None:
#             seconds = router_config['mixed_buffer_len'] / 2
#         else:
#             seconds = min(self._duration_to_dt(duration).seconds, router_config['mixed_buffer_len'])

#         with io.BytesIO(buffer2wav(MIXED_BUFFER.get_last(seconds))) as f:
#             url = upload(f)
#         if url:
#             resp = 'Clip of the last {} seconds: {}'.format(
#                 seconds, url)
#             text(resp)
#         else:
#             say('I wasn\'t able to upload the audio clip')
#             log.warning('Failed to upload audio clip')

#     @handle_intent('cmd_replay_audio')
#     def cmd_replay_audio(self, src, input, duration=None):
#         if duration is None:
#             say('You need to include how long you want me to replay')
#             return
#         seconds = min(self._duration_to_dt(duration).seconds, router_config['mixed_buffer_len'])

#         buf = MIXED_BUFFER.get_last(seconds)
#         MIXED_BUFFER.add_buffer(buf)
#         mmbl_conn.send({
#             'cmd': MumbleControlCommand.SEND_CHANNEL_AUDIO_MSG,
#             'buffer': buf,
#         })

#     @handle_intent('cmd_change_channel')
#     def cmd_change_channel(self, src, input, destination=None):
#         if destination is None:
#             say('Include the channel I should move to')
#             return

#         channel = process.extractOne(destination, [c['name'] for c in MMBL_CHANNELS.values()])[0]
#         log.info('Attempting to move to channel: %s', channel)
#         mmbl_conn.send({
#             'cmd': MumbleControlCommand.MOVE_TO_CHANNEL,
#             'channel_name': channel,
#         })

#     @handle_intent('cmd_wolfram_alpha_query')
#     def cmd_wolfram_alpha_query(self, src, input, query=None):
#         if self._wa_client is None:
#             return text('`router.command_params.wolframalpha_api_key` is not set')
#         self._buy_time_to_answer()
#         result = self._wa_client.query(input)
#         try:
#             answer = ' '.join(next(result.results).text.split('\n')).replace('|', '').strip()
#         except (Exception, StopIteration):
#             say('I don\'t know')
#         else:
#             say(answer)

#     @handle_intent('cmd_silence')
#     def cmd_silence(self, src, input):
#         media_conn.send({
#             'cmd': MediaControlCommand.STOP,
#         })
#         mmbl_conn.send({
#             'cmd': MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER,
#         })

#     @handle_intent('cmd_set_volume')
#     def cmd_set_volume(self, src, input, value=None):
#         if value is None:
#             say('Tell me what to set the volume to, between 10 and 100')
#             return
#         value = max(10, min(100, int(value))) / 100
#         media_conn.send({
#             'cmd': MediaControlCommand.SET_VOLUME,
#             'value': value,
#         })
#         text('Media volume set to {:02d}%'.format(int(value * 100)))

#     @handle_intent('cmd_lower_volume')
#     def cmd_lower_volume(self, src, input):
#         media_conn.send({
#             'cmd': MediaControlCommand.SET_VOLUME_LOWER,
#         })
#         text('Media volume decreased by {:02d}%'.format(int(VOLUME_ADJUSTMENT_STEP * 100)))

#     @handle_intent('cmd_raise_volume')
#     def cmd_raise_volume(self, src, input):
#         media_conn.send({
#             'cmd': MediaControlCommand.SET_VOLUME_HIGHER,
#         })
#         text('Media volume increased by {:02d}%'.format(int(VOLUME_ADJUSTMENT_STEP * 100)))

#     @handle_intent('cmd_play_radio')
#     def cmd_play_radio(self, src, input, station=None):
#         if not self._station_map:
#             return text('`router.command_params.icecast_base_url` is not set')
#         if station is None:
#             say('Tell me what radio station to play')
#             return
#         key, _ = process.extractOne(station, self._station_map.keys())
#         media_conn.send({
#             'cmd': MediaControlCommand.PLAY_AUDIO_URL,
#             'url': self._station_map[key],
#         })
#         mmbl_conn.send({
#             'cmd': MumbleControlCommand.DROP_CHANNEL_AUDIO_BUFFER,
#         })
#         text('Playing radio station: {}'.format(key))

#     @handle_intent('cmd_list_stations')
#     def cmd_list_stations(self, src, input):
#         text('Available radio stations: ' + ', '.join(self._station_map.keys()))

#     @handle_intent('cmd_play_media')
#     def cmd_play_media(self, src, input, source=None, track=None, artist=None, album=None):
#         if self._yt_client is None:
#             return text('`router.command_params.youtube_api_key` is not set')
#         if source is None:
#             # only youtube is supported for now
#             source = 'youtube'
#         if track is None:
#             say('Tell me the name of a song to play')
#             return
#         query = track
#         if artist is not None:
#             query += ' - {}'.format(artist)
#         elif album is not None:
#             query += ' - {}'.format(album)

#         try:
#             for video in self._yt_client.search().list(
#                         part='snippet,id', q=query, type='video',
#                         videoDimension='2d',
#                         regionCode='US',
#                         safeSearch='moderate',
#                         # videoDuration='short',
#                     ).execute()['items']:
#                 selected_video = video
#                 break
#         except Exception as err:
#             log.exception(err)
#             say('I couldn\'t find anything to play for that')
#             return
#         url = self.YOUTUBE_URL_FMT.format(vid=selected_video['id']['videoId'])
#         log.debug('Selected video: id=%s title=%s', selected_video['id']['videoId'], selected_video['snippet']['title'])
#         media_conn.send({
#             'cmd': MediaControlCommand.PLAY_VIDEO_URL,
#             'url': url,
#         })
#         text('Playing media: "{}" <{}>'.format(selected_video['snippet']['title'], url))
