# Class which defines timed reminders, and makes sure they get pickled and unpickled as needed

import json
from datetime import date, datetime, timezone, timedelta
import os

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


class Reminders:
    def __init__(self):
        self.reminders = []
        self.filename = None
    
    def add_reminder(self, reminder):
        if reminder not in self.reminders:
            self.reminders.append(reminder)
            self.save()
            return True
        else:
            return False

    def get_reminders(self):
        return self.reminders
    
    def remove_reminder(self, time, text):
        for reminder in self.reminders:
            if reminder.time == time and reminder.text == text:
                self.reminders.remove(reminder)
                break
        self.save()

    def save(self):
        assert self.filename is not None

        # The time needs to be converted to a string since datetime isn't compatible with JSON
        with open(self.filename, 'w') as f:
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

    def load(self, file):
        reminders = []
        num_dupes = 0
        with file as f:
            for reminder_dict in json.load(f):
                reminder = Reminder(datetime.fromisoformat(reminder_dict['time']), reminder_dict['text'], reminder_dict['repeat'], reminder_dict['repeat_interval'])
                if reminder not in reminders:
                    reminders.append(reminder)
                else:
                    num_dupes += 1

        self.filename = file.name
        self.reminders = reminders
        if num_dupes > 0:
            print(f'Removed {num_dupes} duplicate reminders')
            self.save()
        return reminders

    def reload(self):
        assert self.filename is not None
        self.load(open(self.filename, 'r'))

    def __str__(self):
        # Order the reminders by time, starting with the next one
        self.reminders.sort(key=lambda x: x.time)
        reminders_string = '\n'.join([str(reminder) for reminder in self.reminders])
        return reminders_string

    def as_markdown(self, tz, cutoff=2000):
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
            if num_chars + line_len <= cutoff:
                result.append(line)
                num_chars += line_len
            else:
                break

        return '\n'.join(result)

    def get_reminders_before(self, time):
        return [reminder for reminder in self.reminders if reminder.time < time]
