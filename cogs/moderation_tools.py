import datetime
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc


def is_admin_perm(member: discord.Member) -> bool:
    """Real Discord 'Administrator' permission, not a specific role ID. Bot owner always passes."""
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


# =====================================================================================
# /say and /dm embed modals
# =====================================================================================

MAX_MESSAGE_LENGTH = 2000


def attributed(text: str, sender: discord.abc.User) -> str:
    """Appends a small-text line saying who sent this. Uses the plain @username rather than a
    real mention, so it never pings the sender (or anyone else)."""
    return f"{text}\n-# sent by @{sender.name}"


INFRACTION_EMOJI = {"strike": "❗", "warning": "⚠️", "demote": "⬇️", "timeout": "⏱️", "mute": "🔇", "kick": "👢", "ban": "🔨"}


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def can_hide_sender(user: discord.abc.User, guild: discord.Guild) -> bool:
    """Only the server's owner, or the bot owner, may remove the "sent by" line."""
    return is_owner(user.id) or (guild is not None and guild.owner_id == user.id)


NO_HIDE_MESSAGE = "you dont have the perms"


class EmbedModal(discord.ui.Modal, title="Compose Embed"):
    def __init__(self, on_submit_callback):
        super().__init__()
        self._callback = on_submit_callback
        self.embed_title = discord.ui.TextInput(label="Title (optional)", max_length=256, required=False)
        self.embed_body = discord.ui.TextInput(
            label="Message", style=discord.TextStyle.paragraph, max_length=4000,
        )
        self.add_item(self.embed_title)
        self.add_item(self.embed_body)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(description=str(self.embed_body.value), color=discord.Color.blurple())
        if self.embed_title.value:
            embed.title = str(self.embed_title.value)
        await self._callback(interaction, embed)


class TimeoutModal(discord.ui.Modal, title="Timeout Member"):
    def __init__(self, member: discord.Member):
        super().__init__()
        self.member = member
        self.minutes = discord.ui.TextInput(label="Duration (minutes)", max_length=6, placeholder="60")
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, required=False, max_length=300)
        self.add_item(self.minutes)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            minutes = int(str(self.minutes.value).strip())
        except ValueError:
            return await interaction.response.send_message("Duration must be a whole number of minutes.", ephemeral=True)

        reason = str(self.reason.value).strip() if self.reason.value else "No reason given"
        until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
        try:
            await self.member.timeout(until, reason=f"Timed out by {interaction.user}: {reason}")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Couldn't apply the timeout — check my permissions/role hierarchy.", ephemeral=True,
            )
        db.add_infraction(interaction.guild_id, self.member.id, "timeout", f"{minutes}m — {reason}", interaction.user.id)
        await interaction.response.send_message(f"{self.member.mention} timed out for {minutes} minute(s).", ephemeral=True)


# =====================================================================================
# Moderation panel
# =====================================================================================

