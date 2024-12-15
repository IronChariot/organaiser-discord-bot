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
        elif self.repeat_interval == 'month':
            return timedelta(months=1)
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
    
    def add_reminder(self, reminder):
        if reminder not in self.reminders:
            self.reminders.append(reminder)
            self.save()

    def get_reminders(self):
        return self.reminders
    
    def remove_reminder(self, time, text):
        for reminder in self.reminders:
            if reminder.time == time and reminder.text == text:
                self.reminders.remove(reminder)
                break
        self.save()

    def save(self):
        # The time needs to be converted to a string since datetime isn't compatible with JSON
        with open('reminders.json', 'w') as f:
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

    def load(self):
        # Check if the file exists
        if not os.path.isfile('reminders.json'):
            # Create the file
            self.save()

        reminders = []
        num_dupes = 0
        with open('reminders.json', 'r') as f:
            for reminder_dict in json.load(f):
                reminder = Reminder(datetime.fromisoformat(reminder_dict['time']), reminder_dict['text'], reminder_dict['repeat'], reminder_dict['repeat_interval'])
                if reminder not in reminders:
                    reminders.append(reminder)
                else:
                    num_dupes += 1

        self.reminders = reminders
        if num_dupes > 0:
            print(f'Removed {num_dupes} duplicate reminders')
            self.save()
        return reminders

    def __str__(self):
        # Order the reminders by time, starting with the next one
        self.reminders.sort(key=lambda x: x.time)
        reminders_string = '\n'.join([str(reminder) for reminder in self.reminders])
        return reminders_string

    def get_reminders_before(self, time):
        return [reminder for reminder in self.reminders if reminder.time < time]
