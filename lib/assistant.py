import pathlib
import json
import sys, os
import asyncio
from datetime import datetime, time, timezone, timedelta
from zoneinfo import ZoneInfo

from . import models
from .session import Session
from .reminders import Reminders
from .msgtypes import parse_message

if sys.version_info >= (3, 11):
    import tomllib
else:
    from pip._vendor import tomli

SESSION_DIR = pathlib.Path(__file__).parent.parent.resolve() / 'sessions'


class Assistant:
    def __init__(self, id, model, image_model=None):
        self.id = id
        self.model = model
        self.image_model = image_model
        self.temperature = 1.0
        self.max_tokens = 1024
        self.prompt_template = []
        self.discord_config = {}
        self.summarisation_threshold = None
        self.unsummarised_messages = 1000
        self.timezone = None
        self.rollover = None
        self.reminders = Reminders()

    def get_today(self):
        "Returns the current date, respecting the configured rollover."
        now = datetime.now(tz=self.timezone)
        today = now.date()
        if self.rollover:
            if self.rollover >= time(12):
                if now.time() >= self.rollover:
                    today += timedelta(days=1)
            else:
                if now.time() < self.rollover:
                    today -= timedelta(days=1)
        return today

    @staticmethod
    def load(ident):
        if sys.version_info >= (3, 11):
            data = tomllib.load(open(ident + '.toml', 'rb'))
        else:
            data = tomli.load(open(ident + '.toml', 'r'))
        ident = data.get('id', ident)

        model_name = data['model']
        image_model_name = data.get('image_model')
        if model_name.startswith('claude-'):
            model = models.AnthropicModel(model_name)
        elif model_name.startswith('gpt-'):
            model = models.OpenAIModel(model_name, image_model_name)
        elif model_name.startswith('openrouter-'):
            model = models.OpenRouterModel(model_name)
        elif model_name.startswith('gemini-'):
            model = models.GeminiModel(model_name)
        else:
            model = models.OllamaModel(model_name)

        if image_model_name or os.environ.get('OPENAI_API_KEY'):
            # For now all image generation goes through OpenAI
            if isinstance(model, models.OpenAIModel):
                image_model = model
            else:
                image_model = models.OpenAIModel('gpt-4o-mini', image_model_name)
        else:
            image_model = None

        ass = Assistant(ident, model, image_model)
        if 'temperature' in data:
            ass.temperature = data['temperature']
        if 'max_tokens' in data:
            ass.max_tokens = data['max_tokens']
        if 'timezone' in data:
            ass.timezone = ZoneInfo(data['timezone'])
        if 'rollover' in data:
            ass.rollover = data['rollover']
        if not ass.rollover:
            ass.rollover = time(0, 0)
        ass.prompt_template = data['system_prompt']
        ass.discord_config = data.get('discord', {})
        ass.summarisation_threshold = data.get('summarisation_threshold')
        ass.unsummarised_messages = data.get('unsummarised_messages', 1000)

        # Load reminders from file
        ass.reminders.load()

        return ass

    async def make_system_prompt(self, date, last_session=None):
        prompt = []
        if last_session is None:
            last_session = self.find_session_before(date)

        # Let the AI know what day it is relative to the day it's based on
        date_str = date.strftime('%A, %d %B %Y')
        if last_session:
            delta = date - last_session.date
            if delta.days == 1:
                preface = f"It is now {date_str} (the next day)."
            elif delta.days > 1:
                preface = f"It is now {date_str} ({delta.days} days later)."
            else:
                # Huh?
                preface = f"It is now {date_str}."
        else:
            preface = f"It is now {date_str}."

        for component in self.prompt_template:
            heading = component.get('heading')
            if heading:
                prompt.append(heading)

            if component['type'] == 'date':
                if component['format']:
                    date_str = date.strftime(component['format'])
                else:
                    date_str = f'Today is {date.isoformat()}.'
                prompt.append(date_str)

            elif component['type'] == 'text':
                prompt.append(component['content'].strip())

            elif component['type'] == 'question':
                question = component['question'].strip()
                if last_session:
                    response = await last_session.isolated_query(f'SYSTEM: {preface} {question}')
                    prompt.append(response.strip())

            elif component['type'] == 'user_profile':
                # Get the user profile text from {assistant_name}_user_profile.txt:
                with open(f'{self.id}_user_profile.txt', 'r') as fh:
                    prompt.append(fh.read().strip())

            elif heading:
                prompt.append('Not yet implemented.')

        return '\n\n'.join(prompt)

    def find_session_before(self, date, limit=100):
        """Finds the last session occurring before (not on) the given date."""

        for i in range(limit):
            date = date - timedelta(days=1)

            session = self.load_existing_session(date)
            if session is not None:
                return session

        return None

    def load_existing_session(self, date, writable=False):
        session_path = SESSION_DIR / f'{self.id}-{date.isoformat()}.jsonl'

        if session_path.is_file():
            session_file = session_path.open('r+' if writable else 'r')
            session = Session(date, self)
            session.last_activity = datetime.fromtimestamp(session_path.stat().st_mtime, tz=timezone.utc)

            if writable:
                session.messages_file = session_file

            for line in session_file:
                line = line.strip()
                if line:
                    session.message_history.append(parse_message(line))

            return session
        else:
            return None

    async def load_session(self, date, last_session=None):
        session = self.load_existing_session(date, writable=True)
        if session:
            return session

        system_prompt = await self.make_system_prompt(date, last_session=last_session)
        session = Session(date, self, system_prompt)

        session_path = SESSION_DIR / f'{self.id}-{date.isoformat()}.jsonl'
        session_path.parent.mkdir(exist_ok=True)

        session_file = session_path.open('w')
        session.message_history[0].dump(session_file)
        session_file.flush()

        session.messages_file = session_file
        return session
