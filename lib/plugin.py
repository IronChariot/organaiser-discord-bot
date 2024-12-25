import importlib
import asyncio
import inspect
from datetime import datetime

import discord
from discord.ext import commands

from .msgtypes import Channel


HOOK_NAMES = (
    'init',
    'session_load',
    'configure',
    'post_session_end',
)

class Plugin:
    _registry = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        Plugin._registry[cls.__module__] = cls

    @classmethod
    def load(cls, name, assistant):
        name = f'plugins.{name}'
        if name not in cls._registry:
            importlib.import_module(name)

        plugin = cls._registry[name](assistant)
        asyncio.run(plugin._async_init())
        return plugin

    def __init__(self, assistant):
        self.__assistant = assistant
        self._bot = None
        self._hooks = {}
        self._actions = {}
        self._pinned_messages = []
        self._discord_commands = []
        self._static_system_prompts = []
        self._dynamic_system_prompts = []
        self._scheduled_tasks = set()

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
                self._discord_commands.append(method)

            if hasattr(method, '_dynamic_system_prompt'):
                self._dynamic_system_prompts.append(method)
            elif hasattr(method, '_static_system_prompt'):
                self._static_system_prompts.append(method)

    async def _async_init(self):
        self._bot_future = asyncio.Future()

        for hook in self._get_hooks('init'):
            await hook()

    def _init_bot(self, bot):
        self._bot = bot
        self._bot_future.set_result(bot)

    def _get_hooks(self, name):
        """Returns a list of registered hooks with the given name."""
        return self._hooks.get(name, ())

    @property
    def assistant(self):
        """Returns the Assistant object, which will not change for this instance
        of the Plugin."""
        return self.__assistant

    def schedule(self, when, coro, /):
        """Schedules the given coroutine to run at the specified datetime.
        If when is in the past, it will be scheduled right away.
        Returns an object that can be cancelled or awaited."""

        assert when.tzinfo is not None and when.tzinfo.utcoffset(when) is not None
        assert inspect.iscoroutine(coro)

        async def wait_and_run(when, coro):
            begin_time = datetime.now(tz=when.tzinfo)
            if when > begin_time:
                await asyncio.sleep((when - begin_time).total_seconds())

            try:
                await asyncio.shield(coro)
            except Exception as ex:
                if self._bot is not None:
                    await self._bot.write_bug_report(ex)
                else:
                    raise

        task = asyncio.create_task(wait_and_run(when, coro))
        task.add_done_callback(self._scheduled_tasks.discard)
        self._scheduled_tasks.add(task)
        return task

    def send_message(self, message, *, channel=Channel.CHAT):
        """If this Assistant is running in a Discord bot, sends a message to
        the specified channel, if that channel is configured.

        Otherwise, does nothing."""

        channel_obj = self._bot.get_channel(channel) if self._bot is not None else None
        if channel_obj is not None:
            return self._bot.send_message(channel_obj, message)
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


def system_prompt(func=None, /, *, dynamic=False):
    def decorator(func):
        assert not inspect.iscoroutinefunction(func)
        if dynamic:
            func._dynamic_system_prompt = True
        else:
            func._static_system_prompt = True
        return func

    if func is not None:
        return decorator(func)
    else:
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
