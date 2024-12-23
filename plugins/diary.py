from lib.plugin import Plugin, hook, discord_command
from lib.msgtypes import Channel

import discord
import pathlib

DIARIES_DIR = pathlib.Path(__file__).parent.parent.resolve() / 'diaries'

DEFAULT_PROMPT = (
    "Write a diary entry about the day. Prefix it with a markdown header "
    "including the current date. Use proper capitalization for this post. What "
    "has he been up to today? How did he feel? How was his energy level? What "
    "went well and what went less well? Refer to the user in the third person.")


class DiaryPlugin(Plugin):
    @hook('configure')
    async def on_configure(self, config):
        self.prompt = config.get('prompt', DEFAULT_PROMPT)

    @hook('post_session_end')
    async def on_post_session_end(self, session):
        path = self._get_entry_path(session)
        if not path.exists():
            entry = await self._write_diary_entry(session)
            await self.send_message(entry, channel=Channel.DIARY)

    def _get_entry_path(self, session):
        return DIARIES_DIR / f'{self.assistant.id}-{session.date.isoformat()}.txt'

    async def _write_diary_entry(self, session, overwrite=True):
        path = self._get_entry_path(session)

        response = await session.isolated_query("SYSTEM: " + self.prompt)
        path.parent.mkdir(exist_ok=True)
        with open(path, 'w') as fh:
            fh.write(response)

        return response

    @discord_command(name="diary_entry_write",
                     description="Write out today's diary entry immediately.")
    async def on_discord_command(self, bot, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        session = bot.session
        entry = await self._write_diary_entry(session)
        print(self, entry, self.send_message)
        await self.send_message(entry, channel=Channel.DIARY)

        await interaction.followup.send(f'Wrote diary entry for {session.date}.', ephemeral=True)
