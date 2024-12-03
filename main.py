import sys, os
import json
from datetime import date, datetime, timedelta, timezone
import discord
from discord.ext import tasks
import asyncio

from assistant import Assistant


def run_local(session):
    response = session.get_last_assistant_response()
    if response:
        print(response.get("chat") or response.get("react") or "(no response)")

    while True:
        message = input("> ")
        response = session.chat(message)
        print("\n" + response.get("chat") or response.get("react") or "(no response)")


def run_discord_bot(assistant, session, token, self_prompt_interval):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    self_prompt = None
    chat_channel = None
    log_channel = None
    current_checkin_task = None
    diary_channel = None

    async def respond(content, message=None):
        """Respond to input from the user or the system."""

        global chat_channel, log_channel, current_checkin_task

        timestamp = datetime.now(tz=assistant.timezone).strftime("%H:%M:%S")
        content = f'[{timestamp}] {content}'

        response = session.chat(content)
        response_time = datetime.now(tz=timezone.utc)

        if chat_channel:
            if 'chat' in response:
                await chat_channel.send(response['chat'])

            if 'react' in response:
                if message:
                    await message.add_reaction(response['react'])
                elif 'chat' not in response:
                    await chat_channel.send(response['chat'])

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

        global chat_channel, log_channel, self_prompt_interval, self_prompt

        config = assistant.discord_config
        chat_channel_name = config.get('chat_channel')
        log_channel_name = config.get('log_channel')
        diary_channel_name = config.get('diary_channel')
        chat_channel = discord.utils.get(client.get_all_channels(), name=chat_channel_name) if chat_channel_name else None
        log_channel = discord.utils.get(client.get_all_channels(), name=log_channel_name) if log_channel_name else None
        diary_channel = discord.utils.get(client.get_all_channels(), name=diary_channel_name) if diary_channel_name else None
        self_prompt = open('self_prompt.txt', 'r').read()

        response = session.get_last_assistant_response()
        if response and 'prompt_after' in response:
            deadline = session.last_activity + timedelta(minutes=response['prompt_after'])
            print("Next check-in at", deadline)
            asyncio.create_task(perform_checkin(deadline))

        if self_prompt_interval:
            regular_self_prompt.start()

    @client.event
    async def on_message(message):
        global chat_channel

        if message.author == client.user:
            return

        if message.channel == chat_channel:
            await respond(f'{message.author.display_name}: {message.content}', message)

    # Every self_prompt_interval minutes, prompt the model with the timestamp, asking for a response if one is appropriate
    @tasks.loop(minutes=self_prompt_interval)
    async def regular_self_prompt():
        global chat_channel, log_channel, self_prompt
        
        await respond(self_prompt)

    client.run(token)


if __name__ == '__main__':
    name = sys.argv[1]
    assistant = Assistant.load(sys.argv[1])
    session = assistant.load_session(date.today())

    token = os.environ.get('DISCORD_TOKEN')

    self_prompt_interval = None
    if len(sys.argv) > 2:
        self_prompt_interval = int(sys.argv[2])

    if token and assistant.discord_config:
        run_discord_bot(assistant, session, token=token, self_prompt_interval=self_prompt_interval)
    else:
        print("No Discord configuration; running locally.")
        run_local(session)
