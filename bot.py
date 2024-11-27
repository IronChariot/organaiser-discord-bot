# bot.py
import os
import discord
from discord.ext import commands
from discord.ext import tasks
import ollama
import json

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = os.getenv('DISCORD_GUILD')

SYSTEM_PROMPT = "You are a discord bot named Naiser, in a conversation with two users in a discord channel, rdb and Merastius. Even though the user messages are prepended by their username in brackets, you only need to output your message in text without prepending anything."

# Some user id to user name mapping
user_id_to_name = {}

# Define the intents your bot needs
intents = discord.Intents.default()
# Print all the intents in a human readable way:
# for intent in intents:
#     print(intent)

intents.message_content = True

client = discord.Client(intents=intents)

# Make it print out all the guilds its connected to
@client.event
async def on_ready():
    print(f'{client.user} has connected to Discord!')
    # send_time_message.start()

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    
    # If the message is in the #organaiser channel, we respond with hi, we @ the user, and we quote the current timestamp
    if message.channel.name == 'organaiser':
        messages = [mess async for mess in message.channel.history(limit=50, oldest_first=True)]
        message_history = [{"role": "system", "content": SYSTEM_PROMPT}]
        for mess in messages:
            corrected_content = mess.content
            for user_id, user_name in user_id_to_name.items():
                corrected_content = mess.content.replace(f'<@{user_id}>', f'@{user_name}')
            if mess.author == client.user:
                message_history.append({"role": "assistant", "content": corrected_content})
            else:
                message_history.append({"role": "user", "content": f"[{mess.author.global_name}] {corrected_content}"})
        response = ollama.chat_completion('llama31_q5', message_history)
        # Debug the response structure
        response_json = json.loads(response._content)
        response_content = response_json["message"]["content"]
        await message.channel.send(response_content)

@client.event
async def on_error(event, *args, **kwargs):
    with open('err.log', 'a') as f:
        if event == 'on_message':
            f.write(f'Unhandled message: {args[0]}')
        else:
            raise

# Every 10 minutes, send a message out to give the current time in the #organaiser channel
# @tasks.loop(minutes=10)
# async def send_time_message():
#     channel = discord.utils.get(client.get_all_channels(), name='organaiser')
#     if channel:
#         await channel.send(f'Current time: {discord.utils.utcnow()}')

client.run(TOKEN)