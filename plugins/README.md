# Plugins

Most of the extra functionality of the assistant is intended to be provided via
plugin modules.

## Writing Plugins

Writing a plugin involves creating a class and inheriting it from Plugin.
The plugin class may decorate its methods using the `@hook` or the `@action`
decorators to provide specific functionality.

The Plugin class itself provides a `self.assistant` property containing the
Assistant instance.

### System Prompt

To add a bit to the system prompt, declare a non-async method with
`@system_prompt`.  It will be called at the beginning of a session (and passed
the Session object as argument) to generate the system prompt for that session.
You may tag any number of methods with `@system_prompt`.

To add a bit that needs to be regenerated upon every individual assistant
response use `@system_prompt(dynamic=True)`.

### Hooks

You may declare a hook as a method with any name, decorated with
`@hook('name_of_hook')`.  You may specify multiple hooks with the same name.
Unless specified otherwise, all hooks must be marked `async`.

The following hooks are provided:

#### `@hook('init')`

**Parameters:** none
**Returns:** `None`

Called once, when the plugin is loaded.  Use this instead of an `__init__` to
initialise any variables.

#### `@hook('configure')`

**Parameters:** `config: dict`
**Returns:** `None`

Called once after `load`, and every time after the configuration file has been
changed.  Is passed a dict containing the configuration block for this specific
plugin.

#### `@hook('session_load')`

**Parameters:** `session: Session`
**Returns:** `None`

Called once per session, when the session has been loaded from disk or when a
new session has been created.

#### `@hook('post_session_end')`

**Parameters:** `session: Session`
**Returns:** `None`

Called after a particular session (which will no longer be the active session
at the time of this call) has ended.

### Actions

The assistant is able to trigger actions along with its response by specifying
an extra key in the JSON object it returns.  Plugins can provide these actions
by registering an action handler with the `@action` decorator.

The action will be passed an AssistantResponse object containing the assistant's
response, which may be modified such as attachments added to using `attach()`,
and it will be given keyword arguments with the specified keys.
If it returns something, it should be a string or a list of strings describing
the action(s) taken, which will be added as a status message to the assistant
response.

Make sure to explain how to use the action in a `system_prompt` hook, otherwise
the assistant will not know how to use it!

For example, to take an action upon the `"add_todo"` key being specified:

```python
class TodoPlugin(Plugin):
    @action('add_todo')
    async def on_add_todo(self, response, *, add_todo=None):
        self.todos.append[add_todo]
        return 'Added todo item'

    @hook('system_prompt')
    async def on_system_prompt(self):
        return (
            'To add a todo item, specify the "add_todo" key containing a '
            'string describing the task to be done.'
        )
```

### Discord Commands

To define a Discord slash command, use the `@discord_command` decorator like so:

```python
class TodoPlugin(Plugin):
    @discord_command('clear_todos', description='Clear the todo list')
    async def on_clear_todos_command(self, interaction: discord.Interaction):
        self.todos.clear()
        await interaction.response.send_message('Todo list cleared!', silent=True, ephemeral=True)
```

Consult the `discord.py` documentation on how to use the Interaction object.

### Pinned Messages

Some plugins may wish to maintain a pinned message in the chat channel, for
keeping a record of the current TODO list, for example.  This can be done like
so:

```python
class TodoPlugin(Plugin):
    @pinned_message(header="## Current TODOs")
    def todo_list_message(self):
        return "- Buy milk\n- Pet a cat"
```

At startup, a message starting with the given header will be looked for in the
list of pinned message.  If it does not exist, a new message will be created.

At any point, you may force the pinned message to be updated via
`self.todo_list_message.update()`.

To add a custom Discord view object to the pinned message, it may be passed in
via the `discord_view=` parameter in the decorator.
