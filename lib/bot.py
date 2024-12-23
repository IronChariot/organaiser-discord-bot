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

from .util import split_message, split_emoji, format_json_md
from .reminders import Reminders, Reminder
from .msgtypes import UserMessage, Attachment, Channel
from .plugin import Plugin, PinnedMessage
from . import views

# Max chars Discord allows to be sent per message
MESSAGE_LIMIT = 2000


async def make_discord_file(attachment):
    data = await attachment.read()
    filename = urlparse(attachment.url).path.replace('\\', '/').rsplit('/', 1)[-1]
    return discord.File(BytesIO(data), filename)


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

        self.plugins = {}
        self.__hooks = defaultdict(list)
        self.__pinned_messages = []
        self.__actions = {}
        for plugin, config in self.assistant.plugin_config.items():
            if config.get('enabled'):
                self.load_plugin(plugin, config)

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
        if len(message) > limit and '```' in message:
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

        elif message.strip():
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

    async def send_message_with_action_results(self, channel, message, *, actions_taken=[], futures=[]):
        file_futures = []
        exceptions = []

        for fut in asyncio.as_completed(futures):
            try:
                results = await fut
                if isinstance(results, Attachment) or isinstance(results, str):
                    results = [results]

                for result in results:
                    if isinstance(result, Attachment):
                        file_futures.append(asyncio.create_task(make_discord_file(result)))
                    elif isinstance(result, str):
                        actions_taken.append(result)

            except Exception as exc:
                exceptions.append(exc)

        files = []
        if file_futures:
            for fut in asyncio.as_completed(file_futures):
                try:
                    files.append(await fut)
                except Exception as exc:
                    exceptions.append(exc)

        # Add a log of actions taken in small text
        if actions_taken:
            message = '\n-# '.join([message or ''] + actions_taken)

        await self.send_message(channel, message, files=files)

        # Let the AI know about the exceptions.
        if exceptions:
            if len(files) == 1:
                text = f'SYSTEM: 1 attachment successfully sent to user, but the following encountered errors:'
            elif len(files) > 1:
                text = f'SYSTEM: {len(files)} attachments successfully sent to user, but the following encountered errors:'
            else:
                text = f'SYSTEM: Sent message without attachments due to the following errors:'

            for exception in exceptions:
                msg = str(getattr(exception, 'message', None) or exception)
                text += f'\n{msg}'

            asyncio.create_task(self.respond(text))

    async def respond(self, content, message=None, attachments=[]):
        """Respond to input from the user or the system."""

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

        futures = []
        action_futures = []
        actions_taken = []
        if self.log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            log_future = asyncio.create_task(
                self.send_message(self.log_channel, f'> {quoted_message}\n\n{format_json_md(response)}'))

        if 'bug_report' in response and self.bugs_channel:
            # This depends on the log future since it includes a jump link to
            # the log message
            futures.append(self.write_bug_report(response['bug_report'], message, log_future=log_future))
        elif self.log_channel:
            futures.append(log_future)

        if 'prompt_after' in response:
            # If there's an existing checkin task, cancel it
            if self.current_checkin_task and not self.current_checkin_task.done():
                self.current_checkin_task.cancel()

            self.current_checkin_task = asyncio.create_task(self.perform_checkin(response_time, response['prompt_after']))

        actions = set(self.__actions[key] for key in response if key in self.__actions)
        for action in actions:
            task = asyncio.create_task(action(**{key: response.get(key) for key in action._action_keys}))
            action_futures.append(task)

        if 'long_term_goals_action' in response:
            long_term_goal_action = response['long_term_goals_action']
            long_term_goal_text = response['long_term_goals_text']
            # Check if long_term_goal_text is a string, or a list of strings:
            long_term_goal_text_list = [long_term_goal_text] if isinstance(long_term_goal_text, str) else long_term_goal_text
            futures.append(self.update_long_term_goals(long_term_goal_action, long_term_goal_text_list))
            actions_taken.append(f'Modifying long term goals')

        if response.get('timed_reminder_time'):
            # Set up a timed reminder
            # Parse the time as a datetime:
            reminder_time = datetime.fromisoformat(response['timed_reminder_time'])
            # Make the reminder time local to UTC
            if not reminder_time.tzinfo and self.assistant.timezone is not None:
                reminder_time = reminder_time.replace(tzinfo=self.assistant.timezone)
            reminder_time = reminder_time.astimezone(timezone.utc)
            reminder_text = response['timed_reminder_text']
            repeat = response.get('timed_reminder_repeat', False)
            repeat_interval = response.get('timed_reminder_repeat_interval', 'day')

            reminder = Reminder(reminder_time, reminder_text, repeat, repeat_interval)
            futures.append(self.add_timed_reminder(reminder))

            timestamp = int(reminder_time.timestamp())
            rel_date = None
            if reminder_time.date() == date.today():
                rel_date = 'today'
            elif reminder_time.date() == date.today() + timedelta(days=1):
                rel_date = 'tomorrow'
            else:
                rel_date = f'<t:{timestamp}:R>'
            if repeat and repeat_interval == 'day':
                actions_taken.append(f'Added daily reminder at <t:{timestamp}:t> starting {rel_date}')
            elif repeat:
                actions_taken.append(f'Added {repeat_interval}ly reminder starting {rel_date}')
            elif rel_date == 'today':
                actions_taken.append(f'Added reminder going off <t:{timestamp}:R>')
            else:
                actions_taken.append(f'Added reminder going off {rel_date} at <t:{timestamp}:t>')

        chat = response.get('chat') or ''
        reactions = response.get('react')

        if self.chat_channel:
            # If there's no chat message and there's no user message to react
            # to OR we want to report on actions taken, the reacts become the
            # chat message
            if reactions and not chat and (not message or actions_taken):
                chat = reactions
                reactions = None

            if chat or action_futures:
                futures.insert(0, self.send_message_with_action_results(self.chat_channel, chat, actions_taken=actions_taken, futures=action_futures))

        if message and reactions:
            for emoji in split_emoji(reactions):
                futures.insert(0, message.add_reaction(emoji))

        if futures:
            results = await asyncio.gather(*futures, return_exceptions=True)

            # Check exceptions and report them
            exc = None
            for result in results:
                if isinstance(result, Exception):
                    exc = result
                    trace = ''.join(traceback.format_exception(exc)).rstrip()
                    # Hack to insert zero-width spaces
                    trace = trace.replace('```', '`​`​`')
                    report = f'```python\n{trace}\n```'
                    asyncio.create_task(self.write_bug_report(report, message, log_future=log_future))

            # Re-raise so it shows up properly in the log
            if exc:
                raise exc

    async def write_bug_report(self, report, message=None, log_future=None):
        # Disobedience proofing
        if not isinstance(report, str):
            report = format_json_md(report)

        if log_future:
            log_message = await log_future
            report = f' - [Raw AI response]({log_message.jump_url})\n{report}'
        if message:
            report = f' - [User message]({message.jump_url})\n{report}'

        await self.send_message(self.bugs_channel, report)

    async def add_timed_reminder(self, reminder):
        if not self.assistant.reminders.add_reminder(reminder):
            # Already existed
            return

        # If the reminder is for later today, set up a task to send it
        if reminder.time < self.session.get_next_rollover():
            asyncio.create_task(self.send_reminder(reminder))

        # Update the pinned reminders message
        if self.chat_channel:
            await self.reminders_message.update()

    async def get_reminders_message(self):
        return self.assistant.reminders.as_markdown(self.assistant.timezone)

    async def update_long_term_goals(self, long_term_goal_action, long_term_goal_text_list):
        # Get the list of existing long term goals, if any:
        with self.assistant.open_memory_file('long_term_goals.json', default='{}') as fh:
            long_term_goals_json = fh.read()
        long_term_goals_dict = json.loads(long_term_goals_json)

        if long_term_goal_action == 'add':
            for long_term_goal_text in long_term_goal_text_list:
                long_term_goals_dict[long_term_goal_text] = True

        elif long_term_goal_action == 'remove':
            for long_term_goal_text in long_term_goal_text_list:
                if long_term_goal_text in long_term_goals_dict:
                    del long_term_goals_dict[long_term_goal_text]

        else:
            return

        # Write the dict back to file
        with self.assistant.open_memory_file('long_term_goals.json', 'w') as fh:
            fh.write(json.dumps(long_term_goals_dict))

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
                await self.respond(f'(Immediately thereafter…)')
            elif prompt_after == 1:
                await self.respond(f'(One minute later…)')
            else:
                await self.respond(f'({prompt_after} minutes later…)')
        except asyncio.CancelledError:
            print('Checkin task was cancelled')
            return

    async def send_reminder(self, reminder: Reminder):
        print(f'Setting reminder for {reminder.time}: {reminder.text}')

        begin_time = datetime.now(tz=timezone.utc)
        if reminder.time > begin_time:
            await asyncio.sleep((reminder.time - begin_time).total_seconds())

        await self.respond(f'SYSTEM: Reminder from your past self now going off: {reminder.text}')

        # Remove the reminder from the list
        self.assistant.reminders.remove_reminder(reminder.time, reminder.text)

        # If the reminder repeats, set up a new reminder for the next time (after 1 interval):
        if reminder.repeat:
            new_time = reminder.time + reminder.repeat_delta
            while new_time < begin_time:
                new_time += reminder.repeat_delta

            self.assistant.reminders.add_reminder(Reminder(new_time, reminder.text, repeat=True, repeat_interval=reminder.repeat_interval))

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')

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

                if content.startswith('## Current Reminders\n'):
                    self.reminder_list_message = pin

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

            for prompt in await asyncio.gather(*self.call_hooks('system_prompt')):
                self.session.standard_format_prompt += '\n' + prompt.strip()

            if self.log_channel:
                futures.append(self.send_message(self.log_channel, self.session.initial_system_prompt))

            futures += self.call_hooks('post_session_end', old_session)
            futures.append(self.change_presence(status=discord.Status.online))

            # Schedule next day's reminders
            next_rollover = self.session.get_next_rollover()
            for reminder in self.assistant.reminders.get_reminders_before(next_rollover):
                asyncio.create_task(self.send_reminder(reminder))

        if self.log_channel:
            futures.append(self.log_channel.send(f'Finished rollover to day {date}'))

        if self.chat_channel:
            futures.append(self.reminders_message.update())

        if futures:
            await asyncio.gather(*futures)

    async def setup_hook(self):
        for prompt in await asyncio.gather(*self.call_hooks('system_prompt')):
            self.session.standard_format_prompt += '\n' + prompt.strip()

        self.rollover_lock = asyncio.Lock()

        rollover_time = self.assistant.rollover.replace(tzinfo=self.assistant.timezone)
        print(f'Date is {self.session.date}, next rollover scheduled at {rollover_time}')
        self.check_rollover.change_interval(time=rollover_time)
        self.check_rollover.start()

        next_rollover = self.session.get_next_rollover()
        for reminder in self.assistant.reminders.get_reminders_before(next_rollover):
            asyncio.create_task(self.send_reminder(reminder))

        self.reminders_message = PinnedMessage(self.get_reminders_message,
                                               header='## Current Reminders',
                                               discord_view=views.ReminderListView(self))
        self.__pinned_messages.append(self.reminders_message)
        self.add_view(self.reminders_message._discord_view)

        for plugin in self.plugins.values():
            for pin in plugin._pinned_messages:
                view = pin._init_discord_view(plugin=plugin, bot=self)
                if view is not None:
                    self.add_view(view)

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

    def load_plugin(self, name, config):
        plugin = Plugin.load(name, self, config)
        self.plugins[name] = plugin
        for name, hook in plugin._hooks.items():
            self.__hooks[name].append(hook)

        for pinned_msg in plugin._pinned_messages:
            self.__pinned_messages.append(pinned_msg)

        for key, func in plugin._actions.items():
            self.__actions[key] = func

    def call_hooks(self, name, *args, **kwargs):
        for hooks in self.__hooks[name]:
            for hook in hooks:
                yield hook(*args, **kwargs)
