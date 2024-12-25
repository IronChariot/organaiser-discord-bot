import json
from datetime import date, datetime, time, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
from io import BytesIO
from urllib.parse import urlparse
import traceback
from collections import defaultdict
from functools import wraps

from .util import split_message, format_json_md
from .msgtypes import UserMessage, Attachment, Channel
from . import views

# Max chars Discord allows to be sent per message
MESSAGE_LIMIT = 2000


class Retry(discord.ui.View):
    def __init__(self):
        super().__init__()
        self.retry = False

    @discord.ui.button(label='Retry', style=discord.ButtonStyle.primary)
    async def btn_retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.retry = True
        self.stop()

    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()


class Bot(discord.Client):
    def __init__(self, session):
        self.session = session
        self.assistant = session.assistant
        self.chat_channel = None
        self.log_channel = None
        self.diary_channel = None
        self.query_channel = None
        self.bugs_channel = None
        self.current_checkin_task = None

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.__pinned_messages = []

    def _register_command(self, func):
        args, kwargs = func._discord_command

        @self.tree.command(*args, **kwargs)
        @wraps(func)
        async def command(interaction: discord.Interaction):
            return await func(self, interaction)

        return command

    def get_channel(self, channel):
        #TODO better system for channels
        if channel == Channel.CHAT:
            return self.chat_channel
        elif channel == Channel.LOG:
            return self.log_channel
        elif channel == Channel.DIARY:
            return self.diary_channel
        elif channel == Channel.QUERY:
            return self.query_channel
        elif channel == Channel.BUGS:
            return self.bugs_channel

    async def send_message(self, channel, message, files=[]):
        limit = MESSAGE_LIMIT
        if message and len(message) > limit and '```' in message:
            # Hard case, preserve preformatted blocks across split messages.
            parts = list(split_message(message, limit-12))
            blocks = 0
            send_next = ''
            for part in parts:
                if send_next:
                    last_msg = await channel.send(send_next)

                part_blocks = part.count('```')

                if blocks % 2 != 0:
                    part = '```json\n' + part

                blocks += part_blocks

                if blocks % 2 != 0:
                    part = part.rstrip('\n')
                    if part.endswith('```') or part.endswith('```json'):
                        part = part.rstrip('json').rstrip('`\n')
                    else:
                        part = part + '\n```'

                send_next = part

            # Last one gets sent with the files
            if send_next or files:
                last_msg = await channel.send(send_next, files=files)

        elif message and message.strip():
            # Simple case
            parts = split_message(message, limit)
            for part in parts[:-1]:
                await channel.send(part)

            last_msg = await channel.send(parts[-1], files=files)

        elif files:
            last_msg = await channel.send(files=files)

        else:
            return None

        return last_msg

    async def respond(self, content, message=None, attachments=[]):
        """Respond to input from the user or the system."""

        if not self.__ready.done():
            await self.__ready

        timestamp = message.created_at if message else datetime.now(tz=timezone.utc)
        timestamp_str = timestamp.astimezone(self.assistant.timezone).strftime("%H:%M:%S")
        content = f'[{timestamp_str}] {content}'

        message_id = message.id if message else None
        user_message = UserMessage(content, id=message_id, timestamp=timestamp)
        for attach in attachments:
            user_message.attach(attach.url, attach.content_type)

        retry = True
        while retry:
            try:
                response = await self.session.chat(user_message)
                retry = False
            except ValueError as ex:
                channel = message.channel if message else (self.chat_channel or self.log_channel)
                view = views.RetryButton()
                err_msg = await channel.send(f'⚠️ **Error**: {ex}', view=view)
                await view.wait()
                retry = view.retry
                try:
                    await err_msg.delete()
                except:
                    pass
                if not retry:
                    return

        response_time = datetime.now(tz=timezone.utc)

        # Keep track of all the tasks we spawn, so that we can await them and
        # catch the exceptions at the end.
        tasks = []
        tasks.extend(self.assistant.run_actions(response))

        log_future = None
        if self.log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            log_future = asyncio.create_task(
                self.send_message(self.log_channel, f'> {quoted_message}\n\n{format_json_md(response.raw_data)}'))
            tasks.append(log_future)

        if response.bug_report and self.bugs_channel:
            # This depends on the log future since it includes a jump link to
            # the log message
            tasks.append(asyncio.create_task(self.write_bug_report(response.bug_report, message, log_future=log_future)))

        if response.prompt_after is not None:
            # If there's an existing checkin task, cancel it
            if self.current_checkin_task and not self.current_checkin_task.done():
                self.current_checkin_task.cancel()

            self.current_checkin_task = asyncio.create_task(self.perform_checkin(response_time, response.prompt_after))

        if message:
            for emoji in response.reactions:
                tasks.append(asyncio.create_task(message.add_reaction(emoji)))

        if self.chat_channel:
            # Convert all attachments into Discord files
            files = []
            async for attachment, data in response.read_attachments():
                filename = urlparse(attachment.url).path.replace('\\', '/').rsplit('/', 1)[-1]
                files.append(discord.File(BytesIO(data), filename))

            # Wait for all actions to be done, as they may alter the response
            await response.wait_for_actions()

            # If there's no chat message and there's no user message to react to
            # then the react becomes the chat message
            chat = response.chat
            if not chat and response.reactions and not message:
                chat = ''.join(response.reactions)

            # Add a log of actions taken in small text
            if response.actions_taken:
                chat = '\n-# '.join([chat or ''] + response.actions_taken)

            if chat or files:
                tasks.insert(0, self.send_message(self.chat_channel, chat, files=files))

        # Check exceptions and report them.  This includes any exceptions from
        # the action tasks, which were ignored earlier.
        gatherer = asyncio.gather(*tasks, return_exceptions=True)

        exc = None
        for result in await gatherer:
            if isinstance(result, Exception):
                await self.write_bug_report(result, message=message, log_future=log_future)

        # Re-raise so it shows up properly in the log
        if exc:
            raise exc

    async def write_bug_report(self, report, message=None, log_future=None):
        await self.__ready
        if not self.bugs_channel:
            return

        if isinstance(report, BaseException):
            trace = ''.join(traceback.format_exception(report)).rstrip()
            # Hack to insert zero-width spaces
            trace = trace.replace('```', '`​`​`')
            report = f'```python\n{trace}\n```'

        # Disobedience proofing
        if not isinstance(report, str):
            report = format_json_md(report)

        if log_future:
            log_message = await log_future
            report = f' - [Raw AI response]({log_message.jump_url})\n{report}'
        if message:
            report = f' - [User message]({message.jump_url})\n{report}'

        await self.send_message(self.bugs_channel, report)

    async def perform_checkin(self, last_activity, prompt_after):
        try:
            deadline = last_activity + timedelta(minutes=prompt_after)
            cur_time = datetime.now(tz=timezone.utc)
            if deadline < cur_time:
                print("Next check-in OVERDUE by", cur_time - deadline)
            else:
                print("Next check-in at", deadline)

            while deadline > cur_time:
                seconds_left = (deadline - cur_time).total_seconds()
                await asyncio.sleep(min(3600, seconds_left))
                cur_time = datetime.now(tz=timezone.utc)

            # Unassign this otherwise respond() will cancel us
            self.current_checkin_task = None

            print(f'Checking in due to user inactivity for {prompt_after} minutes.')
            if prompt_after == 0:
                coro = self.respond(f'(Immediately thereafter…)')
            elif prompt_after == 1:
                coro = self.respond(f'(One minute later…)')
            else:
                coro = self.respond(f'({prompt_after} minutes later…)')

            # Shield response from cancellation
            await asyncio.shield(coro)
        except asyncio.CancelledError:
            print('Checkin task was cancelled')
            return

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')

        if not self.__ready.done():
            self.__ready.set_result(None)

        # Reset these if necessary
        for pin in self.__pinned_messages:
            pin._discord_message = None

        config = self.assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        query_channel_name = config.get('query_channel')
        bugs_channel_name = config.get('bugs_channel')

        all_channels = list(self.get_all_channels())
        self.chat_channel = discord.utils.get(all_channels, name=chat_channel_name) if chat_channel_name else None
        self.log_channel = discord.utils.get(all_channels, name=log_channel_name) if log_channel_name else None
        self.diary_channel = discord.utils.get(all_channels, name=diary_channel_name) if diary_channel_name else None
        self.query_channel = discord.utils.get(all_channels, name=query_channel_name) if query_channel_name else None
        self.bugs_channel = discord.utils.get(all_channels, name=bugs_channel_name) if bugs_channel_name else None

        # Do this in the background, it takes a long time
        if self.chat_channel:
            guild = self.chat_channel.guild
            self.tree.copy_global_to(guild=guild)
            sync_task = asyncio.create_task(self.tree.sync(guild=guild))
        else:
            sync_task = None

        if self.chat_channel:
            pin_messages = []

            for message in await self.chat_channel.pins():
                if not message.content:
                    continue

                content = message.content.split('\n', 1)[0]

                for pin in self.__pinned_messages:
                    if content == pin.header:
                        pin._discord_message = message
                        if not message.pinned:
                            pin_messages.append(message)
                        break

            for pin in self.__pinned_messages:
                if not pin._discord_message:
                    message = await self.chat_channel.send(content=pin.header, view=pin._discord_view)
                    pin._discord_message = message
                    pin_messages.append(message)

            try:
                for message in pin_messages:
                    await message.pin()
            except:
                close_btn = views.CloseButton()
                close_btn.message = await self.chat_channel.send('### ⚠️ **Error**\nPinning failed, please pin the above message(s) manually.', view=close_btn, delete_after=180)

            await asyncio.gather(*(pin.update() for pin in self.__pinned_messages))

        # Check if any messages came in while we were down
        await self.check_downtime_messages()

        if sync_task:
            await sync_task

    async def check_downtime_messages(self):
        if not self.chat_channel:
            return

        # Get the last user message with a Discord id
        for message in self.session.message_history[::-1]:
            if message.id:
                last_user_msg = message
                break
        else:
            return

        try:
            last_discord_msg = await self.chat_channel.fetch_message(last_user_msg.id)
        except:
            # May be deleted. TODO: go by timestamp or find other id?
            return

        missed_messages = []
        async for message in self.chat_channel.history(after=last_discord_msg):
            if message.author != self.user and (message.content or message.attachments):
                missed_messages.append(message)

        if missed_messages:
            timestamp = datetime.now(tz=self.assistant.timezone).strftime("%H:%M:%S")
            self.session.append_message(UserMessage(f'[{timestamp}] SYSTEM: The following messages were sent while you were offline:'))

            for message in missed_messages:
                timestamp = message.created_at.astimezone(self.assistant.timezone).strftime("%H:%M:%S")
                content = f'[{timestamp}] {message.author.display_name}: {message.content}'

                message = UserMessage(content, id=message.id, timestamp=message.created_at)
                for attach in message.attachments:
                    message.attach(attach.url, attach.content_type)

                self.session.append_message(message)

            await self.respond('SYSTEM: End of missed messages.')

    async def on_message(self, message):
        if message.author == self.user:
            return

        if not message.content and not message.attachments:
            return

        if message.channel == self.chat_channel:
            async with message.channel.typing():
                await self.respond(f'{message.author.display_name}: {message.content}', message, message.attachments)

        if message.channel == self.query_channel:
            async with message.channel.typing():
                attachments = [Attachment(attach.url, attach.content_type) for attach in message.attachments]

                try:
                    reply = await self.session.isolated_query(message.content, attachments=attachments)
                    if reply.startswith('{'):
                        reply = f'```json\n{reply}\n```'
                except ValueError as ex:
                    reply = f'⚠️ **Error**: {ex}'

                await self.send_message(self.query_channel, reply)

    async def on_raw_message_edit(self, payload):
        message = self.session.find_message(payload.message_id)
        if not message:
            return

        if "content" in payload.data:
            message.content = payload.data["content"]

        # I think Discord only supports removing attachments?
        if "attachments" in payload.data:
            attach_ids = set(int(attach["id"]) for attach in payload.data["attachments"])
            message.attachments = [attachment for attachment in message.attachments if attachment.id in attach_ids]

        self.session._rewrite_message_file()

    async def on_raw_message_delete(self, payload):
        self.session.delete_message(payload.message_id)

    @tasks.loop(reconnect=True)
    async def check_rollover(self):
        date = self.assistant.get_today()
        if date == self.session.date:
            print(f'Not rolling over, date is still {date}')
            return

        futures = []

        # Grab lock and check again in case this method is being called multiple
        # times simultaneously somehow
        async with self.rollover_lock:
            old_session = self.session
            if date == old_session.date:
                print(f'Not rolling over, date is still {date}')
                return

            await self.change_presence(status=discord.Status.dnd)

            print(f'Rolling over to day {date}')
            if self.log_channel:
                await self.log_channel.send(f'Beginning rollover to day {date}')

            self.session = await self.assistant.load_session(date, old_session)

            if self.log_channel:
                futures.append(self.send_message(self.log_channel, self.session.initial_system_prompt))

            futures += self.assistant.call_hooks('session_load', self.session)
            futures += self.assistant.call_hooks('post_session_end', old_session)
            futures.append(self.change_presence(status=discord.Status.online))

        if self.log_channel:
            futures.append(self.log_channel.send(f'Finished rollover to day {date}'))

        if futures:
            await asyncio.gather(*futures)

    async def setup_hook(self):
        self.__ready = asyncio.Future()

        for plugin in self.assistant.plugins.values():
            plugin._init_bot(self)

            for func in plugin._discord_commands:
                self._register_command(func)

        self.rollover_lock = asyncio.Lock()

        rollover_time = self.assistant.rollover.replace(tzinfo=self.assistant.timezone)
        print(f'Date is {self.session.date}, next rollover scheduled at {rollover_time}')
        self.check_rollover.change_interval(time=rollover_time)
        self.check_rollover.start()

        for plugin in self.assistant.plugins.values():
            for pin in plugin._pinned_messages:
                view = pin._init_discord_view(plugin=plugin, bot=self)
                if view is not None:
                    self.add_view(view)
                self.__pinned_messages.append(pin)

        await asyncio.gather(*self.assistant.call_hooks('session_load', self.session))

        # Check when the next check-in should be
        message = self.session.get_last_assistant_message()
        if message:
            try:
                response = message.parse_json()
            except json.JSONDecodeError:
                return

            if 'prompt_after' in response:
                self.current_checkin_task = asyncio.create_task(
                    self.perform_checkin(message.timestamp or self.session.last_activity, response['prompt_after']))
