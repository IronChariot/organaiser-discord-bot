import json
from datetime import datetime

# Format prompt comes from session_format_prompt.txt
with open('session_format_prompt.txt', 'r') as f:
    FORMAT_PROMPT = f.read()

with open('diary_prompt.txt', 'r') as f:
    DIARY_PROMPT = f.read()


class Session:
    def __init__(self, date, model, system_prompt=None, assistant_id="naiser"):
        self.date = date
        self.model = model
        self.messages_file = None
        self.message_history = []
        self.last_activity = datetime.now()
        self.assistant_id = assistant_id
        if system_prompt:
            self.message_history.append({"role": "system", "content": system_prompt})

    def get_last_assistant_response(self):
        for message in self.message_history[::-1]:
            if message["role"] != "assistant":
                continue

            try:
                return json.loads(message["content"])
            except json.JSONDecodeError:
                continue

        return None

    def chat(self, content):
        # User or system sends a message.  Returns AI response (as JSON).
        self.last_activity = datetime.now()

        system_prompt = self.message_history[0]["content"] + "\n\n" + FORMAT_PROMPT

        self.message_history.append({"role": "user", "content": content})

        response = self.model.query(self.message_history, system_prompt=system_prompt, as_json=True)

        self.messages_file.write(f'{json.dumps(self.message_history[-2])}\n')
        self.messages_file.write(f'{json.dumps(self.message_history[-1])}\n')
        self.messages_file.flush()
        return response

    def isolated_query(self, query, format_prompt=None, as_json=False):
        # Runs an isolated query on this session.
        system_prompt = self.message_history[0]["content"]

        if format_prompt:
            system_prompt += "\n\n" + format_prompt

        print("Isolated query:", query)

        messages = self.message_history + [{"role": "user", "content": query}]
        response = self.model.query(messages, system_prompt=system_prompt, as_json=as_json)

        print("Response:", response)
        return response

    def write_diary_entry(self):
        response = self.isolated_query("SYSTEM: " + DIARY_PROMPT)
        with open(f'diaries/{self.assistant_id}-{self.date.isoformat()}.txt', 'w') as fh:
            fh.write(response)

        return response
