import discord
import json
import asyncio
from datetime import datetime, date, time, timedelta, timezone

from lib.plugin import Plugin, hook, action, system_prompt, pinned_message
from lib.msgtypes import UserMessage


class Reminder:
    def __init__(self, time, text, repeat=False, repeat_interval=None):
        self.time = time
        self.text = text
        self.repeat = repeat
        self.repeat_interval = repeat_interval

    @property
    def repeat_delta(self):
        if self.repeat_interval == 'day':
            return timedelta(days=1)
        elif self.repeat_interval == 'week':
            return timedelta(weeks=1)
        elif self.repeat_interval == 'fortnight':
            return timedelta(fortnight=1)
        elif self.repeat_interval == 'month':
            return timedelta(months=1)
        elif self.repeat_interval == 'quarter':
            return timedelta(months=3)
        elif self.repeat_interval == 'year':
            return timedelta(years=1)

    def __eq__(self, other):
        return (self.time == other.time and
                self.text == other.text and
                self.repeat == other.repeat and
                self.repeat_interval == other.repeat_interval)

    def __str__(self):
        if self.repeat:
            return f'{self.time}: {self.text} (Repeats every {self.repeat_interval})'
        return f'{self.time}: {self.text}'

    def __repr__(self):
        return str(self)


class ReminderListView(discord.ui.View):
    """Buttons appearing beneath the pinned reminder list message."""

    def __init__(self, plugin, bot):
        super().__init__(timeout=None)
        self.plugin = plugin

    @discord.ui.button(label='Edit', style=discord.ButtonStyle.secondary, emoji='✏️', custom_id='reminder_list:edit')
    async def btn_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = EditRemindersModal(self.plugin)
        await interaction.response.send_modal(modal)


