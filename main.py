import sys, os
import json
from datetime import date, datetime, timedelta, timezone
from contextlib import contextmanager
import argparse
import discord
from discord.ext import tasks
import asyncio

from lib.assistant import Assistant


def run_local(session):
    response = session.get_last_assistant_response()
    if response:
        print(response.get("chat") or response.get("react") or "(no response)")

    while True:
        message = input("> ")
        response = session.chat(message)
        print("\n" + (response.get("chat") or response.get("react") or "(no response)"))


def run_discord_bot(assistant, session, token, self_prompt_interval):
    chat_channel = None
    log_channel = None
    diary_channel = None
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

        response = session.chat(content)
        response_time = datetime.now(tz=timezone.utc)

        if chat_channel:
            if 'chat' in response:
                await chat_channel.send(response['chat'])

            if 'react' in response:
                reactions = response['react']
                reactions = reactions.encode('utf-16', 'surrogatepass').decode('utf-16')
                if message:
                    for react in reactions:
                        await message.add_reaction(react)
                elif 'chat' not in response:
                    await chat_channel.send(reactions)

        if log_channel:
            quoted_message = '\n> '.join(content.split('\n'))
            await log_channel.send(f'> {quoted_message}\n```json\n{json.dumps(response, indent=4)}\n```')

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


    async def perform_checkin(deadline):
        try:
            begin_time = datetime.now(tz=timezone.utc)
            if deadline > begin_time:
                await asyncio.sleep((deadline - begin_time).total_seconds())

            # Cancel if activity occurred in the meantime
            print(f'Checking in due to user inactivity.')
            await respond(f'SYSTEM: No response within given period.')
        except asyncio.CancelledError:
            print('Checkin task was cancelled')
            return

    @client.event
    async def on_ready():
        print(f'{client.user} has connected to Discord!')

        nonlocal chat_channel, log_channel, diary_channel
        nonlocal current_checkin_task, startup_checkin_deadline

        config = assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        chat_channel = discord.utils.get(client.get_all_channels(), name=chat_channel_name) if chat_channel_name else None
        log_channel = discord.utils.get(client.get_all_channels(), name=log_channel_name) if log_channel_name else None
        diary_channel = discord.utils.get(client.get_all_channels(), name=diary_channel_name) if diary_channel_name else None

        if startup_checkin_deadline:
            print("Next check-in at", startup_checkin_deadline)
            current_checkin_task = asyncio.create_task(perform_checkin(startup_checkin_deadline))
            startup_checkin_deadline = None

        if self_prompt_interval:
            regular_self_prompt.start()

    @client.event
    async def on_message(message):
        nonlocal chat_channel

        if message.author == client.user:
            return

        if message.channel == chat_channel:
            await respond(f'{message.author.display_name}: {message.content}', message)

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
    parser.add_argument("assistant", help="name of the .toml file of the assistant to run, without .toml extension")
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

    with pidfile:
        session = assistant.load_session(date.fromisoformat(args.date) if args.date else assistant.get_today())

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
