from lib.plugin import Plugin, hook, system_prompt
from lib import models

import json
from datetime import date
from dataclasses import dataclass


@dataclass
class Memory:
    id: int
    date: date
    title: str
    summary: str
    content: str
    labels: list

    def __str__(self):
        s = f'[ID: M{self.id:04d}] {self.title}: {self.summary}'

        for label in self.labels:
            s += f' #{label}'

        return s

    def __eq__(self, other):
        return self.id == other.id

    def __hash__(self):
        return self.id


class LongTermMemoryPlugin(Plugin):
    @system_prompt(dynamic=True)
    def dynamic_system_prompt(self, session):
        return ''.join(f'## Long-Term Memory M{memory.id:04}: {memory.title}\n{memory.content}\n\n' for memory in self.active_memories)

    @hook('init')
    async def on_init(self):
        self.memories_by_id = {}
        self.active_memories = set()
        self.pending_updates = set()

        with self.assistant.open_memory_file('ltm.json') as f:
            for mem_data in json.load(f):
                memory = Memory(id=mem_data['id'],
                                date=date.fromisoformat(mem_data['date']),
                                title=mem_data['title'],
                                summary=mem_data['summary'],
                                content=mem_data['content'],
                                labels=mem_data['labels'])
                self.memories_by_id[memory.id] = memory

    @hook('configure')
    async def on_configure(self, config):
        model_name = config.get('active_recall_model', 'gpt-4o-mini')
        self.model = models.create(model_name)
        self.max_active_memories = config.get('max_active_memories', 3)

    @hook('pre_query_assistant_response')
    async def on_pre_query_assistant_response(self, session):
        extra_prompt = '# Long-Term Memories\n\n'
        for memory in self.memories_by_id.values():
            extra_prompt += f' - {memory}\n'

        prompt = f"SYSTEM: Respond with a JSON list (and nothing else) containing the IDs of up to {self.max_active_memories} of the long-term memories that are most relevant to the current conversation. If none are relevant, respond with an empty list."

        result = await session.isolated_query(prompt, format_prompt=extra_prompt, return_type=list)
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

    @hook('post_session_end')
    async def on_post_session_end(self, session):
        pass