class RemindersPlugin(Plugin):
    """Allows the assistant to keep track of a list of TODO items."""

    @hook('init')
    async def on_init(self):
        self.load_reminders()

    @hook('session_load')
    async def on_session_load(self, session):
        next_rollover = session.get_next_rollover()
        for reminder in self.reminders:
            if reminder.time < next_rollover:
                print(f'Setting reminder for {reminder.time}: {reminder.text}')
                self.schedule(reminder.time, self.send_reminder(reminder, session))

    @system_prompt
    def static_system_prompt(self, session):
        return (
            'It may optionally contain a key "timed_reminder_time", containing '
            'a datetime in the format "YYYY-MM-DD HH:MM:SS" at which you will '
            'be reminded to take an action by the SYSTEM. Be careful not to '
            'create timed reminders that already exist above. If you do '
            'specify this, you should also specify a key "timed_reminder_text", '
            'containing a string that will be used as the message to remind you '
            'of something. It may optionally contain a key '
            '"timed_reminder_repeat", which will be a boolean value that '
            'indicates whether you should be reminded repeatedly. If you '
            'include this key, you should also include a key '
            '"timed_reminder_repeat_interval", which will be a string value '
            'that indicates how often to repeat the reminder, which should be '
            'one of "day", "week", "fortnight", "month", "quarter" or "year".'
        )

    @system_prompt(dynamic=True)
    def dynamic_system_prompt(self, session):
        result = ['# Currently scheduled reminders:']

        for reminder in sorted(self.reminders, key=lambda r: r.time):
            time = reminder.time.astimezone(self.assistant.timezone)
            formatted_time = time.strftime('%Y-%m-%d %H:%M')

            text = f'{formatted_time}: {reminder.text}'
            if reminder.repeat:
                text += f' (repeats every {reminder.repeat_interval})'
            result.append(text)

        return '\n'.join(result)

    @pinned_message(header='## Current Reminders', discord_view=ReminderListView)
    async def reminder_list_message(self):
        tz = self.assistant.timezone
        result = []
        num_chars = 0
        last_date = None
        for reminder in sorted(self.reminders, key=lambda r: r.time):
            this_date = reminder.time.astimezone(tz).date()
            if this_date != last_date:
                prefix = f'### {this_date.strftime("%A, %d %B")}\n'
                last_date = this_date
            else:
                prefix = ''

            timestamp = int(reminder.time.timestamp())
            line = f'{prefix}- <t:{timestamp}:t> {reminder.text}'

            line_len = len(line) + 1
            if num_chars + line_len <= 2000:
                result.append(line)
                num_chars += line_len
            else:
                break

        return '\n'.join(result)

    @action('timed_reminder_time', 'timed_reminder_text', 'timed_reminder_repeat', 'timed_reminder_repeat_interval')
    async def on_action(self, response, *, timed_reminder_time=None, timed_reminder_text=None,
                        timed_reminder_repeat=False, timed_reminder_repeat_interval='day'):
        if not timed_reminder_time:
            return

        # Set up a timed reminder
        # Parse the time as a datetime:
        reminder_time = datetime.fromisoformat(timed_reminder_time)
        # Make the reminder time local to UTC
        if not reminder_time.tzinfo and self.assistant.timezone is not None:
            reminder_time = reminder_time.replace(tzinfo=self.assistant.timezone)
        reminder_time = reminder_time.astimezone(timezone.utc)
        reminder_text = timed_reminder_text
        repeat = timed_reminder_repeat
        repeat_interval = timed_reminder_repeat_interval

        reminder = Reminder(reminder_time, reminder_text, repeat, repeat_interval)
        await self.add_timed_reminder(reminder, response.session)

        timestamp = int(reminder_time.timestamp())
        rel_date = None
        if reminder_time.date() == date.today():
            rel_date = 'today'
        elif reminder_time.date() == date.today() + timedelta(days=1):
            rel_date = 'tomorrow'
        else:
            rel_date = f'<t:{timestamp}:R>'

        if repeat and repeat_interval == 'day':
            return f'Added daily reminder at <t:{timestamp}:t> starting {rel_date}'
        elif repeat:
            return f'Added {repeat_interval}ly reminder starting {rel_date}'
        elif rel_date == 'today':
            return f'Added reminder going off <t:{timestamp}:R>'
        else:
            return f'Added reminder going off {rel_date} at <t:{timestamp}:t>'

    async def add_timed_reminder(self, reminder: Reminder, session):
        if reminder in self.reminders:
            return

        self.reminders.append(reminder)
        self.save_reminders()

        if reminder.time < session.get_next_rollover():
            print(f'Setting reminder for {reminder.time}: {reminder.text}')
            self.schedule(reminder.time, self.send_reminder(reminder, session))

        # Update the pinned reminders message
        await self.reminder_list_message.update()

    async def send_reminder(self, reminder: Reminder, session):
        # Remove the reminder from the list
        if reminder in self.reminders:
            self.reminders.remove(reminder)

        begin_time = datetime.now(tz=timezone.utc)
        print(f'Responding to reminder for {reminder.time}: {reminder.text}')
        await self.respond(f'SYSTEM: Reminder from your past self now going off: {reminder.text}')

        # If the reminder repeats, set up a new reminder for the next time (after 1 interval):
        if reminder.repeat:
            new_time = reminder.time + reminder.repeat_delta
            while new_time < begin_time:
                new_time += reminder.repeat_delta

            reminder = Reminder(new_time, reminder.text, repeat=True, repeat_interval=reminder.repeat_interval)
            await self.add_timed_reminder(reminder, session)
        else:
            self.save_reminders()

    def load_reminders(self):
        reminders = []
        with self.assistant.open_memory_file('reminders.json', default='[]') as fh:
            reminders = []
            num_dupes = 0
            for reminder_dict in json.load(fh):
                reminder = Reminder(datetime.fromisoformat(reminder_dict['time']), reminder_dict['text'], reminder_dict['repeat'], reminder_dict['repeat_interval'])
                if reminder not in reminders:
                    reminders.append(reminder)
                else:
                    num_dupes += 1

            self.reminders = reminders
            if num_dupes > 0:
                print(f'Removed {num_dupes} duplicate reminders')
                self.save_reminders()

    def save_reminders(self):
        # The time needs to be converted to a string since datetime isn't compatible with JSON
        with self.assistant.open_memory_file('reminders.json', 'w', default='[]') as f:
            f.write('[\n')
            need_comma = False
            for reminder in self.reminders:
                if need_comma:
                    f.write(',\n')
                reminder_dict = {
                    'time': reminder.time.isoformat(),
                    'text': reminder.text,
                    'repeat': reminder.repeat,
                    'repeat_interval': reminder.repeat_interval,
                }
                json.dump(reminder_dict, f)
                need_comma = True

            f.write(']\n')


class EditRemindersModal(discord.ui.Modal, title='Edit Reminders'):
    content = discord.ui.TextInput(
        label='Content',
        style=discord.TextStyle.paragraph,
        placeholder='[]',
        required=True,
        min_length=2,
    )

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.assistant = plugin.assistant
        with self.assistant.open_memory_file('reminders.json', 'r', default='[]') as fh:
            self.content.default = fh.read()

    async def on_submit(self, interaction: discord.Interaction):
        value = self.content.value.strip()

        # Make sure it parses before we try to write it
        json.loads(value)

        with self.assistant.open_memory_file('reminders.json', 'w', default='[]') as fh:
            fh.write(value + '\n')

        self.plugin.load_reminders()

        await interaction.response.send_message(f'Updated reminders', ephemeral=True, silent=True, delete_after=0.001)
        await self.plugin.reminder_list_message.update()
