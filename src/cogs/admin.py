from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

OREOS_GUILD = discord.Object(id=1354116143950073896)


def _is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.administrator


@dataclass(frozen=True)
class GrabResult:
    filename: str
    content: bytes
    matched: int
    scanned: int


async def _grab_member_messages(
    *,
    channel: discord.TextChannel | discord.Thread,
    member: discord.Member,
    count: int,
) -> GrabResult:
    matched: list[discord.Message] = []
    scanned = 0

    # count=0 means collect all messages from this member in this channel.
    # For a limited count, iterate newest->oldest to finish faster.
    oldest_first = count == 0
    async for msg in channel.history(limit=None, oldest_first=oldest_first):
        scanned += 1
        if msg.author.id != member.id:
            continue

        matched.append(msg)
        if count > 0 and len(matched) >= count:
            break

    if count > 0:
        matched.reverse()  # keep output chronological

    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    for msg in matched:
        content = (msg.content or "").replace("\r", " ").replace("\n", " ").strip()
        if not content:
            continue
        lines.append(content)

    text = "\n".join(lines).strip() + "\n"
    filename = f"messages_{member.id}_{channel.id}_{now}.txt"
    return GrabResult(
        filename=filename,
        content=text.encode("utf-8", errors="replace"),
        matched=len(matched),
        scanned=scanned,
    )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="grabmessages",
        description="Export previous messages from a specific member into a .txt file.",
    )
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    @app_commands.describe(
        member="Member to export messages from",
        count="How many messages to export (0 = all)",
        channel="Channel to scan (defaults to current channel)",
    )
    async def grabmessages(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        count: app_commands.Range[int, 0, 100000],
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "I can only scan a text channel/thread.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            result = await _grab_member_messages(
                channel=target_channel,
                member=member,
                count=count,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to read message history in that channel.", ephemeral=False
            )
            return

        file_obj = io.BytesIO(result.content)
        await interaction.followup.send(
            content=f"Exported {result.matched} messages from {member.mention}.",
            file=discord.File(fp=file_obj, filename=result.filename),
            ephemeral=False,
        )

    @app_commands.command(name="reload", description="Reload the bot's slash-command modules.")
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    async def reload(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        failures: list[str] = []
        for ext in list(getattr(self.bot, "extensions", {}).keys()):
            try:
                await self.bot.reload_extension(ext)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{ext}: {type(e).__name__}: {e}")

        if failures:
            msg = "Some extensions failed to reload:\n" + "\n".join(f"- {f}" for f in failures)
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send("Reloaded successfully.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop the bot process.")
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    async def stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Stopping bot...", ephemeral=True)
        await self.bot.close()

    @grabmessages.error
    @reload.error
    @stop.error
    async def _on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            if interaction.response.is_done():
                await interaction.followup.send("Admin-only command.", ephemeral=True)
            else:
                await interaction.response.send_message("Admin-only command.", ephemeral=True)
            return

        if interaction.response.is_done():
            await interaction.followup.send(f"Error: {type(error).__name__}: {error}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {type(error).__name__}: {error}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

