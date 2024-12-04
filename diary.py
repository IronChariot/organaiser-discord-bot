import sys, os
import json
from datetime import date, datetime, timedelta, timezone
import discord
import asyncio
from io import StringIO

from lib.assistant import Assistant


def run_discord_bot(session, config, token):
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f'{client.user} has connected to Discord!')

        diary_channel_name = config.get('diary_channel')
        diary_channel = discord.utils.get(client.get_all_channels(), name=diary_channel_name) if diary_channel_name else None

        if diary_channel:
            entry = session.write_diary_entry()
            if len(entry) > 2000:
                buf = StringIO(entry)
                await diary_channel.send(file=discord.File(buf, filename="diary.txt"))
            else:
                await diary_channel.send(entry)
            sys.exit(0)
        else:
            print('No diary channel configured or found.')
            sys.exit(1)

    client.run(token)


if __name__ == '__main__':
    name = sys.argv[1]
    assistant = Assistant.load(sys.argv[1])
    session = assistant.load_session(date.fromisoformat(sys.argv[2]))

    token = os.environ.get('DISCORD_TOKEN')

    if token and assistant.discord_config:
        run_discord_bot(session, assistant.discord_config, token=token)
    else:
        print("No Discord configuration; running locally.")
        run_local(session)
