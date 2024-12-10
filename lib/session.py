import json
from datetime import datetime

# Format prompt comes from session_format_prompt.txt
with open('session_format_prompt.txt', 'r') as f:
    FORMAT_PROMPT = f.read()

with open('diary_prompt.txt', 'r') as f:
    DIARY_PROMPT = f.read()

with open('summary_prompt.txt', 'r') as f:
    SUMMARY_PROMPT = f.read()

class Session:
    def __init__(self, date, assistant, system_prompt=""):
        self.date = date
        self.messages_file = None
        self.message_history = []
        self.last_activity = datetime.now()
        self.assistant = assistant
        self.initial_system_prompt = system_prompt
        if self.initial_system_prompt:
            self.message_history.append({"role": "system", "content": self.initial_system_prompt})

    def get_last_assistant_response(self):
        for message in self.message_history[::-1]:
            if message["role"] != "assistant":
                continue

            try:
                return json.loads(message["content"])
            except json.JSONDecodeError:
                continue

        return None
    
    def should_summarise(self):
        """Determines if the message history needs summarisation."""
        # Check if we have more than self.assistant.summarisation_threshold messages
        if len(self.message_history) > self.assistant.summarisation_threshold:
            return True
        
        return False

    def create_summary(self):
        """Creates a summary of older messages."""
        # Keep the last self.assistant.unsummarised_messages messages unsummarised
        messages_to_summarise = self.message_history[1:-self.assistant.unsummarised_messages]  # Skip system prompt
        recent_messages = self.message_history[-self.assistant.unsummarised_messages:]

        # Find the last summary if it exists
        last_summary = None
        for msg in messages_to_summarise[::-1]:
            if msg['content'].startswith("Summary of previous messages:"):
                last_summary = msg
                break

        # Prepare messages for summarisation
        if last_summary:
            # Only summarise messages after the last summary
            summary_start_idx = messages_to_summarise.index(last_summary) + 1
            to_summarise = messages_to_summarise[summary_start_idx:]
        else:
            to_summarise = messages_to_summarise

        if not to_summarise:
            return
        
        message_log_string = ""
        for message in to_summarise:
            # Ignore system messages with no information
            if "SYSTEM: No response within given period." in message['content']:
                continue
            # Check if the role is assistant - if so, extract the json response from the content and extract the "chat" or "react" field, if any
            message_content = ""
            if message['role'] == "assistant":
                try:
                    response = json.loads(message['content'])
                    if 'chat' in response:
                        message_content = response['chat']
                    elif 'react' in response:
                        message_content = response['react']
                except json.JSONDecodeError:
                    message_content = message['content']
            else:
                message_content = message['content']
            # Ignore empty messages
            if not message_content or message_content.isspace():
                continue
            message_log_string += f"{message['role']}: {message_content}\n"

        # Create the summary
        summary_messages = [{"role": "system", "content": SUMMARY_PROMPT}]
        if last_summary:
            summary_messages.append({"role": "assistant", "content": last_summary['content']})
        summary_messages.append({"role": "user", "content": message_log_string})

        summary = self.assistant.model.query(summary_messages, system_prompt=SUMMARY_PROMPT)
        # print("Summary of previous messages: ", summary)

        # Create new message history with the summary
        new_history = [self.message_history[0]]  # Keep system prompt
        if last_summary:
            new_history.extend(messages_to_summarise[:summary_start_idx]) # Keep previous summaries
        new_history.append({
            "role": "assistant",
            "content": f"Summary of previous messages: {summary}"
        })
        new_history.extend(recent_messages)

        # Update message history and save to file
        self.message_history = new_history
        self._rewrite_message_file()

    def _rewrite_message_file(self):
        """Rewrites the entire message file with the current message history."""
        if self.messages_file:
            self.messages_file.seek(0)
            self.messages_file.truncate()
            for message in self.message_history:
                self.messages_file.write(json.dumps(message) + '\n')
            self.messages_file.flush()

    def chat(self, content):
        # Check if we need to summarise before adding new message
        if self.should_summarise():
            self.create_summary()

        # User or system sends a message.  Returns AI response (as JSON).
        self.last_activity = datetime.now()

                # First, get the contents of the todo file and long term goals file
        with open('todo.json', 'r') as fh:
            todo_json = fh.read()
        todo_dict = json.loads(todo_json)
        todo_string = "# Today's TODO:\n"
        for todo_text in todo_dict:
            todo_string += f"- {todo_text}\n"
        todo_string += "\n"

        with open('long_term_goals.json', 'r') as fh:
            long_term_goals = fh.read()
        long_term_goals_string = "# Long term goals:\n"
        for long_term_goal_text in long_term_goals:
            long_term_goals_string += f"- {long_term_goal_text}\n"
        long_term_goals_string += "\n"

        # Get the reminders string:
        reminders_string = "# Reminders:\n"
        reminders_string += str(self.assistant.reminders)

        system_prompt = self.message_history[0]["content"] + todo_string + long_term_goals_string + reminders_string + "\n\n" + FORMAT_PROMPT

        self.message_history.append({"role": "user", "content": content})

        response = self.assistant.model.query(self.message_history, system_prompt=system_prompt, as_json=True)

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
        response = self.assistant.model.query(messages, system_prompt=system_prompt, as_json=as_json)

        print("Response:", response)
        return response

    def write_diary_entry(self):
        response = self.isolated_query("SYSTEM: " + DIARY_PROMPT)
        with open(f'diaries/{self.assistant.id}-{self.date.isoformat()}.txt', 'w') as fh:
            fh.write(response)

        return response