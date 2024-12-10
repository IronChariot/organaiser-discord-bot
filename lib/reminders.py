# Class which defines timed reminders, and makes sure they get pickled and unpickled as needed

import json
from datetime import date, datetime, timezone
import os

class Reminder:
    def __init__(self, time, text, repeat=False, repeat_interval=None):
        self.time = time
        self.text = text
        self.repeat = repeat
        self.repeat_interval = repeat_interval

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
        # Convert the list of Reminders to a list of dictionaries
        # The time needs to be converted to a string since datetime isn't compatible with JSON
        reminders_dict = [{'time': reminder.time.isoformat(), 'text': reminder.text, 'repeat': reminder.repeat, 'repeat_interval': reminder.repeat_interval} for reminder in self.reminders]
        reminders_json = json.dumps(reminders_dict)
        with open('reminders.json', 'w') as f:
            f.write(reminders_json)

    def load(self):
        # Check if the file exists
        if not os.path.isfile('reminders.json'):
            # Create the file
            self.save()
        with open('reminders.json', 'r') as f:
            reminders_json = f.read()
        reminders_dict = json.loads(reminders_json)
        # Need to parse the time back into a datetime
        self.reminders = [Reminder(datetime.fromisoformat(reminder['time']), reminder['text'], reminder['repeat'], reminder['repeat_interval']) for reminder in reminders_dict]
        return self.reminders
        
    def __str__(self):
        # Order the reminders by time, starting with the next one
        self.reminders.sort(key=lambda x: x.time)
        reminders_string = '\n'.join([str(reminder) for reminder in self.reminders])
        return reminders_string
    
    def todays_reminders(self):
        current_date_utc = datetime.now(timezone.utc).date()
        return [reminder for reminder in self.reminders if reminder.time.date() == current_date_utc]
    
