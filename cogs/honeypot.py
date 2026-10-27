import datetime
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc

PUNISHMENTS = ["ban", "softban", "kick", "timeout"]


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


class Honeypot(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- config commands ----------------

    @app_commands.command(name="honeypot", description="[Admin] Configure the anti-spam honeypot - run it with no options to see the current setup")
    @app_commands.describe(
        add_channel="Mark a channel as a honeypot trap — real members should never post here",
        remove_channel="Stop treating a channel as a honeypot trap",
        log_channel="Where honeypot catches get logged",
        punishment="What happens to someone who trips the trap",
        trigger="What kind of message trips the trap",
        enabled="Turn the honeypot on or off",
        purge="Wipe a caught user's recent messages server-wide when triggered",
    )
    @app_commands.choices(
        punishment=[
            app_commands.Choice(name="Ban", value="ban"),
            app_commands.Choice(name="Softban (ban+unban, wipes their messages)", value="softban"),
            app_commands.Choice(name="Kick", value="kick"),
            app_commands.Choice(name="Timeout (24h)", value="timeout"),
        ],
        trigger=[
            app_commands.Choice(name="Any message", value="any"),
            app_commands.Choice(name="Only messages with links", value="links"),
            app_commands.Choice(name="Only messages with attachments", value="attachments"),
        ],
        enabled=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ],
        purge=[
            app_commands.Choice(name="On", value="on"),
            app_commands.Choice(name="Off", value="off"),
        ],
    )
    async def hp_config(
        self,
        interaction: discord.Interaction,
        add_channel: discord.TextChannel = None,
        remove_channel: discord.TextChannel = None,
        log_channel: discord.TextChannel = None,
        punishment: app_commands.Choice[str] = None,
        trigger: app_commands.Choice[str] = None,
        enabled: app_commands.Choice[str] = None,
        purge: app_commands.Choice[str] = None,
    ):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        gid = interaction.guild_id
        changes = []

        if add_channel is not None:
            existing = db.get_setting(gid, "honeypot_channels", "")
            ids = {i for i in existing.split(",") if i}
            ids.add(str(add_channel.id))
            db.set_setting(gid, "honeypot_channels", ",".join(ids))
            changes.append(f"{add_channel.mention} added as a trap channel")

        if remove_channel is not None:
            existing = db.get_setting(gid, "honeypot_channels", "")
            ids = {i for i in existing.split(",") if i}
            ids.discard(str(remove_channel.id))
            db.set_setting(gid, "honeypot_channels", ",".join(ids))
            changes.append(f"{remove_channel.mention} removed as a trap channel")

        if log_channel is not None:
            db.set_setting(gid, "honeypot_log_channel", log_channel.id)
            changes.append(f"Log channel set to {log_channel.mention}")

        if punishment is not None:
            db.set_setting(gid, "honeypot_punishment", punishment.value)
            changes.append(f"Punishment set to **{punishment.name}**")

        if trigger is not None:
            db.set_setting(gid, "honeypot_trigger", trigger.value)
            changes.append(f"Trigger set to **{trigger.name}**")

        if enabled is not None:
            db.set_setting(gid, "honeypot_enabled", "1" if enabled.value == "on" else "0")
            changes.append(f"Honeypot turned **{enabled.name}**")

        if purge is not None:
            db.set_setting(gid, "honeypot_purge", "1" if purge.value == "on" else "0")
            changes.append(f"Message purging set to **{purge.name}**")

        if not changes:  # nothing to change: just show the current setup
            return await interaction.response.send_message(embed=self._status_embed(interaction.guild_id), ephemeral=True)

        await interaction.response.send_message("Updated:\n" + "\n".join(f"- {c}" for c in changes), ephemeral=True)

    def _status_embed(self, gid: int) -> discord.Embed:
        enabled = db.get_setting(gid, "honeypot_enabled", "0") == "1"
        channels = db.get_setting(gid, "honeypot_channels", "")
        chan_text = ", ".join(f"<#{c}>" for c in channels.split(",") if c) or "None set"
        log_id = db.get_setting(gid, "honeypot_log_channel")
        punishment = db.get_setting(gid, "honeypot_punishment", "softban")
        trigger = db.get_setting(gid, "honeypot_trigger", "any")
        purge = db.get_setting(gid, "honeypot_purge", "1") == "1"

        embed = discord.Embed(
            title="🍯 Honeypot Configuration",
            color=discord.Color.green() if enabled else discord.Color.dark_grey(),
        )
        embed.add_field(name="Status", value="🟢 Enabled" if enabled else "🔴 Disabled", inline=True)
        embed.add_field(name="Punishment", value=punishment.title(), inline=True)
        embed.add_field(name="Triggers On", value=trigger.title(), inline=True)
        embed.add_field(name="Trap Channels", value=chan_text, inline=False)
        embed.add_field(name="Log Channel", value=f"<#{log_id}>" if log_id else "Not set", inline=True)
        embed.add_field(name="Purge Their Messages", value="Yes" if purge else "No", inline=True)
        embed.set_footer(text="Staff, admins, and the bot owner are always immune.")
        return embed

    # ---------------- the trap itself ----------------

    def _is_immune(self, member: discord.Member) -> bool:
        if is_owner(member.id):
            return True
        if member.guild.owner_id == member.id:
            return True
        if member.guild_permissions.administrator or member.guild_permissions.manage_messages:
            return True
        role_ids = {r.id for r in member.roles}
        return bool(role_ids & {gc.get(member.guild.id, "staff-role"), gc.get(member.guild.id, "admin-role")})

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        gid = message.guild.id

        if db.get_setting(gid, "honeypot_enabled", "0") != "1":
            return
        channels = db.get_setting(gid, "honeypot_channels", "")
        if str(message.channel.id) not in {c for c in channels.split(",") if c}:
            return
        if self._is_immune(message.author):
            return

        trigger = db.get_setting(gid, "honeypot_trigger", "any")
        if trigger == "links" and not any(x in message.content.lower() for x in ("http://", "https://", "discord.gg", "www.")):
            return
        if trigger == "attachments" and not message.attachments:
            return

        await self._punish(message)

    async def _punish(self, message: discord.Message):
        gid = message.guild.id
        member = message.author
        punishment = db.get_setting(gid, "honeypot_punishment", "softban")
        should_purge = db.get_setting(gid, "honeypot_purge", "1") == "1"

        caught_content = (message.content or "*(no text)*")[:1000]
        if message.attachments:
            caught_content += "\n" + " ".join(a.url for a in message.attachments)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        # Wipe their recent messages across the server before removing them.
        purged_count = 0
        if should_purge:
            cutoff = discord.utils.utcnow() - datetime.timedelta(hours=24)
            for channel in message.guild.text_channels:
                try:
                    deleted = await channel.purge(
                        limit=200, after=cutoff,
                        check=lambda m: m.author.id == member.id,
                        reason="Honeypot: wiping caught spammer's messages",
                    )
                    purged_count += len(deleted)
                except (discord.Forbidden, discord.HTTPException):
                    continue

        action_taken = "None (failed)"
        try:
            if punishment == "ban":
                await member.ban(reason="Honeypot: posted in trap channel", delete_message_seconds=86400)
                action_taken = "Banned"
            elif punishment == "softban":
                await member.ban(reason="Honeypot: posted in trap channel", delete_message_seconds=86400)
                await message.guild.unban(member, reason="Honeypot softban - unbanning immediately")
                action_taken = "Softbanned (messages wiped)"
            elif punishment == "kick":
                await member.kick(reason="Honeypot: posted in trap channel")
                action_taken = "Kicked"
            elif punishment == "timeout":
                await member.timeout(discord.utils.utcnow() + datetime.timedelta(hours=24),
                                      reason="Honeypot: posted in trap channel")
                action_taken = "Timed out (24h)"
        except discord.Forbidden:
            action_taken = "Failed — missing permissions"
        except discord.HTTPException as e:
            action_taken = f"Failed — {e}"

        db.add_infraction(gid, member.id, "honeypot", f"Tripped honeypot in #{message.channel.name}", self.bot.user.id)

        log_id = db.get_setting(gid, "honeypot_log_channel") or gc.get(gid, "honeypot-log-channel")
        log_channel = message.guild.get_channel(int(log_id)) if log_id else None
        if log_channel:
            account_age = discord.utils.utcnow() - member.created_at
            embed = discord.Embed(
                title="🍯 Honeypot Triggered",
                description=f"{member.mention} posted in {message.channel.mention} and was **{action_taken}**.",
                color=discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=False)
            embed.add_field(name="Account Age", value=f"{account_age.days} days", inline=True)
            embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
            if member.joined_at:
                embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
            embed.add_field(name="Messages Purged", value=str(purged_count), inline=True)
            embed.add_field(name="Caught Message", value=caught_content[:1024], inline=False)
            await log_channel.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Honeypot(bot))
