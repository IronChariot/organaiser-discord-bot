from lib.assistant import Assistant
import sys


if __name__ == '__main__':
    ass = Assistant.load('cosmo')
    sess = ass.load_existing_session(ass.get_today())
    if sess is None:
        print("No session for today")
        sys.exit(1)

    for msg in sess.message_history:
        print('-----', msg.role, '-----')
        print(msg.content)
