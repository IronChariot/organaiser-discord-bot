import json
from datetime import date, datetime, time, timedelta, timezone
import discord
from discord.ext import tasks
import asyncio

from .util import split_message
from .reminders import Reminders, Reminder
from .msgtypes import Attachment

# Max chars Discord allows to be sent per message
MESSAGE_LIMIT = 2000

async def send_split_message(channel, message):
    limit = MESSAGE_LIMIT
    if len(message) > limit and '```' in message:
        # Hard case, preserve preformatted blocks across split messages.
        parts = list(split_message(message, limit-12))
        blocks = 0
        for part in parts:
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

            await channel.send(part)
    else:
        # Simple case
        for part in split_message(message, limit):
            await channel.send(part)


class Bot(discord.Client):
    def __init__(self, session):
        self.session = session
        self.assistant = session.assistant
        self.chat_channel = None
        self.log_channel = None
        self.diary_channel = None
        self.query_channel = None
        self.current_checkin_task = None
        self.startup_checkin_deadline = None

        response = session.get_last_assistant_response()
        if response and 'prompt_after' in response:
            self.startup_checkin_deadline = session.last_activity + \
                timedelta(minutes=response['prompt_after'])

        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents)

    async def respond(self, content, message=None):
        """Respond to input from the user or the system."""

        timestamp = datetime.now(tz=self.assistant.timezone).strftime("%H:%M:%S")
        content = f'[{timestamp}] {content}'

        attachments = [Attachment(attach.url, attach.content_type) for attach in message.attachments] if message else []
        message_id = message.id if message else None
        response = await self.session.chat(content, attachments=attachments, message_id=message_id)
        response_time = datetime.now(tz=timezone.utc)

        futures = []

        if self.chat_channel:
            if 'chat' in response:
                futures.append(send_split_message(self.chat_channel, response['chat']))

            if 'react' in response:
                reactions = response['react']
                # Check if reactions is not None
                if reactions is not None:
                    reactions = reactions.encode('utf-16', 'surrogatepass').decode('utf-16')
                    if message:
                        for react in reactions:
                            if not react.isspace():
                                futures.append(message.add_reaction(react))
                    elif 'chat' not in response:
                        futures.append(self.chat_channel.send(reactions))

        if self.log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            futures.append(send_split_message(self.log_channel, f'> {quoted_message}\n\n```json\n{json.dumps(response, indent=4)}\n```'))

        if 'prompt_after' in response:
            # If there's an existing checkin task, cancel it
            if self.current_checkin_task and not self.current_checkin_task.done():
                self.current_checkin_task.cancel()

            deadline = response_time + timedelta(minutes=response['prompt_after'])
            self.current_checkin_task = asyncio.create_task(self.perform_checkin(deadline))

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

        if 'timed_reminder_time' in response:
            # Set up a timed reminder
            # Parse the time as a datetime:
            reminder_time = datetime.fromisoformat(response['timed_reminder_time'])
            # Make the reminder time local to UTC
            reminder_time = reminder_time.astimezone(timezone.utc)
            reminder_text = response['timed_reminder_text']
            repeat = response.get('timed_reminder_repeat', False)
            repeat_interval = response.get('timed_reminder_repeat_interval', 'day')

            self.assistant.reminders.add_reminder(Reminder(reminder_time, reminder_text, repeat, repeat_interval))

            # If the reminder is for later today, set up a task to send it
            if reminder_time.date() == date.today():
                asyncio.create_task(self.send_reminder(Reminder(reminder_time, reminder_text, repeat, repeat_interval)))

        if futures:
            await asyncio.gather(*futures)

    async def update_todo(self, todo_action, todo_text_list):
        # Get the list of existing todos, if any:
        with open('todo.json', 'r') as fh:
            todos_json = fh.read()
        todos_list = json.loads(todos_json)

        if todo_action == 'add':
            for todo_text in todo_text_list:
                todos_list.append(todo_text)
            # Write the dict back to file
            with open('todo.json', 'w') as fh:
                fh.write(json.dumps(todos_list))
        elif todo_action == 'remove':
            for todo_text in todo_text_list:
                if todo_text in todos_list:
                    todos_list.remove(todo_text)
                # Also correctly deal with it if the bot put the whole '- ' at the front of the todo text
                elif todo_text.startswith('- ') and todo_text[2:] in todos_list:
                    todos_list.remove(todo_text[2:])
            # Write the dict back to file
            with open('todo.json', 'w') as fh:
                fh.write(json.dumps(todos_list))

    async def update_long_term_goals(self, long_term_goal_action, long_term_goal_text_list):
        # Get the list of existing long term goals, if any:
        with open('long_term_goals.json', 'r') as fh:
            long_term_goals_json = fh.read()
        long_term_goals_dict = json.loads(long_term_goals_json)

        if long_term_goal_action == 'add':
            for long_term_goal_text in long_term_goal_text_list:
                long_term_goals_dict[long_term_goal_text] = True
            # Write the dict back to file
            with open('long_term_goals.json', 'w') as fh:
                fh.write(json.dumps(long_term_goals_dict))
        elif long_term_goal_action == 'remove':
            for long_term_goal_text in long_term_goal_text_list:
                if long_term_goal_text in long_term_goals_dict:
                    del long_term_goals_dict[long_term_goal_text]
            # Write the dict back to file
            with open('long_term_goals.json', 'w') as fh:
                fh.write(json.dumps(long_term_goals_dict))

    async def perform_checkin(self, deadline):
        try:
            begin_time = datetime.now(tz=timezone.utc)
            seconds_left = 0
            if deadline > begin_time:
                seconds_left = (deadline - begin_time).total_seconds()
                await asyncio.sleep(seconds_left)

            # Unassign this otherwise respond() will cancel us
            self.current_checkin_task = None

            minutes = int(round(seconds_left / 60))
            print(f'Checking in due to user inactivity for {minutes} minutes.')
            if minutes == 0:
                await self.respond(f'(Immediately thereafter…)')
            elif minutes == 1:
                await self.respond(f'(One minute later…)')
            else:
                await self.respond(f'({minutes} minutes later…)')
        except asyncio.CancelledError:
            print('Checkin task was cancelled')
            return

    async def send_reminder(self, reminder: Reminder):
        begin_time = datetime.now(tz=timezone.utc)
        if reminder.time > begin_time:
            await asyncio.sleep((reminder.time - begin_time).total_seconds())

        print(f'Setting reminder for {reminder.time}: {reminder.text}')
        await self.respond(f'SYSTEM: Reminder from your past self now going off: {reminder.text}')

        # If the reminder repeats, set up a new reminder for the next time (after 1 interval):
        if reminder.repeat:
            if reminder.repeat_interval == 'day':
                new_time = reminder.time + timedelta(days=1)
            elif reminder.repeat_interval == 'week':
                new_time = reminder.time + timedelta(weeks=1)
            elif reminder.repeat_interval == 'month':
                new_time = reminder.time + timedelta(months=1)
            elif reminder.repeat_interval == 'year':
                new_time = reminder.time + timedelta(years=1)
            self.assistant.reminders.add_reminder(Reminder(new_time, reminder.text, repeat=True, repeat_interval=reminder.repeat_interval))
        else:
            # Remove the reminder from the list
            self.assistant.reminders.remove_reminder(reminder.time, reminder.text)

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')

        config = self.assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        query_channel_name = config.get('query_channel')
        self.chat_channel = discord.utils.get(self.get_all_channels(), name=chat_channel_name) if chat_channel_name else None
        self.log_channel = discord.utils.get(self.get_all_channels(), name=log_channel_name) if log_channel_name else None
        self.diary_channel = discord.utils.get(self.get_all_channels(), name=diary_channel_name) if diary_channel_name else None
        self.query_channel = discord.utils.get(self.get_all_channels(), name=query_channel_name) if query_channel_name else None

        if self.startup_checkin_deadline:
            print("Next check-in at", self.startup_checkin_deadline)
            self.current_checkin_task = asyncio.create_task(self.perform_checkin(self.startup_checkin_deadline))
            self.startup_checkin_deadline = None

    async def on_message(self, message):
        if message.author == self.user:
            return

        if message.channel == self.chat_channel:
            async with message.channel.typing():
                await self.respond(f'{message.author.display_name}: {message.content}', message)

        if message.channel == self.query_channel:
            async with message.channel.typing():
                attachments = [Attachment(attach.url, attach.content_type) for attach in message.attachments]

                reply = await self.session.isolated_query(message.content, attachments=attachments)

                if reply.startswith('{'):
                    reply = f'```json\n{reply}\n```'

                await send_split_message(self.query_channel, reply)

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
                futures.append(send_split_message(self.log_channel, self.session.initial_system_prompt))

            if not old_session.diary_path.exists():
                entry = await old_session.write_diary_entry()

                if self.diary_channel:
                    futures.append(send_split_message(self.diary_channel, entry))

            futures.append(self.change_presence(status=discord.Status.online))

        if self.log_channel:
            futures.append(self.log_channel.send(f'Finished rollover to day {date}'))

        if futures:
            await asyncio.gather(*futures)

    async def setup_hook(self):
        self.rollover_lock = asyncio.Lock()

        rollover_time = self.assistant.rollover.replace(tzinfo=self.assistant.timezone)
        print(f'Date is {self.session.date}, next rollover scheduled at {rollover_time}')
        self.check_rollover.change_interval(time=rollover_time)
        self.check_rollover.start()

        for reminder in self.assistant.reminders.todays_reminders():
            asyncio.create_task(self.send_reminder(reminder))
