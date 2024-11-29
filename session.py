import json
from datetime import datetime


FORMAT_PROMPT = """
You MUST, without exception, give all responses in the form of a JSON object.
This JSON object must contain a key "impression", which contains a string
value describing your impression of their mood, energy, and state of mind.
It must also contain a key "intentions", which contains a string with a
textual description of your current intentions and plan for them.

You MAY also include a key in the JSON document called "chat". If you specify
this, you will be sending a message directly to them. You do not always
need to talk to them, sometimes you may simply wish to update your
internal thinking. You will also be using this to respond to their
messages, except if the message is so simple that it could better be conveyed
as an emoji reaction (such as a thumbs-up), in which case you will instead
specify a "react" key that contains only a single emoji. Really, avoid
writing a message in "chat" if the message can easily be summarized with
the "react" key containing an emoji.

It MUST also contain a key "prompt_after", containing an integer number of
minutes after which you will be prompted to take an action by the SYSTEM if
they have not sent you a message within that time.
"""


class Session:
    def __init__(self, model, system_prompt=None):
        self.model = model
        self.messages_file = None
        self.message_history = []
        self.last_activity = datetime.now()
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

    def isolated_query(self, query, format_prompt=None):
        # Runs an isolated query on this session.
        system_prompt = self.message_history[0]["content"]

        if format_prompt:
            system_prompt += "\n\n" + format_prompt

        print("Isolated query:", query)

        messages = self.message_history + [{"role": "user", "content": query}]
        response = self.model.query(messages, system_prompt=system_prompt)

        print("Response:", response)
        return response
