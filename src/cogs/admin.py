from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


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
    scan_limit: int,
    include_attachments: bool,
) -> GrabResult:
    matched: list[discord.Message] = []
    scanned = 0

    async for msg in channel.history(limit=scan_limit, oldest_first=False):
        scanned += 1
        if msg.author.id != member.id:
            continue

        matched.append(msg)
        if len(matched) >= count:
            break

    matched.reverse()  # oldest -> newest in output

    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    header = [
        f"guild_id={getattr(channel.guild, 'id', 'unknown')}",
        f"channel_id={channel.id}",
        f"channel_name=#{getattr(channel, 'name', 'unknown')}",
        f"member_id={member.id}",
        f"member_tag={member}",
        f"requested_count={count}",
        f"scan_limit={scan_limit}",
        f"scanned={scanned}",
        f"matched={len(matched)}",
        f"generated_at_utc={now}",
        "",
        "---",
        "",
    ]
    lines.extend(header)

    for msg in matched:
        created = msg.created_at.replace(tzinfo=timezone.utc).isoformat()
        content = msg.clean_content or ""
        lines.append(f"[{created}] {msg.author} (id={msg.author.id})")
        if content:
            lines.append(content)
        else:
            lines.append("(no text content)")

        if include_attachments and msg.attachments:
            lines.append("")
            lines.append("attachments:")
            for a in msg.attachments:
                lines.append(f"- {a.filename} ({a.content_type or 'unknown'}) {a.url}")

        if msg.embeds:
            lines.append("")
            lines.append(f"embeds: {len(msg.embeds)}")

        lines.append("")
        lines.append("---")
        lines.append("")

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
    @app_commands.check(_is_admin)
    @app_commands.describe(
        member="Member to export messages from",
        count="How many messages to export (1-500)",
        channel="Channel to scan (defaults to current channel)",
        scan_limit="How many recent messages to scan (defaults to count*50, max 5000)",
        include_attachments="Include attachment URLs in the export",
    )
    async def grabmessages(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        count: app_commands.Range[int, 1, 500],
        channel: Optional[discord.TextChannel] = None,
        scan_limit: Optional[app_commands.Range[int, 50, 5000]] = None,
        include_attachments: bool = True,
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

        effective_scan_limit = scan_limit or min(5000, max(50, count * 50))

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await _grab_member_messages(
                channel=target_channel,
                member=member,
                count=count,
                scan_limit=effective_scan_limit,
                include_attachments=include_attachments,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to read message history in that channel.", ephemeral=True
            )
            return

        file_obj = io.BytesIO(result.content)
        await interaction.followup.send(
            content=f"Exported **{result.matched}** messages (scanned {result.scanned}).",
            file=discord.File(fp=file_obj, filename=result.filename),
            ephemeral=True,
        )

    @app_commands.command(name="reload", description="Reload the bot's slash-command modules.")
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

