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
