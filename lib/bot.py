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
from .msgtypes import UserMessage, Attachment, Channel, Role
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

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.__pinned_messages = []

        self.response_loop_task = None

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

    async def response_loop_wrapper(self):
        """Catches any exceptions happening in the response loop and restarts
        it as necessary."""

        while True:
            try:
                await self.response_loop()
            except Exception as ex:
                try:
                    print("Response loop aborted with exception!")
                    traceback.print_exc()
                    await self.write_bug_report(ex)
                except:
                    pass

            await asyncio.sleep(10)

    async def response_loop(self):
        """Runs forever to check for new user messages and prompts the assistant
        to respond to them."""

        if not self.__ready.done():
            await self.__ready

        print("Starting response loop.")

        # Check when the next check-in should be
        prompt_after = 10
        deadline = datetime.now(tz=timezone.utc) + timedelta(minutes=prompt_after)
        message = self.session.get_last_assistant_message()
        if message and message.timestamp:
            try:
                response = message.parse_json()
            except json.JSONDecodeError:
                response = {}

            if isinstance(response, dict) and isinstance(response.get('prompt_after'), (float, int)):
                prompt_after = int(response['prompt_after'])
                deadline = message.timestamp + timedelta(minutes=prompt_after)

        while True:
            # Wait for a new user message, or the prompt_after to time out
            cur_time = datetime.now(tz=timezone.utc)
            seconds_left = (deadline - cur_time).total_seconds()
            if deadline < cur_time:
                print("Next check-in OVERDUE by", cur_time - deadline)
            else:
                print("Next check-in at", deadline)
                try:
                    if self.session.last_message.role != Role.USER:
                        await asyncio.wait_for(self.session.new_user_message, timeout=seconds_left)

                    # If we got one, wait a bit for inactivity
                    delay = self.assistant.response_delay
                    if delay > 0:
                        while True:
                            await asyncio.wait_for(self.session.new_user_message, timeout=delay)

                except asyncio.TimeoutError:
                    pass

            last_message = self.session.last_message
            if last_message.role != Role.USER:
                # There's no user message to respond to, we have to generate
                # one, presumably we just woke up due to the prompt_after
                cur_time = datetime.now(tz=timezone.utc)
                if cur_time < deadline:
                    # What?  Apparently not?
                    print(f"Woke up spuriously with {deadline - cur_time} to go.")
                    continue

                if last_message.timestamp:
                    elapsed = int(round((cur_time - last_message.timestamp).total_seconds() / 60))
                else:
                    elapsed = int(prompt_after)

                print(f'Checking in due to user inactivity for {elapsed} minutes.')
                if elapsed == 0:
                    await self.session.push_message(self.make_user_message(f'(Immediately thereafter…)'))
                elif elapsed == 1:
                    await self.session.push_message(self.make_user_message(f'(One minute later…)'))
                else:
                    await self.session.push_message(self.make_user_message(f'({elapsed} minutes later…)'))

            try:
                async with self.chat_channel.typing():
                    response = await self.prompt_response()

                if response is not None and response.prompt_after is not None:
                    prompt_after = response.prompt_after
                else:
                    prompt_after = 10
                deadline = self.session.last_message.timestamp + timedelta(minutes=prompt_after)

            except Exception as ex:
                await self.write_bug_report(ex)

    async def prompt_response(self):
        """Prompts a response from the assistant and handles it."""

        if not self.__ready.done():
            await self.__ready

        while True:
            try:
                response = await self.session.query_assistant_response()
                break
            except ValueError as ex:
                channel = self.chat_channel or self.log_channel
                if channel is None:
                    raise
                view = views.RetryButton()
                err_msg = await channel.send(f'⚠️ **Error**: {ex}', view=view)
                await view.wait()
                try:
                    await err_msg.delete()
                except:
                    pass
                if not view.retry:
                    return

        if not response:
            return

        # Find the corresponding user messages.
        messages = []
        for user_message in response.user_messages:
            try:
                messages.append(await self.chat_channel.fetch_message(user_message.id))
            except:
                message = None

        first_message = messages[0] if messages else None
        last_message = messages[-1] if messages else None

        response_time = datetime.now(tz=timezone.utc)

        # Keep track of all the tasks we spawn, so that we can await them and
        # catch the exceptions at the end.
        tasks = []
        tasks.extend(self.assistant.run_actions(response))

        log_future = None
        if self.log_channel:
            log_message = format_json_md(response.raw_data)

            if response.user_messages:
                quoted_message = '\n> '.join(line for um in response.user_messages if um.content for line in um.content.splitlines())
                log_message = f'> {quoted_message}\n\n{log_message}'

            log_future = asyncio.create_task(
                self.send_message(self.log_channel, log_message))
            tasks.append(log_future)

        if response.bug_report and self.bugs_channel:
            # This depends on the log future since it includes a jump link to
            # the log message
            tasks.append(asyncio.create_task(self.write_bug_report(response.bug_report, first_message, log_future=log_future)))

        if last_message:
            for emoji in response.reactions:
                tasks.append(asyncio.create_task(last_message.add_reaction(emoji)))

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
            if not chat and response.reactions and not last_message:
                chat = ''.join(response.reactions)

            # A message with just "actions taken" sends no notification
            silent = not chat and not files

            # Add a log of actions taken in small text
            if response.actions_taken:
                chat = '\n-# '.join([chat or ''] + response.actions_taken)

            if chat or files:
                tasks.insert(0, self.send_message(self.chat_channel, chat, files=files, silent=silent))

        # Check exceptions and report them.  This includes any exceptions from
        # the action tasks, which were ignored earlier.
        gatherer = asyncio.gather(*tasks, return_exceptions=True)

        exc = None
        for result in await gatherer:
            if isinstance(result, Exception):
                await self.write_bug_report(result, message=messages[0] if messages else None, log_future=log_future)

        return response

    async def write_bug_report(self, report, message=None, log_future=None):
        await self.__ready
        if not self.bugs_channel:
            return

        if isinstance(report, BaseException):
            trace = ''.join(traceback.format_exception(None, report, report.__traceback__)).rstrip()
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

    async def send_message(self, channel, message, files=[], silent=False):
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
                last_msg = await channel.send(send_next, files=files, silent=silent)

        elif message and message.strip():
            # Simple case
            parts = split_message(message, limit)
            for part in parts[:-1]:
                await channel.send(part)

            last_msg = await channel.send(parts[-1], files=files, silent=silent)

        elif files:
            last_msg = await channel.send(files=files, silent=silent)

        else:
            return None

        return last_msg

    def make_user_message(self, content, message=None, attachments=[]):
        timestamp = message.created_at if message else datetime.now(tz=timezone.utc)
        timestamp_str = timestamp.astimezone(self.assistant.timezone).strftime("%H:%M:%S")
        content = f'[{timestamp_str}] {content}'

        message_id = message.id if message else None

        user_message = UserMessage(content, id=message_id, timestamp=timestamp)
        for attach in attachments:
            user_message.attach(attach.url, attach.content_type)
        return user_message

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

        # Run the response loop in the background.
        if not self.response_loop_task or self.response_loop_task.done():
            self.response_loop_task = asyncio.create_task(self.response_loop_wrapper())

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
            new_messages = [self.make_user_message(f'SYSTEM: The following messages were sent while you were offline:')]

            for message in missed_messages:
                content = f'{message.author.display_name}: {message.content}'
                new_messages.append(self.make_user_message(content, message, message.attachments))

            new_messages.append(self.make_user_message('SYSTEM: End of missed messages.'))
            await self.session.push_messages(new_messages)

    async def on_message(self, message):
        if message.author == self.user:
            return

        if not message.content and not message.attachments:
            return

        if message.channel == self.chat_channel:
            msg = self.make_user_message(f'{message.author.display_name}: {message.content}', message, message.attachments)
            await self.session.push_message(msg)

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
