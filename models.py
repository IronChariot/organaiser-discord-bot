import requests
import json
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Tuple
import anthropic
from openai import OpenAI

class Model(ABC):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0, max_tokens: int = 1024, logger=None):
        self.model_name = model_name
        self.system_prompt = system_prompt
        self.messages: List[Dict[str, str]] = []
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.logger = logger or logging.getLogger(__name__)

    def query(self, user_message: str, validate_func) -> str:
        original_temp = self.temperature
        max_attempts = int((1.0 - original_temp) / 0.1) + 1  # Calculate max attempts to reach t=1.0

        for attempt in range(max_attempts):
            if attempt > 0:
                self.logger.info(f"Querying model (Attempt {attempt + 1}, Temperature: {self.temperature})")
            self.logger.info(f"User message: {user_message}")

            response, self.messages = self.chat_completion(user_message, self.messages, self.model_name, self.temperature, self.max_tokens, self.system_prompt)

            self.logger.info(f"Model response: {response}")

            if validate_func(response):
                self.temperature = original_temp
                return response

            self.temperature = min(self.temperature + 0.1, 1.0)
            # Remove the last two messages from self.messages
            if len(self.messages) > 1:
                self.messages.pop()
                self.messages.pop()
            self.logger.warning(f"Invalid response. Increasing temperature to {self.temperature}")

        # If we've reached this point, even t=1.0 didn't work
        self.logger.error("Failed to get a valid response even at maximum temperature.")
        raise ValueError("Failed to get a valid response from the model")

    def reset_conversation(self) -> None:
        """
        Reset the conversation history.
        """
        self.messages = []

    def chat_completion(self):
        """
        Get a chat completion from the model.
        """
        pass

    def test_system_prompt(self) -> str:
        test_message = "Please explain the rules of the game we're about to play."
        response, _ = self.chat_completion(test_message, [], self.model_name, self.temperature, self.max_tokens, self.system_prompt)
        return response

class OllamaModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 5000, logger=None):
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)

    @staticmethod
    def chat_completion(user_message: str, messages: List[Dict[str, str]], model: str, temperature: float, max_tokens: int, system_prompt: str) -> Tuple[str, List[Dict[str, str]]]:
        messages.append({"role": "user", "content": user_message})

        if system_prompt and messages[0]["role"] != "system":
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
            messages.append({"role": "assistant", "content": text_response})
        else:
            text_response = f"Error: {response.status_code}"

        return text_response, messages
    
class AnthropicModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "opus":
            model_name = "claude-3-opus-20240229" # $15/$75
        elif model_name == "sonnet":
            model_name = "claude-3-5-sonnet-20241022" # $3/$15
        elif model_name == "haiku":
            model_name = "claude-3-haiku-20240307" # $0.25/1.25
        else: # Default to sonnet model
            model_name = "claude-3-5-sonnet-20241022"
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
    
    @staticmethod
    def chat_completion(user_message, messages=[], model='claude-3-haiku-20240307', temperature=0.0, max_tokens=1024, system_prompt=""):
        try:
            messages.append({"role": "user", "content": user_message})
            chat_completion = anthropic.Anthropic().messages.create(
                system=system_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=messages
            )

            text_response = chat_completion.content[0].text
            messages.append({"role": "assistant", "content": text_response})
            return text_response, messages
        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e), messages

class OpenAIModel(Model):
    def __init__(self, model_name: str, system_prompt: str = "", temperature: float = 0.0, max_tokens: int = 4000, logger=None):
        if model_name == "gpt-4o":
            model_name = "gpt-4o-2024-08-06" # $2.5/$10
        elif model_name == "gpt-4o-mini":
            model_name = "gpt-4o-mini-2024-07-18" # $0.15/$0.6
        elif model_name == "gpt-4":
            model_name = "gpt-4-turbo-2024-04-09" # $10/$30
        elif model_name == "gpt-3.5":
            model_name = "gpt-3.5-turbo-0125" # $0.5/$1.5
        else:
            model_name = "gpt-4o-mini-2024-07-18"  # Default to GPT-4o Mini
        super().__init__(model_name, system_prompt, temperature, max_tokens, logger)
        self.client = OpenAI()
    
    def chat_completion(self, user_message, messages=[], model='gpt-4o-mini-2024-07-18', temperature=0.0, max_tokens=1024, system_prompt=""):
        try:
            messages.append({"role": "user", "content": user_message})

            if system_prompt and messages[0]["role"] != "system":
                messages.insert(0, {"role": "system", "content": system_prompt})
                
            chat_completion = self.client.chat.completions.create(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens
            )

            text_response = chat_completion.choices[0].message.content
            messages.append({"role": "assistant", "content": text_response})
            return text_response, messages
        except Exception as e:
            print("Error: " + str(e))
            return "Error querying the LLM: " + str(e), messages