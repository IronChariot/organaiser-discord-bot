import json
from datetime import date, datetime, time, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
from io import BytesIO
from urllib.parse import urlparse
import traceback

from .util import split_message, split_emoji
from .reminders import Reminders, Reminder
from .msgtypes import UserMessage, Attachment

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

    async def send_message(self, channel, message, file_futures=[]):
        files = []
        exceptions = []
        if file_futures:
            for i, result in enumerate(await asyncio.gather(*file_futures, return_exceptions=True)):
                if isinstance(result, Exception):
                    exceptions.append((i, result))
                else:
                    files.append(result)

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

        # Let the AI know about the exceptions.
        if exceptions:
            if len(files) == 1:
                text = f'SYSTEM: 1 attachment successfully sent to user, but the following encountered errors:'
            elif len(files) > 1:
                text = f'SYSTEM: {len(files)} attachments successfully sent to user, but the following encountered errors:'
            else:
                text = f'SYSTEM: Sent message without attachments due to the following errors:'

            for i, exception in exceptions:
                msg = str(getattr(exception, 'message', None) or exception)
                text += f'\nAttachment {i}: {msg}'

            asyncio.create_task(self.respond(text))

        return last_msg

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
                view = Retry()
                err_msg = await channel.send(f'⚠️ **Error**: {ex}', view=view)
                await view.wait()
                retry = view.retry
                await err_msg.delete()
                if not retry:
                    return

        response_time = datetime.now(tz=timezone.utc)

        # Any images we need to generate can be done in parallel
        image_futures = []
        if 'images' in response and self.assistant.image_model:
            images = response['images']
            if isinstance(images, dict):
                images = [images]

            for image in images:
                image_futures.append(asyncio.create_task(self.generate_image(**image)))

        futures = []
        if self.chat_channel:
            chat = response.get('chat')
            reactions = response.get('react')

            # If there's no chat message and there's no user message to react
            # to, the reacts become the chat message
            if reactions and not chat and not message:
                chat = reactions
                reactions = None

            if chat:
                futures.append(self.send_message(self.chat_channel, chat, image_futures))
            elif image_futures:
                futures.append(self.send_message(self.chat_channel, '', image_futures))

            if reactions:
                for emoji in split_emoji(reactions):
                    futures.append(message.add_reaction(emoji))

        if self.log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            log_future = asyncio.create_task(
                self.send_message(self.log_channel, f'> {quoted_message}\n\n```json\n{json.dumps(response, indent=4)}\n```'))

        if 'bug_report' in response and self.bugs_channel:
            # This depends on the log future since it includes a jump link to
            # the log message
            futures.append(self.write_bug_report(response['bug_report'], message, log_future=log_future))
        else:
            futures.append(log_future)

        if 'prompt_after' in response:
            # If there's an existing checkin task, cancel it
            if self.current_checkin_task and not self.current_checkin_task.done():
                self.current_checkin_task.cancel()

            self.current_checkin_task = asyncio.create_task(self.perform_checkin(response_time, response['prompt_after']))

        # Take care of the todo list and long term goals list
        if 'todo_action' in response:
            todo_action = response['todo_action']
            todo_text = response['todo_text']
            # Check if todo_text is a string, or a list of strings:
            todo_text_list = [todo_text] if isinstance(todo_text, str) else todo_text
            futures.append(self.update_todo(todo_action, todo_text_list))

        if 'long_term_goals_action' in response:
            long_term_goal_action = response['long_term_goals_action']
            long_term_goal_text = response['long_term_goals_text']
            # Check if long_term_goal_text is a string, or a list of strings:
            long_term_goal_text_list = [long_term_goal_text] if isinstance(long_term_goal_text, str) else long_term_goal_text
            futures.append(self.update_long_term_goals(long_term_goal_action, long_term_goal_text_list))

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

        if futures:
            results = await asyncio.gather(*futures, return_exceptions=True)

            # Check exceptions and report them
            exc = None
            for result in results:
                if isinstance(result, Exception):
                    exc = result
                    trace = ''.join(traceback.format_exception(exc)).rstrip()
                    report = f'```python\n{trace}\n```'
                    asyncio.create_task(self.write_bug_report(report, message, log_future=log_future))

            # Re-raise so it shows up properly in the log
            if exc:
                raise exc

    async def write_bug_report(self, report, message=None, log_future=None):
        # Disobedience proofing
        if not isinstance(report, str):
            report = json.dumps(report, indent=4)
            report = f'```json\n{report}\n```'

        if log_future:
            log_message = await log_future
            report = f' - [Raw AI response]({log_message.jump_url})\n{report}'
        if message:
            report = f' - [User message]({message.jump_url})\n{report}'

        await self.send_message(self.bugs_channel, report)

    async def generate_image(self, **kwargs):
        img = await self.assistant.image_model.generate_image(**kwargs)
        data = await img.read()
        filename = urlparse(img.url).path.replace('\\', '/').rsplit('/', 1)[-1]
        return discord.File(BytesIO(data), filename)

    async def add_timed_reminder(self, reminder):
        if not self.assistant.reminders.add_reminder(reminder):
            # Already existed
            return

        # If the reminder is for later today, set up a task to send it
        if reminder.time < self.session.get_next_rollover():
            asyncio.create_task(self.send_reminder(reminder))

        # Update the pinned reminders message
        if self.chat_channel:
            await self.update_reminders_message()

    async def update_reminders_message(self):
        header = '## Current Reminders\n\n'
        reminders_text = header + self.assistant.reminders.as_markdown(self.assistant.timezone)

        for pin in await self.chat_channel.pins():
            if pin.content and (pin.content + '\n\n').startswith(header):
                await pin.edit(content=reminders_text)
                break
        else:
            msg = await self.chat_channel.send(reminders_text)
            try:
                await msg.pin()
            except:
                await self.chat_channel.send('⚠️ **Error**: pinning failed, please pin the above message manually')

    async def update_todo_message(self):
        with self.assistant.open_memory_file('todo.json', default='[]') as fh:
            todos_json = fh.read().strip()

        todos_list = json.loads(todos_json) if todos_json else []

        header = '## Current TODOs\n'
        todos_text = header + '\n - ' + '\n - '.join(todos_list)
        for pin in await self.chat_channel.pins():
            if pin.content and (pin.content + '\n\n').startswith(header):
                await pin.edit(content=todos_text)
                break
        else:
            msg = await self.chat_channel.send(todos_text)
            try:
                await msg.pin()
            except:
                await self.chat_channel.send('⚠️ **Error**: pinning failed, please pin the above message manually')

    async def update_todo(self, todo_action, todo_text_list):
        # Get the list of existing todos, if any:
        with self.assistant.open_memory_file('todo.json', default='[]') as fh:
            todos_json = fh.read().strip()

        todos_list = json.loads(todos_json) if todos_json else []

        if todo_action == 'add':
            for todo_text in todo_text_list:
                if todo_text not in todos_list:
                    todos_list.append(todo_text)

        elif todo_action == 'remove':
            for todo_text in todo_text_list:
                if todo_text in todos_list:
                    todos_list.remove(todo_text)
                # Also correctly deal with it if the bot put the whole '- ' at the front of the todo text
                elif todo_text.startswith('- ') and todo_text[2:] in todos_list:
                    todos_list.remove(todo_text[2:])

        else:
            return

        # Write the dict back to file
        with self.assistant.open_memory_file('todo.json', 'w') as fh:
            json.dump(todos_list, fh)

        # Update the pinned to do message
        if self.chat_channel:
            await self.update_todo_message()

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

        config = self.assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        query_channel_name = config.get('query_channel')
        bugs_channel_name = config.get('bugs_channel')
        self.chat_channel = discord.utils.get(self.get_all_channels(), name=chat_channel_name) if chat_channel_name else None
        self.log_channel = discord.utils.get(self.get_all_channels(), name=log_channel_name) if log_channel_name else None
        self.diary_channel = discord.utils.get(self.get_all_channels(), name=diary_channel_name) if diary_channel_name else None
        self.query_channel = discord.utils.get(self.get_all_channels(), name=query_channel_name) if query_channel_name else None
        self.bugs_channel = discord.utils.get(self.get_all_channels(), name=bugs_channel_name) if bugs_channel_name else None

        await self.tree.sync(guild=self.chat_channel.guild)

        # Check if any messages came in while we were down
        await self.check_downtime_messages()

        if self.chat_channel:
            await self.update_reminders_message()
            await self.update_todo_message()

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

            if not old_session.diary_path.exists():
                entry = await old_session.write_diary_entry()

                if self.diary_channel:
                    futures.append(self.send_message(self.diary_channel, entry))

            futures.append(self.change_presence(status=discord.Status.online))

            # Schedule next day's reminders
            next_rollover = self.session.get_next_rollover()
            for reminder in self.assistant.reminders.get_reminders_before(next_rollover):
                asyncio.create_task(self.send_reminder(reminder))

        if self.log_channel:
            futures.append(self.log_channel.send(f'Finished rollover to day {date}'))

        if self.chat_channel:
            futures.append(self.update_reminders_message())

        if futures:
            await asyncio.gather(*futures)

    async def setup_hook(self):
        self.rollover_lock = asyncio.Lock()

        rollover_time = self.assistant.rollover.replace(tzinfo=self.assistant.timezone)
        print(f'Date is {self.session.date}, next rollover scheduled at {rollover_time}')
        self.check_rollover.change_interval(time=rollover_time)
        self.check_rollover.start()

        next_rollover = self.session.get_next_rollover()
        for reminder in self.assistant.reminders.get_reminders_before(next_rollover):
            asyncio.create_task(self.send_reminder(reminder))

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