class ModerationPanelView(discord.ui.View):
    def __init__(self, member: discord.Member):
        super().__init__(timeout=300)
        self.member = member

    @discord.ui.button(label="Timeout", style=discord.ButtonStyle.secondary, emoji="⏱️")
    async def timeout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutModal(self.member))

    @discord.ui.button(label="Mute", style=discord.ButtonStyle.secondary, emoji="🔇")
    async def mute_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        mute_role_id = gc.get(interaction.guild_id, "mute-role")
        if not mute_role_id:
            return await interaction.response.send_message("The mute role isn't configured yet — set MUTE_ROLE_ID in .env.", ephemeral=True)
        role = interaction.guild.get_role(mute_role_id)
        if not role:
            return await interaction.response.send_message("The configured mute role wasn't found in this server.", ephemeral=True)
        try:
            await self.member.add_roles(role, reason=f"Muted by {interaction.user} via moderation panel")
        except discord.Forbidden:
            return await interaction.response.send_message("Couldn't add the mute role — check my permissions.", ephemeral=True)
        db.add_infraction(interaction.guild_id, self.member.id, "mute", "Muted via moderation panel", interaction.user.id)
        await interaction.response.send_message(f"🔇 {self.member.mention} has been muted.", ephemeral=True)

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger, emoji="👢")
    async def kick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.member.kick(reason=f"Kicked by {interaction.user} via moderation panel")
        except discord.Forbidden:
            return await interaction.response.send_message("Couldn't kick — check my permissions/role hierarchy.", ephemeral=True)
        db.add_infraction(interaction.guild_id, self.member.id, "kick", "Kicked via moderation panel", interaction.user.id)
        await interaction.response.send_message(f"👢 {self.member.mention} has been kicked.", ephemeral=True)

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="🔨")
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.member.ban(reason=f"Banned by {interaction.user} via moderation panel", delete_message_seconds=0)
        except discord.Forbidden:
            return await interaction.response.send_message("Couldn't ban — check my permissions/role hierarchy.", ephemeral=True)
        db.add_infraction(interaction.guild_id, self.member.id, "ban", "Banned via moderation panel", interaction.user.id)
        await interaction.response.send_message(f"🔨 {self.member.mention} has been banned.", ephemeral=True)

    @discord.ui.button(label="View Logs", style=discord.ButtonStyle.primary, emoji="📜")
    async def logs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.get_infractions(interaction.guild_id, self.member.id)
        if not rows:
            return await interaction.response.send_message(f"{self.member.mention} has no moderation history.", ephemeral=True)
        desc = "\n".join(
            f"**{r['type'].title()}** — {r['reason']} (<t:{int(r['created_at'])}:R>, by <@{r['staff_id']}>)"
            for r in rows
        )
        embed = discord.Embed(title=f"Moderation Log: {self.member.display_name}", description=desc[:4000], color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ModerationTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- /role ----------------

    role_group = app_commands.Group(name="role", description="[Admin] Add or remove a role from a member")

    @role_group.command(name="add", description="Add a role to a member")
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        try:
            await member.add_roles(role, reason=f"Added by {interaction.user} via /role add")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Couldn't add that role — check my permissions and role hierarchy.", ephemeral=True,
            )
        await interaction.response.send_message(f"Added {role.mention} to {member.mention}.", ephemeral=True)

    @role_group.command(name="remove", description="Remove a role from a member")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        try:
            await member.remove_roles(role, reason=f"Removed by {interaction.user} via /role remove")
        except discord.Forbidden:
            return await interaction.response.send_message(
                "Couldn't remove that role — check my permissions and role hierarchy.", ephemeral=True,
            )
        await interaction.response.send_message(f"Removed {role.mention} from {member.mention}.", ephemeral=True)

    # ---------------- /say ----------------

    @app_commands.command(name="say", description="[Admin] Send a plain message to a channel")
    @app_commands.describe(
        message="What to send",
        channel="Channel to send to (defaults to this channel)",
        hide_sender="Remove the \"sent by @user\" line (server owner / bot owner only)",
    )
    async def say(self, interaction: discord.Interaction, message: str, channel: discord.TextChannel = None,
                  hide_sender: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if hide_sender and not can_hide_sender(interaction.user, interaction.guild):
            return await interaction.response.send_message(NO_HIDE_MESSAGE, ephemeral=True)
        target = channel or interaction.channel
        content = message if hide_sender else attributed(message, interaction.user)
        if len(content) > MAX_MESSAGE_LENGTH:
            return await interaction.response.send_message(
                f"That message is too long - with the \"sent by\" line it has to fit in {MAX_MESSAGE_LENGTH} characters.",
                ephemeral=True,
            )
        await target.send(content)
        await interaction.response.send_message(f"Sent to {target.mention}.", ephemeral=True)

    @app_commands.command(name="say-embed", description="[Admin] Send an embed to a channel via a popup form")
    @app_commands.describe(channel="Channel to send to (defaults to this channel)")
    async def say_embed(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        target = channel or interaction.channel

        async def send_it(modal_interaction: discord.Interaction, embed: discord.Embed):
            await target.send(embed=embed)
            await modal_interaction.response.send_message(f"Sent to {target.mention}.", ephemeral=True)

        await interaction.response.send_modal(EmbedModal(send_it))

    # ---------------- /dm ----------------

    @app_commands.command(name="dm", description="[Admin] DM a plain message to a user")
    @app_commands.describe(hide_sender="Remove the \"sent by @user\" line (server owner / bot owner only)")
    async def dm(self, interaction: discord.Interaction, user: discord.Member, message: str,
                 hide_sender: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if hide_sender and not can_hide_sender(interaction.user, interaction.guild):
            return await interaction.response.send_message(NO_HIDE_MESSAGE, ephemeral=True)
        content = message if hide_sender else attributed(message, interaction.user)
        if len(content) > MAX_MESSAGE_LENGTH:
            return await interaction.response.send_message(
                f"That message is too long - with the \"sent by\" line it has to fit in {MAX_MESSAGE_LENGTH} characters.",
                ephemeral=True,
            )
        try:
            await user.send(content)
            await interaction.response.send_message(f"DM sent to {user.mention}.", ephemeral=True)
        except discord.HTTPException:
            await interaction.response.send_message(f"Couldn't DM {user.mention} — their DMs may be closed.", ephemeral=True)

    @app_commands.command(name="dm-embed", description="[Admin] DM an embed to a user via a popup form")
    async def dm_embed(self, interaction: discord.Interaction, user: discord.Member):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        async def send_it(modal_interaction: discord.Interaction, embed: discord.Embed):
            try:
                await user.send(embed=embed)
                await modal_interaction.response.send_message(f"DM sent to {user.mention}.", ephemeral=True)
            except discord.HTTPException:
                await modal_interaction.response.send_message(f"Couldn't DM {user.mention} — their DMs may be closed.", ephemeral=True)

        await interaction.response.send_modal(EmbedModal(send_it))

    # ---------------- /moderation-panel ----------------

    @app_commands.command(name="moderation-panel", description="[Admin] Open a moderation panel for a member")
    async def moderation_panel(self, interaction: discord.Interaction, member: discord.Member):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        role_list = ", ".join(r.mention for r in member.roles if r.name != "@everyone") or "None"
        embed = discord.Embed(title=f"Moderation Panel: {member.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        embed.add_field(name="Roles", value=role_list[:1024], inline=False)

        await interaction.response.send_message(embed=embed, view=ModerationPanelView(member), ephemeral=True)


    # ---------------- /infractions ----------------

    @app_commands.command(name="infractions", description="[Staff] View a member's infraction history")
    async def infractions(self, interaction: discord.Interaction, member: discord.Member):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        rows = db.get_infractions(interaction.guild_id, member.id)
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no infractions on record.", ephemeral=True)

        desc = "\n".join(
            f"{INFRACTION_EMOJI.get(r['type'], '•')} **{r['type'].title()}** — {r['reason']} "
            f"(<t:{int(r['created_at'])}:R>, by <@{r['staff_id']}>)"
            for r in rows
        )
        embed = discord.Embed(title=f"Infraction History: {member.display_name}", description=desc, color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationTools(bot))
