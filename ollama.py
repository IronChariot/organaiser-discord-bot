# ollama runs on localhost:11434

import requests
import json

def chat_completion(model, messages=[], temperature=0.0, max_tokens=4080, system_prompt=""):
    if system_prompt != "":
        if messages.length == 0 or messages[0]["role"] != "system":
            messages = [{"role": "system", "content": system_prompt}] + messages

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
    
    response = requests.post(url, headers=headers, data=json.dumps(data), stream=False)

    return response

def get_available_models():
    url = "http://localhost:11434/api/tags"
    response = requests.get(url)
    if response.status_code == 200:
        models = response.json()['models']
        return [model['name'] for model in models]
    else:
        return ["Error fetching models"]