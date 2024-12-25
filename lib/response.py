import asyncio

from .msgtypes import Attachment
from .util import split_emoji


class AssistantResponse:
    def __init__(self, session, data):
        self.session = session
        self.raw_data = data

        self.chat = data.get('chat') or None
        self.reactions = list(split_emoji(data.get('react') or ''))
        self.bug_report = data.get('bug_report') or None
        self.prompt_after = data.get('prompt_after')
        self.actions_taken = []

        self._exceptions = []
        self._pending_actions = set()

        self.__attachments = []
        self.__attachment_waiter = None

    def run_action(self, action):
        """Runs the given action on the response in the background.  Result may
        be awaited."""

        data = self.raw_data
        task = asyncio.create_task(action(self, **{key: data.get(key) for key in action._action_keys}))
        self._pending_actions.add(task)

        task.add_done_callback(self.finish_action)
        return task

    def finish_action(self, task):
        """Called when the given action task has finished."""

        self._pending_actions.remove(task)

        exc = task.exception()
        if exc is not None:
            self._exceptions.append(exc)
        else:
            results = task.result()
            if results is None:
                results = ()
            elif isinstance(results, Attachment) or isinstance(results, str):
                results = [results]

            for result in results:
                if isinstance(result, Attachment):
                    self.attach(result)
                elif isinstance(result, str):
                    self.actions_taken.append(result)

        # Wake up any get_attachments consumers if this was the last task
        # to be finished.
        if not self._pending_actions:
            waiter = self.__attachment_waiter
            if waiter is not None:
                waiter.set_result(None)

    async def wait_for_actions(self):
        """Waits for all actions to be done.  Ignores exceptions."""

        while self._pending_actions:
            try:
                await next(iter(self._pending_actions))
            except:
                pass

    def attach(self, attachment: Attachment):
        """Adds an attachment to the response."""

        self.__attachments.append(attachment)

        # Wake up anything waiting in get_attachments()
        old_waiter = self.__attachment_waiter
        self.__attachment_waiter = None
        if old_waiter is not None:
            old_waiter.set_result(None)

    def __wait_for_attachment(self):
        """Wait for either another attachment to be added via attach() or
        for all action tasks to be finished.
        Returns a future that must be awaited."""

        waiter = self.__attachment_waiter
        if not waiter:
            waiter = asyncio.Future()
            self.__attachment_waiter = waiter

        return waiter

    async def get_attachments(self):
        """Asynchronously returns a list of attachments, in arbitrary order."""
        i = 0
        while i < len(self.__attachments):
            yield self.__attachments[i]
            i += 1

        while self._pending_actions:
            waiter = self.__wait_for_attachment()
            await waiter

            while i < len(self.__attachments):
                yield self.__attachments[i]
                i += 1

    async def read_attachments(self):
        """Asynchronously returns a pair of (attachment, data) objects, in
        arbitrary order."""

        async def read_attachment(attachment):
            return (attachment, await attachment.read())

        pending = []
        async for attachment in self.get_attachments():
            pending.append(asyncio.create_task(read_attachment(attachment)))

            done, pending = await asyncio.wait(pending, timeout=0)
            for task in done:
                yield task.result()

        if pending:
            for task in asyncio.as_completed(pending):
                yield await task
