import sys, os
import json
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
import argparse
import discord
from discord.ext import tasks
import asyncio

from lib.assistant import Assistant
from lib.util import split_message
from lib.reminders import Reminders, Reminder

def run_local(session):
    response = session.get_last_assistant_response()
    if response:
        print(response.get("chat") or response.get("react") or "(no response)")

    while True:
        message = input("> ")
        response = asyncio.run(session.chat(message))
        print("\n" + (response.get("chat") or response.get("react") or "(no response)"))


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


def run_discord_bot(assistant, session, token, self_prompt_interval):
    chat_channel = None
    log_channel = None
    diary_channel = None
    query_channel = None
    current_checkin_task = None
    startup_checkin_deadline = None

    self_prompt = open('self_prompt.txt', 'r').read()

    response = session.get_last_assistant_response()
    if response and 'prompt_after' in response:
        startup_checkin_deadline = session.last_activity + timedelta(minutes=response['prompt_after'])

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    async def respond(content, message=None):
        """Respond to input from the user or the system."""

        nonlocal chat_channel, log_channel, current_checkin_task

        timestamp = datetime.now(tz=assistant.timezone).strftime("%H:%M:%S")
        content = f'[{timestamp}] {content}'

        response = await session.chat(content)
        response_time = datetime.now(tz=timezone.utc)

        futures = []

        if chat_channel:
            if 'chat' in response:
                futures.append(send_split_message(chat_channel, response['chat']))

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
                        futures.append(chat_channel.send(reactions))

        if log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            futures.append(send_split_message(log_channel, f'> {quoted_message}\n\n```json\n{json.dumps(response, indent=4)}\n```'))

        if 'prompt_after' in response:
            # If there's an existing checkin task, cancel it
            try:
                if current_checkin_task and not current_checkin_task.done():
                    current_checkin_task.cancel()
            except NameError:
                # current_checkin_task doesn't exist yet
                pass

            deadline = response_time + timedelta(minutes=response['prompt_after'])
            current_checkin_task = asyncio.create_task(perform_checkin(deadline))

        # Take care of the todo list and long term goals list
        if 'todo_action' in response:
            todo_action = response['todo_action']
            todo_text = response['todo_text']
            # Check if todo_text is a string, or a list of strings:
            todo_text_list = [todo_text] if isinstance(todo_text, str) else todo_text
            futures.append(update_todo(todo_action, todo_text_list))

        if 'long_term_goals_action' in response:
            long_term_goal_action = response['long_term_goals_action']
            long_term_goal_text = response['long_term_goals_text']
            # Check if long_term_goal_text is a string, or a list of strings:
            long_term_goal_text_list = [long_term_goal_text] if isinstance(long_term_goal_text, str) else long_term_goal_text
            futures.append(update_long_term_goals(long_term_goal_action, long_term_goal_text_list))

        if 'timed_reminder_time' in response:
            # Set up a timed reminder
            # Parse the time as a datetime:
            reminder_time = datetime.fromisoformat(response['timed_reminder_time'])
            # Make the reminder time local to UTC
            reminder_time = reminder_time.astimezone(timezone.utc)
            reminder_text = response['timed_reminder_text']
            repeat = response.get('timed_reminder_repeat', False)
            repeat_interval = response.get('timed_reminder_repeat_interval', 'day')

            assistant.reminders.add_reminder(Reminder(reminder_time, reminder_text, repeat, repeat_interval))

            # If the reminder is for later today, set up a task to send it
            if reminder_time.date() == date.today():
                asyncio.create_task(send_reminder(Reminder(reminder_time, reminder_text, repeat, repeat_interval)))

        if futures:
            await asyncio.gather(*futures)

    async def update_todo(todo_action, todo_text_list):
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

    async def update_long_term_goals(long_term_goal_action, long_term_goal_text_list):
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

    async def perform_checkin(deadline):
        nonlocal current_checkin_task

        try:
            begin_time = datetime.now(tz=timezone.utc)
            seconds_left = 0
            if deadline > begin_time:
                seconds_left = (deadline - begin_time).total_seconds()
                await asyncio.sleep(seconds_left)

            # Unassign this otherwise respond() will cancel us
            current_checkin_task = None

            minutes = int(round(seconds_left / 60))
            print(f'Checking in due to user inactivity for {minutes} minutes.')
            if minutes == 0:
                await respond(f'(Immediately thereafter…)')
            elif minutes == 1:
                await respond(f'(One minute later…)')
            else:
                await respond(f'({minutes} minutes later…)')
        except asyncio.CancelledError:
            print('Checkin task was cancelled')
            return
        
    async def send_reminder(reminder: Reminder):

        begin_time = datetime.now(tz=timezone.utc)
        if reminder.time > begin_time:
            await asyncio.sleep((reminder.time - begin_time).total_seconds())

        print(f'Setting reminder for {reminder.time}: {reminder.text}')
        await respond(f'SYSTEM: Reminder from your past self now going off: {reminder.text}')

        # If the reminder repeats, set up a new reminder for the next time (after 1 interval):
        if reminder.repeat:
            if reminder.repeat_interval == 'day':
                new_time = reminder.time + timedelta(days=1)
            elif reminder.repeat_interval == 'week':
                new_time = reminder.time + timedelta(weeks=1)
            assistant.reminders.add_reminder(Reminder(new_time, reminder.text, repeat=True, repeat_interval=reminder.repeat_interval))
        else:
            # Remove the reminder from the list
            assistant.reminders.remove_reminder(reminder.time, reminder.text)

    @client.event
    async def on_ready():
        print(f'{client.user} has connected to Discord!')

        nonlocal chat_channel, log_channel, diary_channel, query_channel
        nonlocal current_checkin_task, startup_checkin_deadline

        config = assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        query_channel_name = config.get('query_channel')
        chat_channel = discord.utils.get(client.get_all_channels(), name=chat_channel_name) if chat_channel_name else None
        log_channel = discord.utils.get(client.get_all_channels(), name=log_channel_name) if log_channel_name else None
        diary_channel = discord.utils.get(client.get_all_channels(), name=diary_channel_name) if diary_channel_name else None
        query_channel = discord.utils.get(client.get_all_channels(), name=query_channel_name) if query_channel_name else None

        if startup_checkin_deadline:
            print("Next check-in at", startup_checkin_deadline)
            current_checkin_task = asyncio.create_task(perform_checkin(startup_checkin_deadline))
            startup_checkin_deadline = None

        if self_prompt_interval:
            regular_self_prompt.start()

        for reminder in assistant.reminders.todays_reminders():
            asyncio.create_task(send_reminder(reminder))

    @client.event
    async def on_message(message):
        nonlocal chat_channel, query_channel

        if message.author == client.user:
            return

        if message.channel == chat_channel:
            async with message.channel.typing():
                await respond(f'{message.author.display_name}: {message.content}', message)

        if message.channel == query_channel:
            async with message.channel.typing():
                reply = await session.isolated_query(message.content)

                if reply.startswith('{'):
                    for part in split_message(reply, MESSAGE_LIMIT - 12):
                        await query_channel.send(f'```json\n{part}\n```')
                else:
                    for part in split_message(reply, MESSAGE_LIMIT):
                        await query_channel.send(part)

    # Every self_prompt_interval minutes, prompt the model with the timestamp, asking for a response if one is appropriate
    @tasks.loop(minutes=self_prompt_interval)
    async def regular_self_prompt():
        await respond(self_prompt)

    client.run(token)


