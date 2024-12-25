import json
import asyncio

EMOJI_MODIFIERS = '\ufe0f\ufe0e\U0001f3fb\U0001f3fc\U0001f3fd\U0001f3fe\U0001f3ff' \
                +  ''.join(chr(i) for i in range(0xe0020, 0xe0080))


def split_message(msg, max_length=2000):
    if len(msg) <= max_length:
        return [msg]

    parts = msg.split('\n\n')
    result = []
    for part in parts:
        if len(part) > max_length:
            # Have to split this further
            new_lines = []
            for line in part.split('\n'):
                if len(line) > max_length:
                    # Split it even further, based on spaces.
                    new_words = []
                    for word in line.split(' '):
                        if new_words and len(new_words[-1]) + len(word) + 1 <= max_length:
                            new_words[-1] += ' ' + word
                        else:
                            new_words.append(word)

                    line = new_words.pop()
                    new_lines += new_words

                if new_lines and len(new_lines[-1]) + len(line) + 1 <= max_length:
                    new_lines[-1] += '\n' + line
                else:
                    new_lines.append(line)
            result += new_lines

        elif result and len(result[-1]) + len(part) + 2 <= max_length:
            result[-1] += '\n\n' + part

        else:
            result.append(part)

    return result


def split_emoji(string):
    # Trick to get rid of surrogate pairs
    string = string.encode('utf-16', 'surrogatepass').decode('utf-16')

    i = 0
    while i < len(string):
        char = string[i]
        i += 1

        if char.isspace():
            continue

        # Flag consists of two characters
        if 0x1f1e6 <= ord(char) <= 0x1f1ff:
            if i < len(string) and 0x1f1e6 <= ord(string[i]) <= 0x1f1ff:
                char += string[i]
                i += 1

        # Tack on variant selector, skin types and region tag
        while i < len(string) and string[i] in EMOJI_MODIFIERS:
            char += string[i]
            i += 1

        # Check for joiners
        while i + 1 < len(string) and string[i] == '\u200d':
            char += string[i:i+2]
            i += 2

            # Could be extra variant selectors?
            while i < len(string) and string[i] in EMOJI_MODIFIERS:
                char += string[i]
                i += 1

        yield char


def format_json_md(data):
    code = json.dumps(data, indent=4).replace('```', '\\u0060\\u0060\\u0060')
    return f'```json\n{code}\n```'


class Condition:
    """Cheap async condition variable."""

    _future = None

    def notify_all(self):
        old_fut = self._future
        self._future = None
        if old_fut is not None:
            old_fut.set_result(None)

    def wait(self):
        fut = self._future
        if not fut:
            fut = asyncio.Future()
            self._future = fut

        # Cancelling the return value shouldn't cause others to wake up
        return asyncio.shield(fut)

    def __await__(self):
        return self.wait().__await__()
