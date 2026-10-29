"""
DM inbox - bot owners only.

    /inbox list [page]                          everyone the bot has DMs with (since this feature was added)
    /inbox view user [limit]                    the live conversation with someone, with message IDs
    /inbox send user message                    DM someone as the bot
    /inbox edit user message_id new_text        edit one of the bot's own messages (works on embeds too)
    /inbox delete user message_ids              delete the bot's own messages (IDs separated by commas or spaces)
    /inbox feed [channel]                       mirror every DM (in and out) into a channel; empty = turn off

Every command is refused for anyone who isn't on the owner list (permissions.is_owner).

What Discord allows: a bot can only edit and delete ITS OWN DMs - it can't touch the messages the member
sent. There's also no API to list a bot's DM channels, so /inbox list only knows people from the moment this cog
was added (plus anyone with a modmail thread); /inbox view works for any user ID because it reads the history live.

Every DM that reaches or leaves the bot is saved in the dm_log table of the bot's database (content + attachment
links) so it can be listed and mirrored. Edits and deletes are tracked too.
"""
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import database as db
import permissions

FEED_KEY = "dm_feed_channel"
PER_PAGE = 15
BLURPLE = discord.Color.blurple()


def _private(interaction: discord.Interaction) -> bool:
    return not getattr(interaction, "is_prefix", False)


