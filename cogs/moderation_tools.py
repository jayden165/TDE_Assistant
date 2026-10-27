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
# /embed modal
# =====================================================================================

MAX_MESSAGE_LENGTH = 2000


INFRACTION_EMOJI = {"strike": "❗", "warning": "⚠️", "demote": "⬇️", "timeout": "⏱️", "mute": "🔇", "kick": "👢", "ban": "🔨"}


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


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


# =====================================================================================
# Moderation panel
# =====================================================================================

# =====================================================================================
# Moderation panel
# =====================================================================================
# Every button runs the same code as the matching slash command (cogs/punishments.py), so the panel
# gets the same hierarchy checks, DM to the member, infraction record and staff action log entry.

class ActionModal(discord.ui.Modal):
    """Asks for a reason (and a duration for timeouts), then runs the command on the member."""

    def __init__(self, action: str, member: discord.Member):
        super().__init__(title=f"{action.title()} {member.display_name}"[:45])
        self.action, self.member = action, member
        self.duration = None
        if action == "timeout":
            self.duration = discord.ui.TextInput(label="Duration (10m, 2h, 1d - or 'off' to end it)", max_length=20,
                                                 default="1h")
            self.add_item(self.duration)
        self.reason = discord.ui.TextInput(label="Reason" + ("" if action == "warn" else " (optional)"),
                                           style=discord.TextStyle.paragraph, max_length=300,
                                           required=(action == "warn"))
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Punishments")
        if cog is None:
            return await interaction.response.send_message("The moderation commands aren't loaded right now.", ephemeral=True)
        reason = str(self.reason.value).strip() or "No reason given"
        member = self.member
        if self.action == "timeout":
            await cog.timeout.callback(cog, interaction, member, str(self.duration.value).strip(), reason)
        elif self.action == "mute":
            await cog.mute.callback(cog, interaction, member, reason)
        elif self.action == "warn":
            await cog.warn.callback(cog, interaction, member, reason)
        elif self.action == "kick":
            await cog.kick.callback(cog, interaction, member, reason)
        elif self.action == "ban":
            await cog.ban.callback(cog, interaction, member, reason)


