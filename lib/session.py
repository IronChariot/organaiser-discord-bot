import json
import pathlib
import asyncio
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from .response import AssistantResponse
from .msgtypes import Role, Message, SystemMessage, UserMessage, AssistantMessage
from .util import Condition

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

        for plugin in assistant.plugins.values():
            for prompt in plugin._static_system_prompts:
                self.standard_format_prompt += '\n' + prompt(self)

        self.new_user_message = Condition()

    def get_next_rollover(self):
        "Returns the datetime at which this session should end."

        date = self.date
        if self.assistant.rollover < time(12) or not self.assistant.rollover:
            date += timedelta(days=1)

        return datetime.combine(date, self.assistant.rollover, tzinfo=self.assistant.timezone)

    @property
    def last_message(self):
        return self.message_history[-1]

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
        self.append_messages((message,))

    def append_messages(self, messages):
        """Appends messages to the message history.
        Messages will be timestamped if they have not already been."""

        self.message_history.extend(messages)
        for message in messages:
            if message.timestamp is None:
                message.timestamp = datetime.now(tz=timezone.utc)
            message.dump(self.messages_file)
        self.messages_file.flush()

        if any(message.role == Role.USER for message in messages):
            self.new_user_message.notify_all()

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

    async def chat(self, message: UserMessage) -> AssistantResponse:
        """User or system sends a message.  Returns assistant response."""

        self.last_activity = datetime.now()

        self.append_message(message)
        return await self.query_assistant_response()

    async def query_assistant_response(self) -> Optional[AssistantResponse]:
        """Asks the assistant to respond to the current message history,
        if there are any user messages to respond to, or None."""

        system_prompt = self.message_history[0].content + \
                        "\n\n" + \
                        self.standard_format_prompt

        for plugin in self.assistant.plugins.values():
            for prompt in plugin._dynamic_system_prompts:
                system_prompt += '\n\n' + prompt(self)

        async with self.context_lock:
            if self.message_history[-1].role != Role.USER:
                return

            # Check if we need to summarise before adding new message
            if self.should_summarise():
                await self.create_summary()

            user_message = self.message_history[-1]
            data = await self.assistant.model.query(self.message_history, system_prompt=system_prompt, as_json=True)

            self.message_history[-1].dump(self.messages_file)
            self.messages_file.flush()

        return AssistantResponse(self, data, user_message)

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
