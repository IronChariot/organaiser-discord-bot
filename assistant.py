import tomllib
import pathlib
import json
from datetime import datetime, timezone, timedelta

import models
from session import Session

SESSION_DIR = pathlib.Path(__file__).parent.resolve() / 'sessions'


class Assistant:
    def __init__(self, id, model):
        self.id = id
        self.model = model
        self.temperature = 1.0
        self.max_tokens = 1024
        self.prompt_template = []
        self.discord_config = {}

    @staticmethod
    def load(ident):
        data = tomllib.load(open(ident + '.toml', 'rb'))
        ident = data.get('id', ident)

        model_name = data['model']
        if model_name.startswith('claude-'):
            model = models.AnthropicModel(model_name)
        elif model_name.startswith('gpt-'):
            model = models.OpenAIModel(model_name)
        else:
            model = models.OllamaModel(model_name)

        ass = Assistant(ident, model)
        if 'temperature' in data:
            ass.temperature = data['temperature']
        if 'max_tokens' in data:
            ass.max_tokens = data['max_tokens']
        ass.prompt_template = data['system_prompt']
        ass.discord_config = data['discord']
        return ass

    def make_system_prompt(self, date):
        prompt = []
        last_session = self.find_session_before(date)
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
                    response = last_session.isolated_query(f'SYSTEM: {question}')
                    prompt.append(response.strip())

            elif heading:
                prompt.append('Not yet implemented.')

        return '\n\n'.join(prompt)

    def find_session_before(self, date, limit=100):
        """Finds the last session occurring before (not on) the given date."""

        for i in range(limit):
            date = date - timedelta(days=1)

            session_path = SESSION_DIR / f'{self.id}-{date.isoformat()}.jsonl'
            if session_path.is_file():
                session = Session(date, self.model, assistant_id=self.id)

                for line in session_path.open('r'):
                    line = line.strip()
                    if line:
                        session.message_history.append(json.loads(line))

                return session

        return None

    def load_session(self, date):
        session_path = SESSION_DIR / f'{self.id}-{date.isoformat()}.jsonl'

        if session_path.is_file():
            session_file = session_path.open('r+')
            session = Session(date, self.model, assistant_id=self.id)
            session.last_activity = datetime.fromtimestamp(session_path.stat().st_mtime, tz=timezone.utc)

            for line in session_file:
                line = line.strip()
                if line:
                    session.message_history.append(json.loads(line))
        else:
            system_prompt = self.make_system_prompt(date)
            session = Session(date, self.model, system_prompt, assistant_id=self.id)

            session_path.parent.mkdir(exist_ok=True)

            session_file = session_path.open('w')
            session_file.write(json.dumps({"role": "system", "content": system_prompt}) + '\n')
            session_file.flush()

        session.messages_file = session_file
        return session
