# bot.py
import os
import sys
import discord
from discord.ext import commands
from discord.ext import tasks
from lib.models import Model, OllamaModel, AnthropicModel, OpenAIModel
from utilities import setup_logging
import json

main_model_name = "gpt-4o"
main_model = None

system_prompt = "You are a discord bot named Naiser, in a conversation with two users in a discord channel, rdb and Merastius. Even though the user messages are prepended by their username in brackets, you only need to output your message in text without prepending anything."

main_logger, model_logger = setup_logging()

# Initialize models
try:
    if main_model_name in ["opus", "sonnet", "haiku"]:
        main_model = AnthropicModel(main_model_name, system_prompt=system_prompt, logger=model_logger)
        main_logger.info(f"Using Anthropic model: {main_model_name}")
    elif main_model_name in ["gpt-4o", "gpt-4o-mini", "gpt-4", "gpt-3.5"]:
        main_model = OpenAIModel(main_model_name, system_prompt=system_prompt, logger=model_logger)
        main_logger.info(f"Using OpenAI model: {main_model_name}")
    else:
        main_model = OllamaModel(main_model_name, system_prompt=system_prompt, logger=model_logger)
        main_logger.info(f"Using Ollama model: {main_model_name}")
except ValueError as e:
    main_logger.error(f"Error initializing models: {e}")
    sys.exit(1)

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD = os.getenv('DISCORD_GUILD')

# Get user id to user name mapping from userids.json
user_id_to_name = {}
with open('userids.json', 'r') as f:
    user_id_to_name = json.load(f)

# Log the system prompt
main_logger.info(f"System prompt: {system_prompt}")

# Define the intents your bot needs
intents = discord.Intents.default()
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
        # Only add the system prompt as a message if the main model is an ollama model or an openai model
        if (main_model_name not in ["opus", "sonnet", "haiku"]):
            message_history = [{"role": "system", "content": system_prompt}]
        else:
            message_history = []
        # Populate the message history with all but the last message
        for mess in messages[:-1]:
            corrected_content = mess.content
            for user_id, user_name in user_id_to_name.items():
                corrected_content = corrected_content.replace(f'<@{user_id}>', f'@{user_name}')
            if mess.author == client.user:
                message_history.append({"role": "assistant", "content": corrected_content})
            else:
                message_history.append({"role": "user", "content": f"[{mess.author.global_name}] {corrected_content}"})
        main_model.messages = message_history
        response = main_model.query(messages[-1].content, lambda response: response != '')
        await message.channel.send(response)

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