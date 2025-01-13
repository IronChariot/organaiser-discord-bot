from lib.plugin import Plugin, hook, system_prompt
from lib.msgtypes import Channel
from lib import models

import json
import asyncio
from datetime import date, datetime
from dataclasses import dataclass
import re
import discord


@dataclass
class Memory:
    id: int
    date: date
    title: str
    summary: str
    content: str
    labels: list
    commits: list
    message_id: int = None

    def __str__(self):
        s = f'[ID: M{self.id:04d}] {self.title}: {self.summary}'

        for label in self.labels:
            s += f' #{label}'

        return s

    def __eq__(self, other):
        return self.id == other.id

    def __hash__(self):
        return self.id

    def asdict(self):
        result = {
            "id": self.id,
            "date": self.date.isoformat(),
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "labels": self.labels,
            "commits": self.commits
        }
        if self.message_id:
            result["message_id"] = self.message_id
        return result


class LongTermMemoryPlugin(Plugin):
    @system_prompt(dynamic=True)
    def dynamic_system_prompt(self, session):
        return ''.join(f'## Long-Term Memory M{memory.id:04}: {memory.title}\n{memory.content}\n\n' for memory in self.active_memories)

    @hook('init')
    async def on_init(self):
        self.memories_by_id = {}
        self.active_memories = set()
        self.pending_updates = set()
        self.next_id = 1

        with self.assistant.open_memory_file('ltm.json') as f:
            for mem_data in json.load(f):
                memory = Memory(id=mem_data['id'],
                                date=date.fromisoformat(mem_data['date']),
                                title=mem_data['title'],
                                summary=mem_data['summary'],
                                content=mem_data['content'],
                                labels=mem_data['labels'],
                                commits=mem_data.get('commits', []),
                                message_id=int(mem_data.get('message_id') or 0) or None)
                self.memories_by_id[memory.id] = memory
                if memory.id >= self.next_id:
                    self.next_id = memory.id + 1

    @hook('configure')
    async def on_configure(self, config):
        model_name = config.get('recall_model')
        self.recall_model = models.create(model_name) if model_name else self.assistant.model

        model_name = config.get('update_model')
        self.update_model = models.create(model_name) if model_name else self.assistant.model

        self.max_active_memories = config.get('max_active_memories', 3)
        self.discord_channel_id = config.get('discord_channel') or None

    @hook('pre_query_assistant_response')
    async def on_pre_query_assistant_response(self, session):
        extra_prompt = '# Long-Term Memories\n\n'
        for memory in self.memories_by_id.values():
            extra_prompt += f' - {memory}\n'

        prompt = f"SYSTEM: Respond with a JSON list (and nothing else) containing the IDs of up to {self.max_active_memories} of the long-term memories that are most relevant to the current conversation. If none are relevant, respond with an empty list."

        result = await session.isolated_query(prompt, format_prompt=extra_prompt, return_type=list, model=self.recall_model)
        if isinstance(result, dict):
            if 'relevant_memories' in result:
                result = result['relevant_memories']
            elif len(result) == 1:
                result = next(iter(result.values()))

        self.active_memories.clear()
        for mem_id in result or ():
            if isinstance(mem_id, dict):
                mem_id = mem_id.get("ID") or mem_id["id"]

            # "M0001" -> 1
            mem_id = int(mem_id.lstrip('M0'))

            memory = self.memories_by_id.get(mem_id)
            if memory:
                self.active_memories.add(memory)

        if not self.active_memories:
            asyncio.create_task(self.send_message('No memories activated.', channel=Channel.LOG))
        else:
            str_memories = []
            for memory in self.active_memories:
                str_memories.append(f' - M{memory.id:04}: {memory.title}')
            str_memories = '\n'.join(str_memories)
            asyncio.create_task(self.send_message(f'Active memories: \n{str_memories}', channel=Channel.LOG))

    @hook('post_session_end')
    async def on_post_session_end(self, session):
        self.schedule(None, self.update_memories(session))

    async def update_memories(self, session):
        print("Updating memories")
        extra_prompt = ''

        if self.memories_by_id:
            extra_prompt = '# Long-Term Memories\n\n'

            for memory in self.memories_by_id.values():
                extra_prompt += f'{memory}\n'

        update_ids = await session.isolated_query("SYSTEM: Are there any long-term memories in your list you wish to update based on today's chat? Respond with a JSON list of the identifiers of the memories that you wish to update. It's okay to return an empty list if nothing needs to be updated.", format_prompt=extra_prompt, return_type=list, model=self.update_model)
        if not update_ids:
            update_ids = []
        elif isinstance(update_ids, dict):
            # Disobedience proofing
            if 'memories_to_update' in update_ids:
                update_ids = update_ids['memories_to_update']
            elif len(update_ids) == 1:
                update_ids = next(iter(update_ids.values()))
            else:
                print("Invalid response")
                print(json.dumps(update_ids, indent=4))
                return

        update_memories = []
        for update_id in update_ids:
            update_id = int(update_id.lstrip('M'), 10)

            memory = self.memories_by_id.get(update_id)
            if memory:
                update_memories.append(memory)

        update_queries = []
        for memory in update_memories:
            old_data = {
                "id": memory.id,
                "date": memory.date.isoformat(),
                "title": memory.title,
                "summary": memory.summary,
                "content": memory.content,
                "labels": memory.labels
            }
            old_json = json.dumps(old_data, indent=4)
            query = session.isolated_query(f"SYSTEM: Now follows the full definition of memory M{update_id:04d}. Return this same JSON object, but appropriately modified if you have learned new information about this topic. Also include a \"commit_message\" string in that same object explaining what has changed. Remember, the memory content should be removed from time, make sure that it still makes sense when read on any date in the future, and important events should include a date in the text.\n{old_json}", format_prompt=extra_prompt, return_type=dict, model=self.update_model)
            update_queries.append(query)

        results = await session.assistant.model.batch(*update_queries)

        for memory, result in zip(update_memories, results):
            if 'title' in result:
                memory.title = result['title']
            if 'summary' in result:
                memory.summary = result['summary']
            if 'content' in result:
                memory.content = result['content']
            if 'labels' in result:
                memory.labels = result['labels']
            if 'commit_message' in result:
                memory.commits.append({'date': session.date.isoformat(), 'message': result['commit_message']})
            await self.update_memory(memory)

        if update_memories:
            updated_str = ', '.join(f'M{memory.id:04d} ({memory.title})' for memory in update_memories)
            if len(update_ids) == 1:
                updated_msg = ' You have just updated memory ' + updated_str + '.'
            else:
                updated_msg = ' You have just updated memories ' + updated_str + '.'
        else:
            updated_msg = ''

        query = session.isolated_query(f"SYSTEM:{updated_msg} Besides that, are there any new things, unrelated to these or any other existing memories, that you wish to commit to long term memory for later recall, for example detailing the current state of a project or pursuit, or a particularly significant conversation? Return a JSON list of memories, each memory being a JSON object with a \"title\" key, a brief \"summary\", the full \"content\" (be detailed!) and a list of \"labels\". Memories are not for diary entries, so do not create a memory just describing the day or energy patterns. It's okay to not create any memories if there are no new things unrelated to existing memories. If you mention particular events, include a date on which that event occurred in the text. The memory content should be removed from time, make sure that it still makes sense when read on any date in the future. Be very, very thorough to make sure you've included all the important details that were brought up today regarding this topic.", format_prompt=extra_prompt, return_type=list, model=self.update_model)
        new_memories, = await session.assistant.model.batch(query)
        if not new_memories:
            new_memories = []
        elif isinstance(new_memories, dict):
            if 'new_memories' in new_memories:
                new_memories = new_memories['new_memories']
            elif len(new_memories) == 1:
                new_memories = next(iter(new_memories.values()))
            else:
                print("Invalid response")
                print(json.dumps(new_memories, indent=4))
                return

        for result in new_memories:
            memory = Memory(
                id=self.next_id,
                date=session.date,
                title=result['title'],
                summary=result['summary'],
                content=result['content'],
                labels=result.get('labels') or [],
                commits=[])
            self.next_id += 1

            self.memories_by_id[memory.id] = memory
            await self.update_memory(memory)

        print(f"Finished updating memories (added {len(new_memories)} new, updated {update_memories})")

    @hook('discord_setup')
    async def on_discord_setup(self, client):
        client.add_dynamic_items(EditMemoryButton, DeleteMemoryButton)

    @hook('discord_ready')
    async def on_discord_ready(self, client):
        self.discord_channel = client.get_channel(self.discord_channel_id) or await client.fetch_channel(self.discord_channel_id)

        if self.discord_channel:
            for memory in self.memories_by_id.values():
                if not memory.message_id:
                    await self.update_memory(memory)

    async def update_memory(self, memory):
        if not self.discord_channel:
            return

        message = None
        if memory.message_id:
            message = await self.discord_channel.fetch_message(memory.message_id)

        content = f"## {memory.title}\n{memory.summary}"
        content += f"\n-# M{memory.id:04} created {memory.date.isoformat()}"
        if memory.commits:
            last_commit = memory.commits[-1]
            content += f", last updated {last_commit['date']}"

        if message:
            await message.edit(content=content)
        else:
            view = discord.ui.View(timeout=None)
            view.add_item(EditMemoryButton(self, memory))
            view.add_item(DeleteMemoryButton(self, memory))
            message = await self.discord_channel.send(content, view=view)
            if message:
                memory.message_id = message.id

        self.save_memories()

    async def delete_memory(self, memory):
        if memory.id in self.memories_by_id:
            del self.memories_by_id[memory.id]

        self.active_memories.discard(memory)
        self.save_memories()

        if not memory.message_id:
            return

        message = await self.discord_channel.fetch_message(memory.message_id)
        if not message:
            return

        await message.delete()

    def save_memories(self):
        mems_encoded = [mem.asdict() for mem in self.memories_by_id.values()]

        with self.assistant.open_memory_file('ltm.json', 'w') as f:
            json.dump(mems_encoded, f, indent=4)


