import importlib
import asyncio

from .msgtypes import Channel


HOOK_NAMES = (
    'config',
    'post_session_end',
    'assistant_message',
    'discord_setup',
)


class Plugin:
    _registry = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Plugin._registry[cls.__module__] = cls

    @classmethod
    def load(cls, name, bot, config):
        name = f'plugins.{name}'
        if name not in cls._registry:
            importlib.import_module(name)

        plugin = cls._registry[name](bot)
        asyncio.run(plugin.on_config(config))
        return plugin

    def __init__(self, bot):
        self.__bot = bot

    @property
    def assistant(self):
        return self.__bot.assistant

    @property
    def hooks(self):
        for name in HOOK_NAMES:
            hook = getattr(self, 'on_' + name, None)
            if hook:
                yield (name, hook)

    def send_message(self, message, *, channel=Channel.CHAT):
        channel_obj = self.__bot.get_channel(channel)
        print(channel, channel_obj, message)
        if channel_obj is not None:
            return self.__bot.send_message(channel_obj, message)
        else:
            return asyncio.gather()

    def register_discord_commands(self, tree):
        pass