class ModerationPanelView(discord.ui.View):
    def __init__(self, member: discord.Member, opener_id: int):
        super().__init__(timeout=300)
        self.member, self.opener_id = member, opener_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message("This panel belongs to someone else - run `/moderation-panel` yourself.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Warn", style=discord.ButtonStyle.secondary, emoji="⚠️", row=0)
    async def warn_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActionModal("warn", self.member))

    @discord.ui.button(label="Timeout", style=discord.ButtonStyle.secondary, emoji="⏱️", row=0)
    async def timeout_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActionModal("timeout", self.member))

    @discord.ui.button(label="Mute / Unmute", style=discord.ButtonStyle.secondary, emoji="🔇", row=0)
    async def mute_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActionModal("mute", self.member))

    @discord.ui.button(label="Kick", style=discord.ButtonStyle.danger, emoji="👢", row=1)
    async def kick_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActionModal("kick", self.member))

    @discord.ui.button(label="Ban", style=discord.ButtonStyle.danger, emoji="🔨", row=1)
    async def ban_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ActionModal("ban", self.member))

    @discord.ui.button(label="View Logs", style=discord.ButtonStyle.primary, emoji="📜", row=1)
    async def logs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = db.get_infractions(interaction.guild_id, self.member.id)
        if not rows:
            return await interaction.response.send_message(f"{self.member.mention} has no moderation history.", ephemeral=True)
        desc = "\n".join(
            f"{INFRACTION_EMOJI.get(r['type'], '•')} **{r['type'].title()}** - {r['reason']} "
            f"(<t:{int(r['created_at'])}:R>, by <@{r['staff_id']}>)"
            for r in rows
        )
        embed = discord.Embed(title=f"Moderation Log: {self.member.display_name}", description=desc[:4000],
                              color=discord.Color.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ModerationTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------------- /role ----------------

    @app_commands.command(name="role", description="[Admin] Add a role to a member - or take it away with remove:True")
    @app_commands.describe(member="Who to change", role="The role to add or remove",
                           remove="Take the role away instead of adding it")
    async def role(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role, remove: bool = False):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        verb = "remove" if remove else "add"
        try:
            if remove:
                await member.remove_roles(role, reason=f"Removed by {interaction.user} via /role")
            else:
                await member.add_roles(role, reason=f"Added by {interaction.user} via /role")
        except discord.Forbidden:
            return await interaction.response.send_message(
                f"Couldn't {verb} that role — check my permissions and role hierarchy.", ephemeral=True,
            )
        await interaction.response.send_message(
            f"Removed {role.mention} from {member.mention}." if remove else f"Added {role.mention} to {member.mention}.",
            ephemeral=True)

    # ---------------- staff action log ----------------

    async def _log_staff_action(self, interaction: discord.Interaction, title: str, where_label: str,
                                where_value: str, content: str, status: str = "Sent"):
        """Posts a /say record to the staff action log channel (if one is configured).
        Never raises - a logging problem must not break the command itself."""
        try:
            channel = self.bot.get_channel(gc.get(interaction.guild_id, "staff-action-log-channel"))
            if channel is None:
                return
            embed = discord.Embed(
                title=title,
                description=content[:4000],
                color=discord.Color.orange() if status == "Sent" else discord.Color.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Used by", value=f"{interaction.user.mention} ({interaction.user.id})", inline=True)
            embed.add_field(name=where_label, value=where_value, inline=True)
            embed.add_field(name="Status", value=status, inline=True)
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ---------------- /say ----------------

    @app_commands.command(name="say", description="[Owner] Send a plain message to a channel - or DM it to a member")
    @app_commands.describe(
        message="What to send",
        channel="Channel to send to (defaults to this channel)",
        user="DM it to this member instead of posting in a channel",
    )
    async def say(self, interaction: discord.Interaction, message: str, channel: discord.TextChannel = None,
                  user: discord.Member = None):
        if not is_owner(interaction.user.id):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        if len(message) > MAX_MESSAGE_LENGTH:
            return await interaction.response.send_message(
                f"That message is too long - it has to fit in {MAX_MESSAGE_LENGTH} characters.", ephemeral=True,
            )
        if user is not None:  # DM mode
            try:
                await user.send(message)
            except discord.HTTPException:
                await interaction.response.send_message(f"Couldn't DM {user.mention} — their DMs may be closed.", ephemeral=True)
                return await self._log_staff_action(
                    interaction, "✉️ /say (DM)", "Recipient", f"{user.mention} ({user.id})", message, status="Failed (DMs closed)",
                )
            await interaction.response.send_message(f"DM sent to {user.mention}.", ephemeral=True)
            return await self._log_staff_action(interaction, "✉️ /say (DM)", "Recipient", f"{user.mention} ({user.id})", message)

        target = channel or interaction.channel
        try:
            await target.send(message)
        except discord.HTTPException:
            await interaction.response.send_message(
                f"Couldn't send to {target.mention} - check my permissions there.", ephemeral=True,
            )
            return await self._log_staff_action(
                interaction, "💬 /say", "Channel", target.mention, message, status="Failed",
            )
        await interaction.response.send_message(f"Sent to {target.mention}.", ephemeral=True)
        await self._log_staff_action(interaction, "💬 /say", "Channel", target.mention, message)

    # ---------------- /embed ----------------

    @app_commands.command(name="embed", description="[Admin] Send an embed (popup form) to a channel - or DM it to a member")
    @app_commands.describe(channel="Channel to send to (defaults to this channel)",
                           user="DM it to this member instead of posting in a channel")
    async def embed(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                    user: discord.Member = None):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        target = channel or interaction.channel

        async def send_it(modal_interaction: discord.Interaction, built: discord.Embed):
            if user is not None:
                try:
                    await user.send(embed=built)
                except discord.HTTPException:
                    return await modal_interaction.response.send_message(
                        f"Couldn't DM {user.mention} — their DMs may be closed.", ephemeral=True)
                return await modal_interaction.response.send_message(f"DM sent to {user.mention}.", ephemeral=True)
            try:
                await target.send(embed=built)
            except discord.HTTPException:
                return await modal_interaction.response.send_message(
                    f"Couldn't send to {target.mention} - check my permissions there.", ephemeral=True)
            await modal_interaction.response.send_message(f"Sent to {target.mention}.", ephemeral=True)

        await interaction.response.send_modal(EmbedModal(send_it))

    # ---------------- /moderation-panel ----------------

    @app_commands.command(name="moderation-panel", description="[Admin] Open a moderation panel for a member")
    @app_commands.describe(member="The member to moderate")
    async def moderation_panel(self, interaction: discord.Interaction, member: discord.Member):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        embed = discord.Embed(title=f"Moderation Panel: {member.display_name}", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="User", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"), inline=True)
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"), inline=True)
        status = []
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            status.append(f"⏱️ Timed out until {discord.utils.format_dt(member.timed_out_until, 'R')}")
        mute_role = interaction.guild.get_role(gc.get(interaction.guild.id, "mute-role") or 0)
        if mute_role and mute_role in member.roles:
            status.append("🔇 Muted")
        embed.add_field(name="Status", value="\n".join(status) or "✅ No active punishments", inline=False)
        embed.add_field(name="Past actions", value=f"{len(db.get_infractions(interaction.guild.id, member.id))} on record", inline=True)
        embed.add_field(name="Roles", value=(" ".join(roles) or "None")[:1024], inline=False)
        embed.set_footer(text="Buttons use the same rules as /ban, /kick, /timeout, /mute and /warn.")

        await interaction.response.send_message(embed=embed, view=ModerationPanelView(member, interaction.user.id), ephemeral=True)

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
