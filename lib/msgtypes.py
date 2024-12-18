from enum import Enum
import json
import aiohttp
from datetime import datetime, timezone


http_session = None

class Role(Enum):
    SYSTEM = 'system'
    USER = 'user'
    ASSISTANT = 'assistant'


class Channel(Enum):
    CHAT = 'chat'
    LOG = 'log'
    DIARY = 'diary'
    QUERY = 'query'
    BUGS = 'bugs'


class Attachment:
    def __init__(self, url, content_type, id=None):
        self.content_type = content_type
        self.url = url
        self.id = None

        self.__cached_data = None

    async def read(self):
        if self.__cached_data is not None:
            return self.__cached_data

        if self.url.startswith('file://'):
            data = open(self.url[7:], 'rb').read()
            self.__cached_data = data
            return data

        global http_session
        if not http_session:
            http_session = aiohttp.ClientSession(raise_for_status=True)

        headers = {'Accept': self.content_type}

        async with http_session.get(self.url, headers=headers) as response:
            content_type = response.headers.get('Content-Type', '').split(';')[0]
            data = await response.read()
            self.__cached_data = data
            return data


class Message:
    __slots__ = 'content', 'id', 'attachments', 'timestamp'

    def __init__(self, content: str, id=None, timestamp=None):
        assert hasattr(self, 'role')
        self.content = content
        self.id = id
        self.attachments = []
        self.timestamp = timestamp

    def attach(self, url, content_type):
        self.attachments.append(Attachment(url, content_type))

    def parse_json(self):
        return json.loads(self.content, strict=False)

    def dump(self, file):
        obj = {"role": self.role.value, "content": self.content}
        if self.id is not None:
            obj["id"] = self.id
        if self.timestamp:
            obj["timestamp"] = int(self.timestamp.timestamp())
        if self.attachments:
            obj["attachments"] = [{"url": attach.url, "content_type": attach.content_type} for attach in self.attachments]

        file.write(json.dumps(obj) + "\n")

    def is_summary(self):
        return False
    
    def __str__(self):
        return f'{self.role.value}: {self.content}'
    
    def __repr__(self):
        return str(self)


class AssistantMessage(Message):
    role = Role.ASSISTANT

    def is_summary(self):
        return self.content.startswith("~~~")


class SystemMessage(Message):
    role = Role.SYSTEM


class UserMessage(Message):
    role = Role.USER


def parse_message(string):
    obj = json.loads(string)
    msg_id = obj.get("id")

    if obj["role"] == "system":
        msg = SystemMessage(obj["content"], id=msg_id)

    elif obj["role"] == "assistant":
        msg = AssistantMessage(obj["content"], id=msg_id)

    elif obj["role"] == "user":
        msg = UserMessage(obj["content"], id=msg_id)

    else:
        raise RuntimeError("encountered unexpected role")

    if obj.get("timestamp"):
        msg.timestamp = datetime.fromtimestamp(obj["timestamp"], tz=timezone.utc)

    for attach in obj.get("attachments", ()):
        msg.attachments.append(Attachment(attach["url"], attach["content_type"]))

    return msg
