import importlib
import asyncio
from dataclasses import dataclass
from functools import wraps
import discord
from discord.ext import commands

from .msgtypes import Channel


HOOK_NAMES = (
    'configure',
    'system_prompt',
    'post_session_end',
)

def wrap_discord_command(bot, func, *args, **kwargs):
    @bot.tree.command(*args, **kwargs)
    @wraps(func)
    async def wrapper(interaction: discord.Interaction):
        return await func(bot, interaction)

    return wrapper


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
        for hook in plugin._get_hooks('configure'):
            asyncio.run(hook(config))
        return plugin

    def __init__(self, bot):
        self.__bot = bot
        self._hooks = {}
        self._actions = {}
        self._pinned_messages = []

        for name in dir(self):
            method = getattr(self, name)
            if hasattr(method, '_hook_name'):
                hooks = self._hooks.setdefault(method._hook_name, [])
                hooks.append(method)

            if hasattr(method, '_action_keys'):
                for key in method._action_keys:
                    self._actions[key] = method

            if hasattr(method, '_pin_header'):
                msg = PinnedMessage(method)
                setattr(self, name, msg)
                self._pinned_messages.append(msg)

            if hasattr(method, '_discord_command'):
                args, kwargs = method._discord_command
                setattr(self, name, wrap_discord_command(bot, method, *args, **kwargs))

    def _get_hooks(self, name):
        return self._hooks.get(name, ())

    @property
    def assistant(self):
        return self.__bot.assistant

    def send_message(self, message, *, channel=Channel.CHAT):
        channel_obj = self.__bot.get_channel(channel)
        if channel_obj is not None:
            return self.__bot.send_message(channel_obj, message)
        else:
            return asyncio.gather()


def hook(name):
    assert name in HOOK_NAMES, f"Invalid hook '{name}'"

    def decorator(func):
        func._hook_name = name
        return func

    return decorator


def action(key, *args):
    """Decorator used to register an action that can be taken by the assistant.
    It is identified by one or more keys that will be included by the LLM in the
    response, which will be passed to the action as keyword arguments."""
    keys = frozenset((key, )) | frozenset(args)

    def decorator(func):
        func._action_keys = keys
        return func

    return decorator


def pinned_message(*, header, discord_view=None):
    def decorator(func):
        func._pin_header = header
        func._pin_discord_view = discord_view
        return func

    return decorator


def discord_command(*args, **kwargs):
    def decorator(func):
        func._discord_command = (args, kwargs)
        return func

    return decorator


class PinnedMessage:
    def __init__(self, func, *, header=None, discord_view=None):
        self._func = func
        if not header:
            header = func._pin_header
        self.header = header.strip()
        self._discord_view = discord_view
        self._discord_message = None

    def _init_discord_view(self, *args, **kwargs):
        if not self._discord_view and self._func._pin_discord_view:
            self._discord_view = self._func._pin_discord_view(*args, **kwargs)
        return self._discord_view

    async def update(self):
        if self._discord_message:
            content = await self._func()
            content = self.header + '\n\n' + content
            await self._discord_message.edit(content=content, view=self._discord_view)
