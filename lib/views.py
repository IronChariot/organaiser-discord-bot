import discord
import json
import asyncio


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
    prompt = discord.ui.TextInput(
        label='Prompt',
        style=discord.TextStyle.paragraph,
        placeholder='',
        required=True,
        min_length=1,
    )

    def __init__(self, session):
        super().__init__()
        self.session = session
        self.prompt.default = session.message_history[0].content

    async def on_submit(self, interaction: discord.Interaction):
        async with self.session.context_lock:
            self.session.message_history[0].content = self.prompt.value
            self.session._rewrite_message_file()

        await interaction.response.send_message(f'Updated system prompt.', ephemeral=True, silent=True)