@contextmanager
def pidfile(path):
    with open(path, 'w') as pidfile:
        pidfile.write(str(os.getpid()))

    try:
        yield
    finally:
        if os.path.isfile(path):
            os.unlink(path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", dest="daemonize", action="store_true", help="daemonize the process (runs in the background)")
    parser.add_argument("--date", help="make or continue the session for a give date (in YYYY-MM-DD format)")
    parser.add_argument("assistant", help="name of the .toml file of the assistant to run, without .toml extension", nargs='?', default='naiser')
    parser.add_argument("interval", type=int, nargs="?", help="self prompt interval")
    args = parser.parse_args()

    assistant = Assistant.load(args.assistant)

    # Make sure there isn't already an instance running of this assistant
    pidfile_path = os.path.abspath(f"{assistant.id}.pid")
    if os.path.isfile(pidfile_path):
        with open(pidfile_path) as pidf:
            pid = pidf.read()
        print(f"Assistant {args.assistant} is already running (pid={pid}). Delete {pidfile_path} if this is not the case")
        sys.exit(1)

    with pidfile(pidfile_path):
        session = asyncio.run(assistant.load_session(date.fromisoformat(args.date) if args.date else assistant.get_today()))

    token = os.environ.get('DISCORD_TOKEN')
    self_prompt_interval = args.interval

    if token and assistant.discord_config:
        if args.daemonize:
            import daemon
            print("Spawning daemon.")

            with daemon.DaemonContext():
                with pidfile(pidfile_path):
                    run_discord_bot(assistant, session, token=token, self_prompt_interval=self_prompt_interval)
        else:
            with pidfile(pidfile_path):
                run_discord_bot(assistant, session, token=token, self_prompt_interval=self_prompt_interval)

    elif args.daemonize:
        print("No Discord configuration; cannot run as daemon.")
        sys.exit(1)
    else:
        print("No Discord configuration; running locally.")
        run_local(session)


if __name__ == '__main__':
    main()
