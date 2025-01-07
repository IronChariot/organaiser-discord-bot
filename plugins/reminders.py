import discord
import json
import asyncio
from datetime import datetime, date, time, timedelta, timezone

from lib.plugin import Plugin, hook, action, system_prompt, pinned_message
from lib.msgtypes import UserMessage


class Reminder:
    def __init__(self, time, text, repeat=None):
        self.id = None
        self.time = time
        self.text = text
        if repeat and repeat.lower() not in ('none', 'null'):
            if repeat.startswith('1 '):
                repeat = repeat.strip('1 s')
            self.repeat = repeat
        else:
            self.repeat = None
        self.active = False

    def get_next_repetition(self, after=None):
        if not self.repeat:
            return None

        if after is None:
            after = self.time

        interval = self.repeat.rstrip('s')
        if ' ' in interval:
            count, interval = interval.split(' ')
            count = int(count)
        else:
            count = 1

        # Reduce everything to days or months
        if interval == 'week':
            interval = 'day'
            count *= 7
        elif interval == 'fortnight':
            interval = 'day'
            count *= 14
        elif interval == 'quarter':
            interval = 'month'
            count *= 3
        elif interval == 'year':
            interval = 'month'
            count *= 12
        elif interval == 'lustrum':
            interval = 'month'
            count *= 60
        elif interval == 'decade':
            interval = 'month'
            count *= 120

        if interval == 'day':
            time = self.time
            while time <= after:
                time += timedelta(days=count)
            return time

        assert interval == 'month'
        time = self.time
        while time <= after:
            carry, new_month = divmod((time.month - 1) + count, 12)
            new_month += 1
            time = time.replace(year=time.year + carry, month=new_month)
        return time

    def __eq__(self, other):
        return (self.time == other.time and
                self.text == other.text and
                self.repeat == other.repeat)

    def __str__(self):
        if self.repeat:
            return f'[ID: R{self.id:03}] {self.time}: {self.text} (repeats every {self.repeat})'
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
        self.next_id = 1
        self.load_reminders()

    @hook('session_load')
    async def on_session_load(self, session):
        next_rollover = session.get_next_rollover()
        for reminder in self.reminders:
            if reminder.time < next_rollover:
                print(f'Setting reminder for {reminder.time}: {reminder.text}')
                reminder.active = True
                self.schedule(reminder.time, self.send_reminder(reminder, session))

    @system_prompt
    def static_system_prompt(self, session):
        return (
            'You may optionally set the key "add_reminders" if you wish to set '
            'a time at which you will be prompted by the SYSTEM to consider a '
            'thought or action. These reminders are set by you and for you, the '
            'user will not see them unless you decide to inform them. The key '
            'contains a list of JSON objects like {"time": "YYYY-MM-DD HH:MM:SS", '
            '"text": "Remind user of piano appointment"}, optionally extended '
            'with the key "repeat" which may be something like "3 days" or '
            '"4 weeks" or "1 month" if you want the reminder to reschedule '
            'itself automatically when it goes off. The system will assign a '
            'unique ID to each reminder of the form R987 which will appear in '
            'your reminder list. If you wish to remove reminders, specify a '
            '"remove_reminders" key in your JSON response with a list of '
            'identifiers of the respective reminder, but note that that this '
            'will remove all future repetitions as well!'
        )

    @system_prompt(dynamic=True)
    def dynamic_system_prompt(self, session):
        result = ['# Currently scheduled reminders:']

        for reminder in sorted(self.reminders, key=lambda r: r.time):
            time = reminder.time.astimezone(self.assistant.timezone)
            formatted_time = time.strftime('%Y-%m-%d %H:%M')

            text = f'[ID: R{reminder.id:03}] {formatted_time}: {reminder.text}'
            if reminder.repeat:
                text += f' (repeats every {reminder.repeat})'
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

    @action('remove_reminders')
    async def on_remove_reminders(self, response, *, remove_reminders=()):
        if not remove_reminders:
            return

        remove_ids = set()
        for reminder_id in remove_reminders:
            remove_ids.add(int(reminder_id.lstrip('rR IDid:[').rstrip(' ]'), 10))

        old_reminders = self.reminders
        self.reminders = []
        num_removed = 0
        for reminder in old_reminders:
            if reminder.id in remove_ids:
                reminder.active = False
                num_removed += 1
            else:
                self.reminders.append(reminder)

        if num_removed == 0:
            return

        self.save_reminders()

        if num_removed > 1:
            return f'Removed {num_removed} reminders'
        else:
            return f'Removed 1 reminder'

    @action('add_reminders')
    async def on_add_reminders(self, response, *, add_reminders=()):
        if not add_reminders:
            return

        if isinstance(add_reminders, dict):
            add_reminders = (add_reminders, )

        actions = []
        for data in add_reminders:
            if not data.get("time") or not data.get("text"):
                continue

            # Parse the time as a datetime:
            reminder_time = datetime.fromisoformat(data["time"])
            # Make the reminder time local to UTC
            if not reminder_time.tzinfo and self.assistant.timezone is not None:
                reminder_time = reminder_time.replace(tzinfo=self.assistant.timezone)
            reminder_time = reminder_time.astimezone(timezone.utc)

            reminder = Reminder(reminder_time, data["text"], data.get("repeat"))
            await self.add_timed_reminder(reminder, response.session)

            if not reminder.id:
                continue

            timestamp = int(reminder_time.timestamp())
            rel_date = None
            if reminder_time.date() == date.today():
                rel_date = 'today'
            elif reminder_time.date() == date.today() + timedelta(days=1):
                rel_date = 'tomorrow'
            else:
                rel_date = f'<t:{timestamp}:R>'

            if reminder.repeat == 'day':
                actions.append(f'Added daily reminder at <t:{timestamp}:t> starting {rel_date}')
            elif reminder.repeat:
                actions.append(f'Added {reminder.repeat}ly reminder starting {rel_date}')
            elif rel_date == 'today':
                actions.append(f'Added reminder going off <t:{timestamp}:R>')
            else:
                actions.append(f'Added reminder going off {rel_date} at <t:{timestamp}:t>')

        return actions

    async def add_timed_reminder(self, reminder: Reminder, session):
        if reminder in self.reminders:
            return

        if reminder.id is None:
            reminder.id = self.next_id
            self.next_id += 1

        self.reminders.append(reminder)
        self.save_reminders()

        if reminder.time < session.get_next_rollover():
            print(f'Setting reminder for {reminder.time}: {reminder.text}')
            reminder.active = True
            self.schedule(reminder.time, self.send_reminder(reminder, session))

        # Update the pinned reminders message
        await self.reminder_list_message.update()

    async def send_reminder(self, reminder: Reminder, session):
        if not reminder.active:
            print(f'Not responding to deactivated reminder R{reminder.id:03}')
            return
        reminder.active = False

        # Remove the reminder from the list
        if reminder in self.reminders:
            self.reminders.remove(reminder)

        begin_time = datetime.now(tz=timezone.utc)
        print(f'Responding to reminder R{reminder.id:03} for {reminder.time}: {reminder.text}')
        msg = f'SYSTEM: Reminder R{reminder.id:03} from your past self now going off: {reminder.text}.'
        if reminder.repeat:
            msg += f' Reminder will repeat after {reminder.repeat}.'
        else:
            msg += ' Reminder has been removed.'
        await session.push_message(UserMessage(msg))

        # If the reminder repeats, set up a new reminder for the next time (after 1 interval):
        if reminder.repeat:
            new_time = reminder.get_next_repetition(after=begin_time)
            new_reminder = Reminder(new_time, reminder.text, reminder.repeat)
            new_reminder.id = reminder.id
            await self.add_timed_reminder(new_reminder, session)
        else:
            self.save_reminders()

    def load_reminders(self):
        reminders = []
        with self.assistant.open_memory_file('reminders.json', default='[]') as fh:
            reminders = []
            num_dupes = 0
            num_without_id = 0
            for reminder_dict in json.load(fh):
                repeat = reminder_dict['repeat_interval'] if reminder_dict['repeat'] else None
                reminder = Reminder(datetime.fromisoformat(reminder_dict['time']), reminder_dict['text'], repeat)
                if reminder not in reminders:
                    reminders.append(reminder)
                else:
                    num_dupes += 1

                if reminder_dict.get('id'):
                    reminder.id = reminder_dict['id']
                    self.next_id = max(self.next_id, reminder.id + 1)
                else:
                    num_without_id += 1

            if num_without_id > 0:
                for reminder in reminders:
                    reminder.id = self.next_id
                    self.next_id += 1

            self.reminders = reminders

            if num_dupes > 0:
                print(f'Removed {num_dupes} duplicate reminders')
                self.save_reminders()
            elif num_without_id > 0:
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
                    'id': reminder.id,
                    'time': reminder.time.isoformat(),
                    'text': reminder.text,
                    'repeat': bool(reminder.repeat),
                    'repeat_interval': reminder.repeat or 'daily',
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