def _clip(text: str, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _message_text(m: discord.Message) -> str:
    """Everything readable in a message: its text, embed text and attachment links."""
    parts = [m.content] if m.content else []
    for e in m.embeds:
        bits = [b for b in (e.title, e.description) if b]
        if bits:
            parts.append("[embed] " + " - ".join(bits))
    parts.extend(a.url for a in m.attachments)
    return "\n".join(parts) or "*(empty)*"


class DMInbox(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        db._run("""CREATE TABLE IF NOT EXISTS dm_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            user_name TEXT,
            direction TEXT NOT NULL,          -- in (member -> bot), out (bot -> member)
            content TEXT,
            attachments TEXT,                 -- JSON list of urls
            edited INTEGER NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )""")
        db._run("CREATE INDEX IF NOT EXISTS idx_dm_log_user ON dm_log (user_id)")
        db._run("CREATE INDEX IF NOT EXISTS idx_dm_log_msg ON dm_log (message_id)")

    # ---------------- logging ----------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is not None or not isinstance(message.channel, discord.DMChannel):
            return
        outgoing = message.author.id == self.bot.user.id
        other = message.channel.recipient
        if other is None:
            return
        direction = "out" if outgoing else "in"
        text = _message_text(message) if outgoing else (message.content or "")
        db._run("INSERT INTO dm_log (message_id, user_id, user_name, direction, content, attachments, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message.id, other.id, str(other), direction, text,
                 json.dumps([a.url for a in message.attachments]), message.created_at.timestamp()))
        await self._feed(other, direction, text, message.id, [a.url for a in message.attachments] if not outgoing else [])

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is not None:
            return
        content = payload.data.get("content")
        if content is None:
            return
        row = db._fetch("SELECT id FROM dm_log WHERE message_id=? AND direction='in'", (payload.message_id,))
        if row:
            db._run("UPDATE dm_log SET content=?, edited=1 WHERE id=?", (content, row["id"]))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is not None:
            return
        db._run("UPDATE dm_log SET deleted=1 WHERE message_id=?", (payload.message_id,))

    async def _feed(self, other: discord.abc.User, direction: str, text: str, message_id: int, files: list[str]):
        channel_id = db.get_setting(0, FEED_KEY)
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        embed = discord.Embed(description=(text or "*(empty)*")[:4000],
                              color=discord.Color.green() if direction == "out" else BLURPLE,
                              timestamp=discord.utils.utcnow())
        arrow = "➡️ Bot → " if direction == "out" else "⬅️ "
        embed.set_author(name=f"{arrow}{other} ({other.id})", icon_url=other.display_avatar.url)
        embed.set_footer(text=f"message ID {message_id}")
        if files:
            embed.add_field(name="Attachments", value="\n".join(files)[:1024], inline=False)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logging.warning("couldn't post to the DM feed channel")

    # ---------------- helpers ----------------

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if permissions.is_owner(interaction.user.id):
            return True
        await interaction.response.send_message("you dont have the perms", ephemeral=True)
        return False

    @staticmethod
    async def _dm(user: discord.abc.User) -> discord.DMChannel | None:
        try:
            return user.dm_channel or await user.create_dm()
        except discord.HTTPException:
            return None

    # ---------------- /inbox ----------------

    dm_group = app_commands.Group(name="inbox", description="[Owner] See, send, edit and delete the bot's DMs")

    @dm_group.command(name="list", description="Everyone the bot has DMs with")
    @app_commands.describe(page="Page number")
    async def dm_list(self, interaction: discord.Interaction, page: int = 1):
        if not await self._guard(interaction):
            return
        rows = db._fetch(
            "SELECT user_id, MAX(user_name) AS name, COUNT(*) AS n, MAX(created_at) AS last, "
            "SUM(direction='in') AS incoming FROM dm_log GROUP BY user_id ORDER BY last DESC", many=True)
        known = {r["user_id"] for r in rows}
        extra = [r["user_id"] for r in db._fetch("SELECT DISTINCT user_id FROM modmail_threads", many=True)
                 if r["user_id"] not in known]
        pages = max(1, -(-len(rows) // PER_PAGE))
        page = min(max(1, page), pages)
        lines = []
        for r in rows[(page - 1) * PER_PAGE: page * PER_PAGE]:
            when = discord.utils.format_dt(datetime.fromtimestamp(r["last"], timezone.utc), "R")
            lines.append(f"<@{r['user_id']}> `{r['user_id']}` - {r['n']} msgs ({r['incoming']} from them) · last {when}")
        body = "\n".join(lines) or "No DMs logged yet - they're saved from now on."
        embed = discord.Embed(title="📥 Bot DMs", description=body[:4000], color=BLURPLE)
        if extra and page == 1:
            embed.add_field(name="Modmail users with nothing logged yet",
                            value=" ".join(f"<@{u}>" for u in extra[:20])[:1024], inline=False)
        embed.set_footer(text=f"Page {page}/{pages} · /inbox view <user> to open one")
        await interaction.response.send_message(embed=embed, ephemeral=_private(interaction),
                                                allowed_mentions=discord.AllowedMentions.none())

    @dm_group.command(name="view", description="Read the live DM conversation with someone (shows message IDs)")
    @app_commands.describe(user="Mention or user ID", limit="How many recent messages (default 15, max 40)")
    async def dm_view(self, interaction: discord.Interaction, user: discord.User, limit: int = 15):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=_private(interaction))
        dm = await self._dm(user)
        if dm is None:
            return await interaction.followup.send("I can't open a DM with that user.", ephemeral=_private(interaction))
        limit = min(max(1, limit), 40)
        try:
            msgs = [m async for m in dm.history(limit=limit)]
        except discord.HTTPException:
            return await interaction.followup.send("Couldn't read that DM.", ephemeral=_private(interaction))
        if not msgs:
            return await interaction.followup.send(f"No messages with {user.mention}.", ephemeral=_private(interaction),
                                                   allowed_mentions=discord.AllowedMentions.none())
        lines = []
        for m in reversed(msgs):
            who = "🤖 bot" if m.author.id == self.bot.user.id else f"👤 {m.author.display_name}"
            stamp = discord.utils.format_dt(m.created_at, "t")
            lines.append(f"{stamp} `{m.id}` **{who}**: {_clip(_message_text(m), 300)}")
        chunks, cur = [], ""
        for line in lines:
            if len(cur) + len(line) + 1 > 3900:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        embeds = [discord.Embed(title=f"💬 DMs with {user} ({user.id})" if i == 0 else None,
                                description=c, color=BLURPLE) for i, c in enumerate(chunks)]
        embeds[-1].set_footer(text="Copy a message ID into /inbox edit or /inbox delete (bot messages only)")
        for i in range(0, len(embeds), 10):
            await interaction.followup.send(embeds=embeds[i:i + 10], ephemeral=_private(interaction))

    @dm_group.command(name="send", description="DM someone as the bot")
    @app_commands.describe(user="Mention or user ID", message="What to send")
    async def dm_send(self, interaction: discord.Interaction, user: discord.User, message: str):
        if not await self._guard(interaction):
            return
        dm = await self._dm(user)
        try:
            sent = await dm.send(message, allowed_mentions=discord.AllowedMentions.none()) if dm else None
        except discord.HTTPException:
            sent = None
        if sent is None:
            return await interaction.response.send_message(
                "❌ Couldn't send - their DMs are closed, or they don't share a server with the bot.", ephemeral=True)
        await interaction.response.send_message(f"✅ Sent to {user.mention} (message ID `{sent.id}`).",
                                                ephemeral=_private(interaction),
                                                allowed_mentions=discord.AllowedMentions.none())

    @dm_group.command(name="edit", description="Edit one of the bot's own DMs")
    @app_commands.describe(user="Whose DM it's in", message_id="The message ID (from /inbox view)", new_text="The new text")
    async def dm_edit(self, interaction: discord.Interaction, user: discord.User, message_id: str, new_text: str):
        if not await self._guard(interaction):
            return
        dm = await self._dm(user)
        try:
            msg = await dm.fetch_message(int(message_id.strip())) if dm else None
        except (ValueError, discord.HTTPException):
            msg = None
        if msg is None:
            return await interaction.response.send_message("I can't find that message in that DM.", ephemeral=True)
        if msg.author.id != self.bot.user.id:
            return await interaction.response.send_message("I can only edit my own messages, not theirs.", ephemeral=True)
        try:
            if not msg.content and msg.embeds and msg.embeds[0].description is not None:
                embed = msg.embeds[0]  # e.g. modmail replies live in an embed
                embed.description = new_text[:4000]
                await msg.edit(embed=embed)
            else:
                await msg.edit(content=new_text[:2000])
        except discord.HTTPException:
            return await interaction.response.send_message("Discord wouldn't let me edit that message.", ephemeral=True)
        db._run("UPDATE dm_log SET content=?, edited=1 WHERE message_id=?", (new_text, msg.id))
        await interaction.response.send_message("✏️ Edited.", ephemeral=_private(interaction))

    @dm_group.command(name="delete", description="Delete one or more of the bot's own DMs")
    @app_commands.describe(user="Whose DM it's in", message_ids="Message ID(s), separated by commas or spaces")
    async def dm_delete(self, interaction: discord.Interaction, user: discord.User, message_ids: str):
        if not await self._guard(interaction):
            return
        dm = await self._dm(user)
        ids = [t for t in message_ids.replace(",", " ").split() if t.isdigit()]
        if dm is None or not ids:
            return await interaction.response.send_message("Give me at least one valid message ID.", ephemeral=True)
        await interaction.response.defer(ephemeral=_private(interaction))
        done, failed = 0, []
        for raw in ids[:25]:
            try:
                msg = await dm.fetch_message(int(raw))
                if msg.author.id != self.bot.user.id:
                    failed.append(f"`{raw}` (not mine)")
                    continue
                await msg.delete()
                done += 1
            except discord.NotFound:
                failed.append(f"`{raw}` (not found)")
            except discord.HTTPException:
                failed.append(f"`{raw}` (Discord refused)")
        text = f"🗑️ Deleted **{done}** message(s)."
        if failed:
            text += "\n⚠️ Skipped: " + ", ".join(failed)
        await interaction.followup.send(text, ephemeral=_private(interaction))

    @dm_group.command(name="feed", description="Mirror every DM to/from the bot into a channel (empty = off)")
    @app_commands.describe(channel="Where to post them - keep it private! Leave empty to turn it off")
    async def dm_feed(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not await self._guard(interaction):
            return
        if channel is None:
            db.delete_setting(0, FEED_KEY)
            return await interaction.response.send_message("🔕 DM feed is off.", ephemeral=_private(interaction))
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I can't post embeds in {channel.mention} - check my permissions there.", ephemeral=True)
        db.set_setting(0, FEED_KEY, channel.id)
        await interaction.response.send_message(
            f"📡 Every DM to or from the bot will be copied to {channel.mention}. Make sure only owners can see it.",
            ephemeral=_private(interaction))


async def setup(bot: commands.Bot):
    await bot.add_cog(DMInbox(bot))
