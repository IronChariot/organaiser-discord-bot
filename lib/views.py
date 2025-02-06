import discord
import json
import asyncio

from .util import split_message


class RetryButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.retry = False

    @discord.ui.button(label='Retry', style=discord.ButtonStyle.primary)
    async def btn_retry(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.retry = True
        self.stop()

    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()


class CloseButton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.message = None

    @discord.ui.button(label='Close', style=discord.ButtonStyle.secondary)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.message.delete()
        self.stop()


class EditSystemPromptModal(discord.ui.Modal, title='Edit System Prompt for Today'):
    def __init__(self, session):
        self.inputs = []
        super().__init__()

        for part in split_message(session.system_message.content, 4000):
            input = discord.ui.TextInput(
                label='Prompt',
                style=discord.TextStyle.paragraph,
                placeholder='',
                required=True,
                min_length=0,
            )
            input.default = part
            self.inputs.append(input)
            self.add_item(input)

        self.session = session

    async def on_submit(self, interaction: discord.Interaction):
        new_prompt = '\n'.join(input.value for input in self.inputs).strip()

        async with self.session.context_lock:
            self.session.system_message.content = new_prompt
            self.session._rewrite_message_file()

        await interaction.response.send_message(f'Updated system prompt.', ephemeral=True, silent=True)
