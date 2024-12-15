import sys, os
from contextlib import contextmanager
import argparse
import asyncio

from lib.assistant import Assistant
from lib.bot import Bot
from lib.msgtypes import UserMessage


def run_local(session):
    response = session.get_last_assistant_response()
    if response:
        print(response.get("chat") or response.get("react") or "(no response)")

    while True:
        message = UserMessage(input("> "))
        response = asyncio.run(session.chat(message))
        print("\n" + (response.get("chat") or response.get("react") or "(no response)"))


def run_discord_bot(assistant, session, token):
    bot = Bot(session)
    bot.run(token)


@contextmanager
def pidfile(path):
    with open(path, 'w') as pidfile:
        pidfile.write(str(os.getpid()))

    try:
        yield
    finally:
        if os.path.isfile(path):
            os.unlink(path)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-d", dest="daemonize", action="store_true", help="daemonize the process (runs in the background)")
    parser.add_argument("--date", help="make or continue the session for a give date (in YYYY-MM-DD format)")
    parser.add_argument("assistant", help="name of the .toml file of the assistant to run, without .toml extension", default='naiser')
    args = parser.parse_args()

    assistant = Assistant.load(args.assistant)

    # Make sure there isn't already an instance running of this assistant
    pidfile_path = os.path.abspath(f"{assistant.id}.pid")
    if os.path.isfile(pidfile_path):
        with open(pidfile_path) as pidf:
            pid = pidf.read()
        print(f"Assistant {args.assistant} is already running (pid={pid}). Delete {pidfile_path} if this is not the case")
        sys.exit(1)

    with pidfile(pidfile_path):
        session = asyncio.run(assistant.load_session(date.fromisoformat(args.date) if args.date else assistant.get_today()))

    token = os.environ.get('DISCORD_TOKEN')

    if token and assistant.discord_config:
        if args.daemonize:
            import daemon
            print("Spawning daemon.")

            with daemon.DaemonContext():
                with pidfile(pidfile_path):
                    run_discord_bot(assistant, session, token=token)
        else:
            with pidfile(pidfile_path):
                run_discord_bot(assistant, session, token=token)

    elif args.daemonize:
        print("No Discord configuration; cannot run as daemon.")
        sys.exit(1)
    else:
        print("No Discord configuration; running locally.")
        run_local(session)


if __name__ == '__main__':
    main()
