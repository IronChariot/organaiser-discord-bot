import requests
import json
from base64 import standard_b64encode
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Tuple, Optional
import anthropic
from openai import AsyncOpenAI
import os
from datetime import datetime, timezone
import asyncio

from .msgtypes import Role, Message, AssistantMessage, Attachment


class Model(ABC):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0, max_tokens: int = 1024, logger=None):
        self.model_name = model_name
        self.system_prompt = system_prompt
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.logger = logger or logging.getLogger(__name__)

    def describe_image_parameters(self):
        return "However, the current model does NOT support image generation."

    async def query(self, messages: List[Message], system_prompt=None, validate_func=None, as_json=False) -> str:
        temperature = self.temperature
        max_attempts = int((1.0 - temperature) / 0.1) + 1  # Calculate max attempts to reach t=1.0

        if system_prompt is None:
            system_prompt = self.system_prompt

        for attempt in range(max_attempts):
            if attempt > 0:
                self.logger.info(f"Querying model (Attempt {attempt + 1}, Temperature: {temperature})")
            self.logger.info(f"User message: {messages[-1].content}")

            text_response = await self.chat_completion(messages, self.model_name, temperature, self.max_tokens, system_prompt)
            response_time = datetime.now(tz=timezone.utc)
            self.logger.info(f"Model response: {text_response}")

            valid = True
            if as_json:
                # Some models surround JSON with triple backticks
                if text_response.startswith('```json'):
                    text_response = text_response[7:]
                text_response = text_response.strip('`\n\t ')
                try:
                    response = json.loads(text_response, strict=False)
                except json.JSONDecodeError:
                    valid = False
            else:
                response = text_response

            if valid and validate_func is not None and not validate_func(response):
                valid = False

            if valid:
                messages.append(AssistantMessage(text_response, timestamp=response_time))
                return response

            temperature = min(temperature + 0.1, 1.0)
            self.logger.warning(f"Response: {text_response}")
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
    async def chat_completion(self, messages: List[Message], model: str, temperature: float, max_tokens: int, system_prompt: str) -> str:
        """
        Send a message to the model and get a response.
        """
        pass


class OllamaModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 5000, logger=None):
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)

    @staticmethod
    async def chat_completion(messages: List[Message], model: str, temperature: float, max_tokens: int, system_prompt: str) -> Tuple[str, List[Dict[str, str]]]:
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

        return text_response


class AnthropicModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "claude-opus":
            model_name = "claude-3-opus-20240229" # $15/$75
        elif model_name == "claude-sonnet":
            model_name = "claude-3-5-sonnet-20241022" # $3/$15
        elif model_name == "claude-haiku":
            model_name = "claude-3-5-haiku-20241022" # $1/$5
        else: # Default to sonnet model
            model_name = "claude-3-5-sonnet-20241022"
            print("Invalid model specified. Defaulting to claude-3-5-sonnet-20241022.")
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        self.client = anthropic.AsyncAnthropic()

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

    async def chat_completion(self, messages=[], model='claude-3-haiku-20240307', temperature=0.0, max_tokens=1024, system_prompt=""):
        # Check if the first message is a system message - if it is, we need to not pass it to Anthropic
        clean_messages = [await self.encode_message(message) for message in messages if message.role != Role.SYSTEM]

        try:
            chat_completion = await self.client.messages.create(
                system=system_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=clean_messages
            )

            text_response = chat_completion.content[0].text
            return text_response

        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e)


class OpenAIModel(Model):
    def __init__(self, model_name: str, image_model_name: Optional[str] = None, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
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
        if image_model_name is None:
            image_model_name = "dall-e-2"
        self.image_model_name = image_model_name
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        self.client = AsyncOpenAI()

    def describe_image_parameters(self):
        if self.image_model_name == "dall-e-2":
            return 'The image model is DALL·E 2, which requires a "prompt" string ' \
                   'describing the desired ' \
                   'image in exquisite detail, a "size" string with the desired ' \
                   'size which MUST be one of "256x256", "512x512" or "1024x1024", ' \
                   '(if the user explicitly specifies a different size, choose the ' \
                   'closest size and inform them)'
        elif self.image_model_name == "dall-e-3":
            return 'The image model is DALL·E 3, which requires a "prompt" string ' \
                   'describing the desired ' \
                   'image in exquisite detail, a "size" string with the desired ' \
                   'size which MUST be one of "1024x1024", "1024x1792" or "1792x1024" ' \
                   '(if the user explicitly specifies a different size, choose the ' \
                   'closest size and inform them), a "quality" string which must be ' \
                   '"standard" except if the user explicitly requested a high ' \
                   'quality image in which case it should be "hd", and a "style" ' \
                   'string which should be "natural" for a natural-looking image ' \
                   'or "vivid" for a hyper-real, dramatic image.'
        else:
            return f'Actually, image generation DOES NOT WORK! Let them know that the configuration specifies the invalid model "{self.image_model_name}".'

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

    async def chat_completion(self, messages=[], model='gpt-4o-mini-2024-07-18', temperature=0.0, max_tokens=1024, system_prompt=""):
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
            return text_response

        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e)

    async def generate_image(self, prompt: str, size: str, quality: str = "standard", style: str = "natural"):
        response = await self.client.images.generate(
            model=self.image_model_name,
            prompt=prompt,
            size=size,
            quality=quality,
            style=style,
            n=1)

        return Attachment(response.data[0].url, "image/png")


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

    async def chat_completion(self, messages=[], model='meta-llama/llama-3.1-405b-instruct', temperature=0.0, max_tokens=1024, system_prompt=""):
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
            return text_response

        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e)
