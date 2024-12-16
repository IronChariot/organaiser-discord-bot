from enum import Enum
from pydantic import BaseModel
import datetime

class IntervalEnum(str, Enum):
    day = 'day'
    week = 'week'
    fortnight = 'fortnight'
    month = 'month'
    quarter = 'quarter'
    year = 'year'

class ResponseSchema(BaseModel):
    # Textual description of the user's current mood
    impression: str
    # Textual description of intentions
    intentions: str
    # Message to send
    chat: str
    # Emoji to react to a message with
    react: str
    # Number of minutes to wait before sending the next self-prompt
    prompt_after: int
    # Date and Time of a reminder to create
    timed_reminder_time: datetime
    timed_reminder_text: str
    timed_reminder_repeat: bool
    # Interval should be a string, specifically either 'day' or 'week'
    timed_reminder_repeat_interval: IntervalEnum
    bug_report: str