class EditMemoryModal(discord.ui.Modal, title='Edit Memory'):
    title_input = discord.ui.TextInput(
        label='Title',
        required=True,
    )

    summary_input = discord.ui.TextInput(
        label='Summary',
        required=True,
    )

    content_input = discord.ui.TextInput(
        label='Content',
        style=discord.TextStyle.paragraph,
        required=True,
    )

    labels_input = discord.ui.TextInput(
        label='Labels (comma-separated)',
        required=True,
    )

    commit_message_input = discord.ui.TextInput(
        label='Commit message (leave blank for no commit)',
        required=False,
    )

    def __init__(self, plugin, memory):
        super().__init__()
        self.plugin = plugin
        self.memory = memory
        self.title_input.default = memory.title
        self.summary_input.default = memory.summary
        self.content_input.default = memory.content
        self.labels_input.default = ', '.join(memory.labels)

    async def on_submit(self, interaction: discord.Interaction):
        memory = self.memory
        memory.title = self.title_input.value
        memory.summary = self.summary_input.value
        memory.content = self.content_input.value
        memory.labels = [label.strip() for label in self.labels_input.value.split(',')]

        commit_message = self.commit_message_input.value
        if commit_message:
            memory.commits.append({"date": date.today().isoformat(), "message": commit_message})

        await self.plugin.update_memory(memory)
        await interaction.response.send_message(f'Updated!', ephemeral=True, silent=True, delete_after=0.001)


