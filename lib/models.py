import requests
import json
import re
from base64 import standard_b64encode
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Optional
import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request
from openai import AsyncOpenAI
import os
from datetime import datetime, timezone
import asyncio
from contextvars import ContextVar

from .msgtypes import Role, Message, AssistantMessage, Attachment

# Time in seconds between checking whether a batch is done.
BATCH_CHECK_DELAY = 60.0
BATCH_CHECK_BACKOFF = 1.5


class Model(ABC):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0, max_tokens: int = 1024, logger=None):
        self.model_name = model_name
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.logger = logger or logging.getLogger(__name__)

    async def batch(self, *calls):
        # Default implementation just runs everything one by one
        return [await call for call in calls]

    async def query(self, messages: List[Message], system_prompt=None, validate_func=None, return_type=str) -> str:
        temperature = self.temperature
        max_attempts = int((1.0 - temperature) / 0.1) + 1  # Calculate max attempts to reach t=1.0

        if system_prompt is None:
            system_prompt = self.system_prompt

        for attempt in range(max_attempts):
            if attempt > 0:
                self.logger.info(f"Querying model (Attempt {attempt + 1}, Temperature: {temperature})")
            self.logger.info(f"User message: {messages[-1].content}")

            message = await self.chat_completion(messages, self.model_name, temperature, self.max_tokens, system_prompt, return_type)
            self.logger.info(f"Model response: {message}")

            if message and not message.timestamp:
                message.timestamp = datetime.now(tz=timezone.utc)

            valid = True
            if return_type is not str:
                # Some models will output text other than the JSON response
                # Best way to find the JSON alone would be to specifically get everything from the { to the }
                if return_type is list and "[" in message.content:
                    begin = message.content.find("[")
                    end = message.content.rfind("]")
                    if begin < 0 or end < begin:
                        valid = False
                    else:
                        message.content = message.content[begin:end + 1]

                elif return_type is dict and "{" in message.content:
                    begin = message.content.find("{")
                    end = message.content.rfind("}")
                    if begin < 0 or end < begin:
                        valid = False
                    else:
                        message.content = message.content[begin:end + 1]

                if not message.content:
                    response = None
                else:
                    try:
                        response = json.loads(message.content, strict=False)
                    except json.JSONDecodeError:
                        # Remove comments
                        if '//' in message.content or '#' in message.content:
                            message.content = re.sub(r'([\[\]\{\},"])\s*(//|#).*$', '\\1', message.content, flags=re.MULTILINE)
                            try:
                                response = json.loads(message.content, strict=False)
                            except json.JSONDecodeError:
                                valid = False
                        else:
                            valid = False
            else:
                response = message.content

            if valid and validate_func is not None and not validate_func(response):
                valid = False

            if valid:
                messages.append(message)
                return response

            temperature = min(temperature + 0.1, 1.0)
            self.logger.warning(f"Response: {message.content}")
            self.logger.warning(f"Invalid response. Increasing temperature to {temperature}")
            await asyncio.sleep(0.25)

        # If we've reached this point, even t=1.0 didn't work
        self.logger.error("Failed to get a valid response even at maximum temperature.")
        raise ValueError("Failed to get a valid response from the model")

    def reset_conversation(self) -> None:
        """
        Reset the conversation history.
        """
        pass

    @abstractmethod
    async def chat_completion(self, messages: List[Message], model: str, temperature: float, max_tokens: int, system_prompt: str, return_type: type) -> str:
        """
        Send a message to the model and get a response.
        """
        pass


class OllamaModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 5000, logger=None):
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)

    @staticmethod
    async def chat_completion(messages: List[Message], model: str, temperature: float, max_tokens: int, system_prompt: str, return_type: type) -> Tuple[str, List[Dict[str, str]]]:
        messages = [{"role": message.role.value, "content": message.content} for message in messages if message.role != Role.SYSTEM]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        url = "http://localhost:11434/api/chat"
        headers = {
            "Content-Type": "application/json"
        }
        data = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens
            }
        }

        response = requests.post(url, headers=headers, data=json.dumps(data))

        text_response = ""
        if response.status_code == 200:
            text_response = response.json()["message"]["content"]
        else:
            text_response = f"Error: {response.status_code}"

        return AssistantMessage(text_response)


class AnthropicModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "claude-opus":
            model_name = "claude-3-opus-20240229" # $15/$75
        elif model_name == "claude-sonnet":
            model_name = "claude-3-5-sonnet-20241022" # $3/$15
        elif model_name == "claude-haiku":
            model_name = "claude-3-5-haiku-20241022" # $1/$5
        elif model_name == "claude-3-sonnet":
            model_name = "claude-3-sonnet-20240229"
        elif model_name == "claude-3-haiku":
            model_name = "claude-3-haiku-20240307"
        else: # Default to sonnet model
            model_name = "claude-3-5-sonnet-20241022"
            print("Invalid model specified. Defaulting to claude-3-5-sonnet-20241022.")
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        self.client = anthropic.AsyncAnthropic()
        self.batcher = ContextVar('batcher', default=None)

    async def encode_message(self, message):
        encoded = {"role": message.role.value}
        if message.attachments:
            encoded["content"] = []
            if message.content:
                encoded["content"].append({"type": "text", "text": message.content})
            for attach in message.attachments:
                data = standard_b64encode(await attach.read())
                source = {
                    "type": "base64",
                    "media_type": attach.content_type,
                    "data": data.decode("ascii"),
                }
                encoded["content"].append({"type": "image", "source": source})
        else:
            encoded["content"] = message.content
        return encoded

    async def chat_completion(self, messages=[], model='claude-3-haiku-20240307', temperature=0.0, max_tokens=1024, system_prompt="", return_type=str):
        # Check if the first message is a system message - if it is, we need to not pass it to Anthropic
        clean_messages = [await self.encode_message(message) for message in messages if message.role != Role.SYSTEM]

        # Prefill to increase changes of generating the right type
        prefill = ''
        if return_type is dict:
            prefill = '{'
        elif return_type is list:
            prefill = '['

        if prefill:
            clean_messages.append({"role": "assistant", "content": prefill})

        try:
            chat_completion = await self._do_request(
                system=system_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=clean_messages
            )

            text_response = prefill + chat_completion.content[0].text
            return AssistantMessage(text_response)

        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e)

    def _do_request(self, **kwargs):
        batcher = self.batcher.get()
        if batcher is not None:
            return batcher(MessageCreateParamsNonStreaming(**kwargs))
        else:
            return self.client.messages.create(**kwargs)

    async def batch(self, *calls):
        """Like asyncio.gather, but any queries are pooled together into a
        batch.  Returns a list of results when the batch is completed."""

        if len(calls) == 0:
            return []

        def collect_request(custom_id, coro):
            # Triggered when a request has been made
            request_fut = asyncio.Future()

            def batcher(params):
                request = Request(custom_id=custom_id, params=params)
                result_fut = asyncio.Future()
                request_fut.set_result((request, result_fut))
                self.batcher.set(None)
                return result_fut

            async def wrapper():
                try:
                    self.batcher.set(batcher)
                    return await coro
                finally:
                    # Make sure the result fut is flagged even if no request was
                    # added, or the caller will end up waiting indefinitely
                    if not request_fut.done():
                        request_fut.set_result((None, None))

            return request_fut, asyncio.create_task(wrapper())

        requests = []
        result_futs = {}
        tasks = []
        for i, coro in enumerate(calls):
            custom_id = str(i)
            request_fut, task = collect_request(custom_id, coro)
            request, result_fut = await request_fut
            if request is not None:
                requests.append(request)
                result_futs[custom_id] = result_fut

            tasks.append(task)

        batch = await self.client.messages.batches.create(requests=requests)
        batch_id = batch.id
        print(f"Initiated batch {batch_id}")

        delay = BATCH_CHECK_DELAY
        try:
            while batch.processing_status != 'ended':
                await asyncio.sleep(delay)
                delay *= BATCH_CHECK_BACKOFF
                batch = await self.client.messages.batches.retrieve(batch_id)

        finally:
            if batch.processing_status not in ('ended', 'canceling'):
                print(f"Cancelling batch {batch_id}")
                await self.client.messages.batches.cancel(batch_id)

        async for response in await self.client.messages.batches.results(batch_id):
            result_futs[response.custom_id].set_result(response.result.message)

        return await asyncio.gather(*tasks)


class OpenAIModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "gpt-4o":
            model_name = "gpt-4o-2024-11-20" # $2.5/$10
        elif model_name == "gpt-4o-mini":
            model_name = "gpt-4o-mini-2024-07-18" # $0.15/$0.6
        elif model_name == "gpt-4":
            model_name = "gpt-4-turbo-2024-04-09" # $10/$30
        elif model_name == "gpt-3.5":
            model_name = "gpt-3.5-turbo-1106" # $0.5/$1.5
        else:
            model_name = "gpt-4o-mini-2024-07-18"  # Default to GPT-4o Mini
            print("Invalid model specified. Defaulting to GPT-4o Mini.")
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        self.client = AsyncOpenAI()

    def encode_message(self, message):
        encoded = {"role": message.role.value}
        if message.attachments:
            encoded["content"] = []
            if message.content:
                encoded["content"].append({"type": "text", "text": message.content})
            for attach in message.attachments:
                encoded["content"].append({"type": "image_url", "image_url": {"url": attach.url}})
        else:
            encoded["content"] = message.content
        return encoded

    async def chat_completion(self, messages=[], model='gpt-4o-mini-2024-07-18', temperature=0.0, max_tokens=1024, system_prompt="", return_type=str):
        messages = [self.encode_message(message) for message in messages if message.role != Role.SYSTEM]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        try:
            chat_completion = await self.client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens
            )

            text_response = chat_completion.choices[0].message.content
            return AssistantMessage(text_response)

        except Exception as e:
            print("Error: " + str(e))
            return AssistantMessage("Error querying the LLM: " + str(e))


class OpenRouterModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "openrouter-llama-3.1":
            model_name = "meta-llama/llama-3.1-405b-instruct" # free
        elif model_name == "openrouter-llama-3.3":
            model_name = "meta-llama/llama-3.3-70b-instruct" # $0.13/M input tokens / $0.4/M output tokens
        elif model_name == "openrouter-qwen":
            model_name = "qwen/qwen-2.5-72b-instruct" # $0.23/M input tokens / $0.4/M output tokens
        else:
            model_name = "meta-llama/llama-3.1-405b-instruct"  # Default to the free Llama 3.1 API (while it's free?)
            print("Invalid model specified. Defaulting to meta-llama/llama-3.1-405b-instruct.")
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        OR_API_KEY = os.getenv('OPENROUTER_API_KEY')
        self.client = AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=OR_API_KEY
        )

    async def chat_completion(self, messages=[], model='meta-llama/llama-3.1-405b-instruct', temperature=0.0, max_tokens=1024, system_prompt="", return_type=str):
        messages = [{"role": message.role.value, "content": message.content} for message in messages if message.role != Role.SYSTEM]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        try:
            chat_completion = await self.client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens
            )
            text_response = chat_completion.choices[0].message.content
            return AssistantMessage(text_response)

        except Exception as e:
            print("Error: " + str(e))
            return AssistantMessage("Error querying the LLM: " + str(e))


class GeminiModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "You are a helpful assistant.", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        import google.generativeai as genai
        if model_name == "gemini-2.0-flash":
            model_name = "gemini-2.0-flash-exp"
        elif model_name in ("gemini-2.0-flash-thinking", "gemini-2.0-flash-thinking-exp"):
            model_name = "gemini-2.0-flash-thinking-exp-1219"
        elif model_name == "gemini-1.5-flash":
            pass
        elif model_name == "gemini-1.5-pro":
            pass
        else:
            model_name = "gemini-2.0-flash-exp"  # Default to the latest flash model
            print("Invalid model specified. Defaulting to gemini-2.0-flash-exp.")
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
        genai.configure(api_key=GEMINI_API_KEY)

    async def encode_parts(self, message):
        import google.generativeai as genai

        parts = []
        if message.content:
            parts.append(message.content)

        for attach in message.attachments:
            data = standard_b64encode(await attach.read())
            parts.append({
                "mime_type": attach.content_type,
                "data": data.decode("ascii"),
            })

        return parts

    async def encode_message(self, message):
        return {
            "role": "model" if message.role == Role.ASSISTANT else "user",
            "parts": await self.encode_parts(message),
        }

    async def chat_completion(self, messages=[], model='gemini-2.0-flash-exp', temperature=0.0, max_tokens=1024, system_prompt="", return_type=str):
        import google.generativeai as genai
        history = [await self.encode_message(message) for message in messages[:-1] if message.role != Role.SYSTEM and (message.content or message.attachments)]

        # Need to recreate the model with the system prompt as the system instruction
        client = genai.GenerativeModel(
            model_name=model,
            generation_config={
                "temperature": temperature,
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": max_tokens,
                "response_mime_type": "text/plain" if return_type is str or "thinking" in model else "application/json",
            },
            system_instruction=system_prompt
        )

        try:
            chat_session = client.start_chat(history=history)
            response = await chat_session.send_message_async(await self.encode_parts(messages[-1]))

            if len(response.parts) <= 1:
                return AssistantMessage(response.text)

            if return_type is dict or return_type is list:
                # Identify which part is JSON, the rest must be thought
                thoughts = []
                content = None
                for part in response.parts:
                    text = part.text.strip()
                    if text.lstrip()[0] in '{[`':
                        content = text
                    else:
                        thoughts.append(text)

                if not content:
                    return "No JSON found in LLM response."

                thought = '\n'.join(thoughts).strip()
                return AssistantMessage(content, thought=thought)

            # Assume the first part is thoughts, the rest content
            content = ''.join(part.text for part in response.parts[1:] if "text" in part)
            return AssistantMessage(content, thought=response.parts[0].text)

        except Exception as e:
            print("Error: " + str(e))
            return AssistantMessage("Error querying the LLM: " + str(e))


class DeepSeekModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        model_name = "deepseek-chat"  # $0.14 / 1M tokens Input, $0.28/1M tokens Output
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY')
        print(f"Key: {DEEPSEEK_API_KEY}")
        self.client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

    def encode_message(self, message):
        encoded = {"role": message.role.value}
        if message.attachments:
            encoded["content"] = []
            if message.content:
                encoded["content"].append({"type": "text", "text": message.content})
            for attach in message.attachments:
                encoded["content"].append({"type": "image_url", "image_url": {"url": attach.url}})
        else:
            encoded["content"] = message.content
        return encoded

    async def chat_completion(self, messages=[], model='deepseek-chat', temperature=0.0, max_tokens=1024, system_prompt=""):
        messages = [self.encode_message(message) for message in messages if message.role != Role.SYSTEM]
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})

        try:
            chat_completion = await self.client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False
            )

            text_response = chat_completion.choices[0].message.content
            return AssistantMessage(text_response)

        except Exception as e:
            print("Error: " + str(e))
            return AssistantMessage("Error querying the LLM: " + str(e))


def create(model_name):
    if model_name.startswith('claude-'):
        return AnthropicModel(model_name)
    elif model_name.startswith('gpt-'):
        return OpenAIModel(model_name)
    elif model_name.startswith('openrouter-'):
        return OpenRouterModel(model_name)
    elif model_name.startswith('gemini-'):
        return GeminiModel(model_name)
    elif model_name.startswith('deepseek-'):
        return DeepSeekModel(model_name)
    else:
        return OllamaModel(model_name)
