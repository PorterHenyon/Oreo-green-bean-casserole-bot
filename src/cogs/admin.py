from __future__ import annotations

import asyncio
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


def _normalize_message_text(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").strip()


async def _grab_member_messages(
    *,
    channel: discord.TextChannel | discord.Thread,
    member: discord.Member,
    count: int,
) -> GrabResult:
    """Collect message text only. count=0 means entire channel history for that member."""
    matched: list[str] = []
    scanned = 0

    if count > 0:
        async for msg in channel.history(limit=None, oldest_first=False):
            scanned += 1
            if msg.author.id != member.id:
                continue
            content = _normalize_message_text(msg.content or "")
            if not content:
                continue
            matched.append(content)
            if len(matched) >= count:
                break
        matched.reverse()
    else:
        async for msg in channel.history(limit=None, oldest_first=True):
            scanned += 1
            if msg.author.id != member.id:
                continue
            content = _normalize_message_text(msg.content or "")
            if not content:
                continue
            matched.append(content)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_lines = [
        f"member_name={member}",
        f"member_id={member.id}",
        "",
        *matched,
    ]
    text = "\n".join(output_lines).strip() + "\n"
    filename = f"messages_{member.id}_{channel.id}_{now}.txt"
    return GrabResult(
        filename=filename,
        content=text.encode("utf-8", errors="replace"),
        matched=len(matched),
        scanned=scanned,
    )


async def _defer_public(interaction: discord.Interaction) -> None:
    if interaction.response.is_done():
        return
    try:
        await interaction.response.defer(ephemeral=False, thinking=True)
    except discord.HTTPException:
        pass


async def _followup_public(interaction: discord.Interaction, **kwargs: object) -> None:
    try:
        await interaction.followup.send(**kwargs)  # type: ignore[arg-type]
    except discord.HTTPException:
        pass


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="grabmessages",
        description="Export messages from a member in this channel (text only, one line per message).",
    )
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    @app_commands.describe(
        member="Member to export messages from",
        count="How many messages (0 = all in this channel)",
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
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This command can only be used in a server.", ephemeral=True
                )
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "I can only scan a text channel/thread.", ephemeral=True
                )
            return

        await _defer_public(interaction)

        try:
            result = await _grab_member_messages(
                channel=target_channel,
                member=member,
                count=count,
            )
        except discord.Forbidden:
            await _followup_public(
                interaction,
                content="I don't have permission to read message history in that channel.",
                ephemeral=False,
            )
            return
        except Exception as e:  # noqa: BLE001
            await _followup_public(
                interaction,
                content=f"Export failed: {type(e).__name__}: {e}",
                ephemeral=False,
            )
            return

        file_obj = io.BytesIO(result.content)
        await _followup_public(
            interaction,
            content=f"Exported {result.matched} messages from {member.mention}.",
            file=discord.File(fp=file_obj, filename=result.filename),
            ephemeral=False,
        )

    @app_commands.command(name="reload", description="Reload bot modules.")
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    async def reload(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer(ephemeral=True, thinking=True)
            except discord.HTTPException:
                pass

        failures: list[str] = []
        for ext in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(ext)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{ext}: {type(e).__name__}: {e}")

        msg = (
            "Some extensions failed to reload:\n" + "\n".join(f"- {f}" for f in failures)
            if failures
            else "Reloaded successfully."
        )
        try:
            await interaction.followup.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    @app_commands.command(name="stop", description="Stop the bot (disconnect and exit process).")
    @app_commands.guilds(OREOS_GUILD)
    @app_commands.check(_is_admin)
    async def stop(self, interaction: discord.Interaction) -> None:
        # Acknowledge first so Discord never hangs on "loading"
        if not interaction.response.is_done():
            try:
                await interaction.response.send_message("Stopping bot...", ephemeral=True)
            except discord.HTTPException:
                try:
                    await interaction.response.defer(ephemeral=True, thinking=False)
                    await interaction.followup.send("Stopping bot...", ephemeral=True)
                except discord.HTTPException:
                    pass

        await asyncio.sleep(0.4)
        await self.bot.close()

    @grabmessages.error
    @reload.error
    @stop.error
    async def _on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            try:
                if interaction.response.is_done():
                    await interaction.followup.send("Admin-only command.", ephemeral=True)
                else:
                    await interaction.response.send_message("Admin-only command.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        inner = error
        if isinstance(error, app_commands.CommandInvokeError) and error.original is not None:
            inner = error.original  # type: ignore[assignment]

        msg = f"{type(inner).__name__}: {inner}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"Error: {msg}", ephemeral=True)
            else:
                await interaction.response.send_message(f"Error: {msg}", ephemeral=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))
