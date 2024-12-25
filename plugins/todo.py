import discord
import json
import asyncio

from lib.plugin import Plugin, hook, action, pinned_message, system_prompt
from lib.msgtypes import UserMessage


class TodoListView(discord.ui.View):
    """Buttons at the bottom of the pinned todo list message."""

    def __init__(self, plugin, bot):
        super().__init__(timeout=None)
        self.plugin = plugin
        self.bot = bot

    @discord.ui.button(label='Edit', style=discord.ButtonStyle.secondary, emoji='✏️', custom_id='todo_list:edit')
    async def btn_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditTodosModal(self.plugin, self.bot)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label='Add', style=discord.ButtonStyle.secondary, emoji='➕', custom_id='todo_list:add')
    async def btn_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddTodoModal(self.plugin)
        await interaction.response.send_modal(modal)


class TodoPlugin(Plugin):
    """Allows the assistant to keep track of a list of TODO items."""

    @system_prompt
    def on_static_system_prompt(self, session):
        return (
            'It may optionally contain a key "todo_action" and "todo_text", '
            'which will be used to add or remove an item from today\'s todo '
            'list. The todo_text be be a string that is the exact text of the '
            'todo item, or a list of such strings if you wish to add or remove '
            'multiple items at once. The todo_action should either be "add" or '
            '"remove".'
        )

    @system_prompt(dynamic=True)
    def on_dynamic_system_prompt(self, session):
        with self.assistant.open_memory_file('todo.json') as fh:
            todo_json = fh.read()
        todo_dict = json.loads(todo_json)
        todo_string = "# Current TODO List:\n"
        for todo_text in todo_dict:
            todo_string += f"- {todo_text}\n"
        return todo_string.rstrip()

    @pinned_message(header='## Current TODOs', discord_view=TodoListView)
    async def todo_list_message(self):
        with self.assistant.open_memory_file('todo.json', default='[]') as fh:
            todos_json = fh.read().strip()

        todos_list = json.loads(todos_json) if todos_json else []
        return ' - ' + '\n - '.join(todos_list)

    @action('todo_action', 'todo_text')
    async def on_todo_action(self, response, *, todo_action=None, todo_text=None):
        if not todo_text or todo_action not in ('add', 'remove'):
            return

        # Check if todo_text is a string, or a list of strings:
        todo_text_list = [todo_text] if isinstance(todo_text, str) else todo_text
        await self.update_todo(todo_action, todo_text_list)

        plural_s = 's' if len(todo_text_list) != 1 else ''
        if todo_action == 'add':
            return f'Added {len(todo_text_list)} todo item{plural_s}'

        elif todo_action == 'remove':
            return f'Removed {len(todo_text_list)} todo item{plural_s}'

    async def update_todo(self, todo_action, todo_text_list):
        # Get the list of existing todos, if any:
        with self.assistant.open_memory_file('todo.json', default='[]') as fh:
            todos_json = fh.read().strip()

        todos_list = json.loads(todos_json) if todos_json else []

        if todo_action == 'add':
            for todo_text in todo_text_list:
                if todo_text not in todos_list:
                    todos_list.append(todo_text)

        elif todo_action == 'remove':
            for todo_text in todo_text_list:
                if todo_text in todos_list:
                    todos_list.remove(todo_text)
                # Also correctly deal with it if the bot put the whole '- ' at the front of the todo text
                elif todo_text.startswith('- ') and todo_text[2:] in todos_list:
                    todos_list.remove(todo_text[2:])

        else:
            return

        # Write the dict back to file
        with self.assistant.open_memory_file('todo.json', 'w') as fh:
            json.dump(todos_list, fh)

        # Update the pinned to do message in the background
        asyncio.create_task(self.todo_list_message.update())


class AddTodoModal(discord.ui.Modal, title='Add Todo'):
    text = discord.ui.TextInput(
        label='Text',
        placeholder='Do things with stuff',
        required=True,
    )

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin

    async def on_submit(self, interaction: discord.Interaction):
        text = self.text.value.strip()
        await interaction.response.send_message(f'Added', ephemeral=True, silent=True, delete_after=0.001)
        await self.plugin.update_todo('add', [text])


class EditTodosModal(discord.ui.Modal, title='Edit Todos'):
    items = discord.ui.TextInput(
        label='Items (one per line)',
        style=discord.TextStyle.paragraph,
        placeholder='',
        required=True,
        min_length=0,
    )

    def __init__(self, plugin, bot):
        super().__init__()
        self.plugin = plugin
        self.bot = bot
        with self.bot.assistant.open_memory_file('todo.json', 'r', default='[]') as fh:
            self.items.default = '\n'.join(json.load(fh))

    async def on_submit(self, interaction: discord.Interaction):
        value = self.items.value.strip().split('\n')

        with self.bot.assistant.open_memory_file('todo.json', 'w', default='[]') as fh:
            json.dump(value, fh, indent=4)

        await interaction.response.send_message(f'Updated todo.json', ephemeral=True, silent=True, delete_after=0.001)

        asyncio.create_task(self.plugin.todo_list_message.update())
