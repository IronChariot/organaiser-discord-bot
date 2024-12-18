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


class EditMemoryFileModal(discord.ui.Modal, title='Edit'):
    content = discord.ui.TextInput(
        label='Content',
        style=discord.TextStyle.paragraph,
        placeholder='[]',
        required=True,
        min_length=2,
    )

    def __init__(self, bot, filename):
        super().__init__()
        self.bot = bot
        self.filename = filename
        with self.bot.assistant.open_memory_file(filename, 'r', default='[]') as fh:
            self.content.default = fh.read()

    async def on_submit(self, interaction: discord.Interaction):
        value = self.content.value.strip()

        # Make sure it parses before we try to write it
        if self.filename.endswith('.json'):
            json.loads(value)

        with self.bot.assistant.open_memory_file(self.filename, 'w', default='[]') as fh:
            fh.write(value + '\n')

        if self.filename == 'reminders.json':
            self.bot.assistant.reminders.reload()

        await interaction.response.send_message(f'Updated {self.filename}', ephemeral=True, silent=True, delete_after=0.001)

        if self.filename == 'reminders.json':
            asyncio.create_task(self.bot.update_reminders_message())
        elif self.filename == 'todo.json':
            asyncio.create_task(self.bot.update_todo_message())


class ReminderListView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Edit', style=discord.ButtonStyle.secondary, emoji='✏️', custom_id='reminder_list:edit')
    async def btn_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditMemoryFileModal(self.bot, 'reminders.json')
        await interaction.response.send_modal(modal)


class AddTodoModal(discord.ui.Modal, title='Add Todo'):
    text = discord.ui.TextInput(
        label='Text',
        placeholder='Do things with stuff',
        required=True,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        text = self.text.value.strip()
        await interaction.response.send_message(f'Added', ephemeral=True, silent=True, delete_after=0.001)
        await self.bot.update_todo('add', [text])


class TodoListView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label='Edit', style=discord.ButtonStyle.secondary, emoji='✏️', custom_id='todo_list:edit')
    async def btn_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditMemoryFileModal(self.bot, 'todo.json')
        await interaction.response.send_modal(modal)

    @discord.ui.button(label='Add', style=discord.ButtonStyle.secondary, emoji='➕', custom_id='todo_list:add')
    async def btn_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddTodoModal(self.bot)
        await interaction.response.send_modal(modal)
