from enum import Enum
import json


class Role(Enum):
    SYSTEM = 'system'
    USER = 'user'
    ASSISTANT = 'assistant'


class Message:
    __slots__ = 'content', 'id'

    def __init__(self, content: str, id=None):
        assert hasattr(self, 'role')
        self.content = content
        self.id = id

    def parse_json(self):
        return json.loads(self.content, strict=False)

    def dump(self, file):
        obj = {"role": self.role.value, "content": self.content}
        if self.id is not None:
            obj["id"] = self.id
        file.write(json.dumps(obj) + "\n")

    def is_summary(self):
        return False


class AssistantMessage(Message):
    role = Role.ASSISTANT

    def is_summary(self):
        return self.content.startswith("Summary of previous messages:")


class SystemMessage(Message):
    role = Role.SYSTEM


class UserMessage(Message):
    role = Role.USER


def parse_message(string):
    obj = json.loads(string)
    msg_id = obj.get("id")

    match obj["role"]:
        case "system":
            msg = SystemMessage(obj["content"], id=msg_id)

        case "assistant":
            msg = AssistantMessage(obj["content"], id=msg_id)

        case "user":
            msg = UserMessage(obj["content"], id=msg_id)

        case _:
            raise RuntimeError("encountered unexpected role")

    return msg
