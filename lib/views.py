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