class EditMemoryButton(discord.ui.DynamicItem[discord.ui.Button], template=r'ltm:edit:(?P<id>M[0-9]+)'):
    def __init__(self, plugin, memory) -> None:
        super().__init__(
            discord.ui.Button(
                label='Edit',
                style=discord.ButtonStyle.blurple,
                custom_id=f'ltm:edit:M{memory.id:04}',
            )
        )
        self.plugin = plugin
        self.memory = memory

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        plugin = interaction.client.assistant.plugins['ltm']
        memory_id = int(match['id'].lstrip('M0'))
        memory = plugin.memories_by_id[memory_id]
        return cls(plugin, memory)

    async def callback(self, interaction: discord.Interaction) -> None:
        modal = EditMemoryModal(self.plugin, self.memory)
        await interaction.response.send_modal(modal)


class DeleteMemoryButton(discord.ui.DynamicItem[discord.ui.Button], template=r'ltm:delete:(?P<id>M[0-9]+)'):
    def __init__(self, plugin, memory) -> None:
        super().__init__(
            discord.ui.Button(
                label='Delete',
                style=discord.ButtonStyle.secondary,
                custom_id=f'ltm:delete:M{memory.id:04}',
            )
        )
        self.plugin = plugin
        self.memory = memory

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        plugin = interaction.client.assistant.plugins['ltm']
        memory_id = int(match['id'].lstrip('M0'))
        memory = plugin.memories_by_id[memory_id]
        return cls(plugin, memory)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.plugin.delete_memory(self.memory)
