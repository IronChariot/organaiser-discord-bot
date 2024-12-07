# Class which defines timed reminders, and makes sure they get pickled and unpickled as needed

import pickle
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

    def get_reminders(self):
        return self.reminders
    
    def remove_reminder(self, time, text):
        for reminder in self.reminders:
            if reminder.time == time and reminder.text == text:
                self.reminders.remove(reminder)
                break

    def save(self):
        with open('reminders.pickle', 'wb') as f:
            pickle.dump(self, f)

    def load(self):
        # Check if the file exists
        if not os.path.isfile('reminders.pickle'):
            # Create the file
            reminders = Reminders()
            reminders.save()
        with open('reminders.pickle', 'rb') as f:
            return pickle.load(f)
        
    def __str__(self):
        return f'{self.reminders}'
    
    def todays_reminders(self):
        current_date_utc = datetime.now(timezone.utc).date()
        return [reminder for reminder in self.reminders if reminder.time.date() == current_date_utc]
    
