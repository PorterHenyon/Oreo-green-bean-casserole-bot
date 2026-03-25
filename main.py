import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


load_dotenv()


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class OreoBot(commands.Bot):
    def __init__(self, *, guild_id: int):
        intents = discord.Intents.default()
        intents.message_content = True  # required to read message content from history

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.guild_id = guild_id
        self.initial_extensions = [
            "src.cogs.admin",
        ]

    async def setup_hook(self) -> None:
        for ext in self.initial_extensions:
            await self.load_extension(ext)

        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self) -> None:
        assert self.user is not None
        print(f"Logged in as {self.user} (id={self.user.id})")


async def main() -> None:
    token = _require_env("DISCORD_TOKEN")
    guild_id = int(_require_env("GUILD_ID"))

    bot = OreoBot(guild_id=guild_id)
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
