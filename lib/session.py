import json
import pathlib
import asyncio
from datetime import datetime, time, timedelta

from .msgtypes import Role, Message, SystemMessage, UserMessage, AssistantMessage

# Format prompt comes from session_format_prompt.txt
with open('session_format_prompt.txt', 'r') as f:
    FORMAT_PROMPT = f.read().strip()

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
            self.message_history.append(SystemMessage(self.initial_system_prompt))
        self.context_lock = asyncio.Lock()

        self.standard_format_prompt = FORMAT_PROMPT

    def get_next_rollover(self):
        "Returns the datetime at which this session should end."

        date = self.date
        if self.assistant.rollover < time(12) or not self.assistant.rollover:
            date += timedelta(days=1)

        return datetime.combine(date, self.assistant.rollover, tzinfo=self.assistant.timezone)

    def find_message(self, id):
        assert id is not None

        for message in self.message_history:
            if message.id == id:
                return message

        return None

    def delete_message(self, id):
        assert id is not None

        for i, message in enumerate(self.message_history):
            if message.id == id:
                del self.message_history[i]
                break
        else:
            return

        self._rewrite_message_file()

    def append_message(self, message):
        "Appends a message without invoing the model."

        self.message_history.append(message)
        message.dump(self.messages_file)
        self.messages_file.flush()

    def get_last_assistant_message(self):
        for message in self.message_history[::-1]:
            if message.role != Role.ASSISTANT:
                continue

            return message

        return None

    def should_summarise(self):
        """Determines if the message history needs summarisation."""
        # Check if we have more than self.assistant.summarisation_threshold messages
        # Count how many messages there are since the last summarisation (which started with "~~~")
        messages_since_last_summary = 0
        for message in self.message_history[::-1]:
            if message.is_summary():
                break
            messages_since_last_summary += 1
        if self.assistant.summarisation_threshold is not None and \
           messages_since_last_summary > self.assistant.summarisation_threshold:
            return True

        return False

    async def create_summary(self):
        """Creates a summary of older messages."""
        # Keep the last self.assistant.unsummarised_messages messages unsummarised
        messages_to_summarise = self.message_history[1:-self.assistant.unsummarised_messages]  # Skip system prompt
        recent_messages = self.message_history[-self.assistant.unsummarised_messages:]

        # Find the last summary if it exists
        last_summary = None
        for msg in messages_to_summarise[::-1]:
            if msg.is_summary():
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
            if "minutes later\u2026)" in message.content or "SYSTEM:" in message.content:
                continue
            # Check if the role is assistant - if so, extract the json response from the content and extract the "chat" or "react" field, if any
            message_content = ""
            if message.role == Role.ASSISTANT:
                try:
                    response = message.parse_json()
                    if 'chat' in response:
                        message_content = response['chat']
                    elif 'react' in response:
                        message_content = response['react']
                except json.JSONDecodeError:
                    message_content = message.content
            else:
                message_content = message.content
            # Ignore empty messages
            if not message_content or message_content.isspace():
                continue
            message_log_string += f"{message.role}: {message_content}\n"

        # Create the summary
        summary_messages = [SystemMessage(SUMMARY_PROMPT)]
        if last_summary:
            summary_messages.append(AssistantMessage(last_summary.content))
        summary_messages.append(UserMessage(message_log_string))

        summary = await self.assistant.model.query(summary_messages, system_prompt=SUMMARY_PROMPT)
        # print("Sent to model to summarise: \n", summary_messages)
        # print("Summary of previous messages: ", summary)

        # Create new message history with the summary
        new_history = [self.message_history[0]]  # Keep system prompt
        if last_summary:
            new_history.extend(messages_to_summarise[:summary_start_idx]) # Keep previous summaries
        new_history.append(AssistantMessage(f"~~~ {summary}"))
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
                message.dump(self.messages_file)
            self.messages_file.flush()

    async def chat(self, message: UserMessage):
        "User or system sends a message.  Returns AI response (as JSON)."

        self.last_activity = datetime.now()

        # First, get the contents of the todo file and long term goals file
        with self.assistant.open_memory_file('todo.json') as fh:
            todo_json = fh.read()
        todo_dict = json.loads(todo_json)
        todo_string = "# Today's TODO:\n"
        for todo_text in todo_dict:
            todo_string += f"- {todo_text}\n"
        todo_string += "\n"

        with self.assistant.open_memory_file('long_term_goals.json') as fh:
            long_term_goals = fh.read()
        long_term_goals_string = "# Long term goals:\n"
        for long_term_goal_text in long_term_goals:
            long_term_goals_string += f"- {long_term_goal_text}\n"
        long_term_goals_string += "\n"

        # Get the reminders string:
        reminders_string = "# Currently scheduled reminders:\n"
        reminders_string += str(self.assistant.reminders)

        system_prompt = self.message_history[0].content + \
                        todo_string + \
                        long_term_goals_string + \
                        reminders_string + \
                        "\n\n" + \
                        self.standard_format_prompt

        async with self.context_lock:
            # Check if we need to summarise before adding new message
            if self.should_summarise():
                await self.create_summary()

            self.message_history.append(message)

            response = await self.assistant.model.query(self.message_history, system_prompt=system_prompt, as_json=True)

            self.message_history[-2].dump(self.messages_file)
            self.message_history[-1].dump(self.messages_file)
            self.messages_file.flush()

        return response

    async def isolated_query(self, query, attachments=[], format_prompt=None, as_json=False):
        # Runs an isolated query on this session.
        system_prompt = self.message_history[0].content

        if format_prompt:
            system_prompt += "\n\n" + format_prompt

        print("Isolated query:", query)

        message = UserMessage(query)
        message.attachments[:] = attachments
        messages = self.message_history + [message]
        response = await self.assistant.model.query(messages, system_prompt=system_prompt, as_json=as_json)

        print("Response:", response)
        return response
