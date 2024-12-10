from lib.assistant import Assistant

if __name__ == '__main__':
    ass = Assistant.load('cosmo')
    sess = ass.load_session(ass.get_today())

    for msg in sess.message_history:
        print('-----', msg['role'], '-----')
        print(msg['content'])
