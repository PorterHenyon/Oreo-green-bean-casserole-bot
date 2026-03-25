from __future__ import annotations

import asyncio
import io
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

OREOS_GUILD = discord.Object(id=1354116143950073896)
DB_PATH = Path("data/messages.db")


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


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Long timeout avoids "database is locked" when another export holds WAL briefly.
    conn = sqlite3.connect(str(DB_PATH), timeout=60.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS indexed_messages (
            message_id INTEGER PRIMARY KEY,
            channel_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            content TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_channel_author_msg
        ON indexed_messages(channel_id, author_id, message_id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS channel_sync_state (
            channel_id INTEGER PRIMARY KEY,
            last_message_id INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _normalize_message_text(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ").strip()


async def _sync_channel_index(
    conn: sqlite3.Connection, channel: discord.TextChannel | discord.Thread
) -> int:
    cur = conn.execute(
        "SELECT last_message_id FROM channel_sync_state WHERE channel_id = ?",
        (channel.id,),
    )
    row = cur.fetchone()
    after_obj = discord.Object(id=int(row[0])) if row else None

    scanned = 0
    highest_id = int(row[0]) if row else 0
    batch: list[tuple[int, int, int, str]] = []

    async for msg in channel.history(limit=None, oldest_first=True, after=after_obj):
        scanned += 1
        highest_id = max(highest_id, msg.id)
        content = _normalize_message_text(msg.content or "")
        if not content:
            continue
        batch.append((msg.id, channel.id, msg.author.id, content))

        if len(batch) >= 1000:
            conn.executemany(
                """
                INSERT OR REPLACE INTO indexed_messages(message_id, channel_id, author_id, content)
                VALUES(?, ?, ?, ?)
                """,
                batch,
            )
            batch.clear()

    if batch:
        conn.executemany(
            """
            INSERT OR REPLACE INTO indexed_messages(message_id, channel_id, author_id, content)
            VALUES(?, ?, ?, ?)
            """,
            batch,
        )

    if highest_id > 0:
        conn.execute(
            """
            INSERT INTO channel_sync_state(channel_id, last_message_id)
            VALUES(?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET last_message_id=excluded.last_message_id
            """,
            (channel.id, highest_id),
        )
    conn.commit()
    return scanned


async def _grab_member_messages(
    *,
    channel: discord.TextChannel | discord.Thread,
    member: discord.Member,
    count: int,
    db_lock: asyncio.Lock,
) -> GrabResult:
    # Fast path for limited exports: scan newest messages and stop early.
    if count > 0:
        scanned = 0
        lines: list[str] = []
        async for msg in channel.history(limit=None, oldest_first=False):
            scanned += 1
            if msg.author.id != member.id:
                continue
            content = _normalize_message_text(msg.content or "")
            if not content:
                continue
            lines.append(content)
            if len(lines) >= count:
                break

        lines.reverse()  # chronological in output
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        output_lines = [
            f"member_name={member}",
            f"member_id={member.id}",
            "",
            *lines,
        ]
        text = "\n".join(output_lines).strip() + "\n"
        filename = f"messages_{member.id}_{channel.id}_{now}.txt"
        return GrabResult(
            filename=filename,
            content=text.encode("utf-8", errors="replace"),
            matched=len(lines),
            scanned=scanned,
        )

    # count=0 path: full export via indexed store (serialize — concurrent runs caused DB locks).
    async with db_lock:
        conn = _get_db()
        try:
            scanned = await _sync_channel_index(conn, channel)
            cur = conn.execute(
                """
                SELECT content FROM indexed_messages
                WHERE channel_id = ? AND author_id = ?
                ORDER BY message_id ASC
                """,
                (channel.id, member.id),
            )
            lines = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    output_lines = [
        f"member_name={member}",
        f"member_id={member.id}",
        "",
        *lines,
    ]
    text = "\n".join(output_lines).strip() + "\n"
    filename = f"messages_{member.id}_{channel.id}_{now}.txt"
    return GrabResult(
        filename=filename,
        content=text.encode("utf-8", errors="replace"),
        matched=len(lines),
        scanned=scanned,
    )


class AdminCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._message_db_lock = asyncio.Lock()

    async def _shutdown_bot(self) -> None:
        # Small delay gives Discord time to receive the ack/followup.
        await asyncio.sleep(0.3)
        await self.bot.close()

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
                db_lock=self._message_db_lock,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to read message history in that channel.", ephemeral=False
            )
            return
        except sqlite3.OperationalError as e:
            await interaction.followup.send(
                "Database was busy (another export may be running). Wait a few seconds and try again. "
                f"Details: {e}",
                ephemeral=False,
            )
            return
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"Export failed: {type(e).__name__}: {e}",
                ephemeral=False,
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
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=False)

        try:
            await interaction.followup.send("Stopping bot...", ephemeral=True)
        except discord.HTTPException:
            # Even if reply fails/expired, still stop the process.
            pass

        asyncio.create_task(self._shutdown_bot())

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

        cause = getattr(error, "original", None)
        msg = f"{type(error).__name__}: {error}"
        if cause is not None:
            msg = f"{type(cause).__name__}: {cause}"

        if interaction.response.is_done():
            try:
                await interaction.followup.send(f"Error: {msg}", ephemeral=True)
            except discord.HTTPException:
                pass
        else:
            await interaction.response.send_message(f"Error: {msg}", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminCog(bot))

