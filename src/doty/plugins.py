import googleapiclient.discovery
import requests
import wolframalpha

from .plugin_host import AbstractPlugin, TextCommandMixin, VerbalCommandMixin

class BridgePlugin(AbstractPlugin): pass
class WolframAlphaPlugin(AbstractPlugin, VerbalCommandMixin): pass
class MediaPlayerPlugin(AbstractPlugin, VerbalCommandMixin, TextCommandMixin): pass
class RecorderPlugin(AbstractPlugin): pass
class ClippingPlugin(AbstractPlugin, VerbalCommandMixin, TextCommandMixin): pass
