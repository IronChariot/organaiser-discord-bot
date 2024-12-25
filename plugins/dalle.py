import asyncio
from openai import AsyncOpenAI

from lib.plugin import Plugin, hook, action, system_prompt
from lib.msgtypes import Attachment
from lib import models


class DallEPlugin(Plugin):
    """Allows the assistant to generate images using DALL·E."""

    @hook('configure')
    async def on_configure(self, config):
        self.model_name = config.get('model', 'dall-e-2')

        if isinstance(self.assistant.model, models.OpenAIModel):
            self.client = self.assistant.model.client
        else:
            self.client = AsyncOpenAI()

    @system_prompt
    def on_system_prompt(self, session):
        text = 'If it is necessary to show the user an image, you MAY include ' \
               'a key called "images", containing a list of JSON objects each ' \
               'describing an image to be generated.'

        if self.model_name == 'dall-e-2':
            return f'{text} The image model is DALL·E 2, which requires a ' \
               '"prompt" string describing the desired ' \
               'image in exquisite detail, a "size" string with the desired ' \
               'size which MUST be one of "256x256", "512x512" or "1024x1024" ' \
               '(if the user explicitly specifies a different size, choose the ' \
               'closest size and inform them).'

        elif self.model_name == 'dall-e-3':
            return f'{text} The image model is DALL·E 3, which requires a ' \
               '"prompt" string describing the desired ' \
               'image in exquisite detail, a "size" string with the desired ' \
               'size which MUST be one of "1024x1024", "1024x1792" or "1792x1024" ' \
               '(if the user explicitly specifies a different size, choose the ' \
               'closest size and inform them), a "quality" string which must be ' \
               '"standard" except if the user explicitly requested a high ' \
               'quality image in which case it should be "hd", and a "style" ' \
               'string which should be "natural" for a natural-looking image ' \
               'or "vivid" for a hyper-real, dramatic image.'

        else:
            return f'Image generation DOES NOT WORK! Let them know that the configuration specifies the invalid model "{self.model_name}".'

    @action('images')
    async def on_generate_images(self, response, *, images=[]):
        for fut in asyncio.as_completed(self.generate_image(**image) for image in images):
            response.attach(await fut)

    async def generate_image(self, prompt: str, size: str, quality: str = "standard", style: str = "natural"):
        response = await self.client.images.generate(
            model=self.model_name,
            prompt=prompt,
            size=size,
            quality=quality,
            style=style,
            n=1)

        return Attachment(response.data[0].url, "image/png")
