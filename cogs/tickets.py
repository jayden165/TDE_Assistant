import io
import re
import logging
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def is_admin(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    return gc.get(member.guild.id, "admin-role") in {r.id for r in member.roles}


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-")[:20]


class TicketPanelView(discord.ui.View):
    """Posted by /ticketpanel - a single button that opens a private ticket."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.primary, emoji="🎫",
                       custom_id="ticket_panel_open")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.open_ticket(interaction)


class CloseConfirmView(discord.ui.View):
    def __init__(self, cog: "Tickets", requester_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id and not is_admin(interaction.user):
            await interaction.response.send_message("you dont have the perms", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.perform_close(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Close cancelled.", view=None)


class DeleteFinalConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes, delete permanently", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Deleting...", ephemeral=True)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.NotFound:
            pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Delete cancelled.", view=None)


class DeleteTicketView(discord.ui.View):
    """Posted after a ticket is closed - a standalone button to permanently delete it."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Delete Ticket", style=discord.ButtonStyle.danger, emoji="🗑️", custom_id="ticket_delete")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_message(
            "Are you sure you want to **permanently delete** this ticket? This can't be undone.",
            view=DeleteFinalConfirmView(), ephemeral=True,
        )


class TicketControlView(discord.ui.View):
    """Posted inside each ticket channel. Persistent."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, custom_id="ticket_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.claim(interaction)

    @discord.ui.button(label="Unclaim", style=discord.ButtonStyle.secondary, custom_id="ticket_unclaim")
    async def unclaim(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.unclaim(interaction)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: "Tickets" = interaction.client.get_cog("Tickets")
        await cog.close(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(TicketPanelView())
        self.bot.add_view(TicketControlView())
        self.bot.add_view(DeleteTicketView())

    # ---------------- panel setup ----------------

    @app_commands.command(name="ticketpanel", description="[Staff] Post the ticket creation panel in this channel")
    async def ticketpanel(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        embed = discord.Embed(
            title="📩 Create a Ticket",
            description="Click the button below to open a private ticket with staff.",
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=TicketPanelView())
        await interaction.response.send_message("Panel posted.", ephemeral=True)

    # ---------------- ticket creation ----------------

    async def create_ticket_channel(self, guild: discord.Guild, user: discord.Member,
                                     name_override: str = None, extra_pings: list = None):
        """Shared ticket-channel creation used by the panel and applications."""
        ticket_category_id = gc.get(guild.id, "ticket-category")
        cat_channel = guild.get_channel(ticket_category_id) if ticket_category_id else None
        if ticket_category_id and not isinstance(cat_channel, discord.CategoryChannel):
            # The ID is set but doesn't resolve to a category (wrong ID, or the bot can't see it).
            # Log loudly instead of silently dumping tickets at the top of the server.
            logging.warning(
                "Ticket category %s did not resolve to a category channel in guild %s - ticket will be "
                "created without a category. Check TICKET_CATEGORY_ID in .env and the bot's permissions.",
                ticket_category_id, guild.id,
            )
            cat_channel = None

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        admin_role = guild.get_role(gc.get(guild.id, "admin-role"))
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        if admin_role:
            overwrites[admin_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        chan_name = name_override or f"ticket-{slugify(user.name)}"
        try:
            channel = await guild.create_text_channel(
                name=chan_name[:100],
                category=cat_channel,
                overwrites=overwrites,
                topic=f"Ticket | opened by {user} ({user.id})",
                reason=f"Ticket opened by {user}",
            )
        except discord.Forbidden:
            return None

        db.create_ticket(channel.id, guild.id, user.id)

        embed = discord.Embed(
            title="🎫 Ticket",
            description=f"Hey {user.mention}, thanks for reaching out! Staff will be with you shortly.\n\n"
                         f"Use the buttons below to manage this ticket.",
            color=discord.Color.blurple(),
        )
        ping = user.mention
        if extra_pings:
            ping += " " + " ".join(extra_pings)
        elif staff_role:
            ping += f" {staff_role.mention}"
        await channel.send(content=ping, embed=embed, view=TicketControlView())
        return channel

    async def open_ticket(self, interaction: discord.Interaction):
        channel = await self.create_ticket_channel(interaction.guild, interaction.user)
        if channel is None:
            return await interaction.response.send_message(
                "I don't have permission to create ticket channels. Contact an admin.", ephemeral=True,
            )

        await interaction.response.send_message(
            f"\u2705 Your ticket has been created: {channel.mention}", ephemeral=True,
        )

    # ---------------- claim system ----------------

    async def claim(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if ticket["claimed_by"]:
            return await interaction.response.send_message("This ticket is already claimed.", ephemeral=True)

        db.claim_ticket(interaction.channel_id, interaction.user.id)
        await self._lock_to_claimer(interaction.channel, interaction.user.id)
        await interaction.response.send_message(f"🔒 Ticket claimed by {interaction.user.mention}. "
                                                  f"Other staff can no longer type here until it's unclaimed.")

    async def unclaim(self, interaction: discord.Interaction):
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
        if not ticket["claimed_by"]:
            return await interaction.response.send_message("This ticket isn't claimed.", ephemeral=True)
        if ticket["claimed_by"] != interaction.user.id and not is_admin(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        db.unclaim_ticket(interaction.channel_id)
        await self._unlock_staff(interaction.channel)
        await interaction.response.send_message("🔓 Ticket unclaimed. Any staff member may respond now.")

    async def _lock_to_claimer(self, channel: discord.TextChannel, claimer_id: int):
        guild = channel.guild
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        if staff_role:
            await channel.set_permissions(staff_role, send_messages=False, view_channel=True, read_message_history=True)
        claimer = guild.get_member(claimer_id)
        if claimer:
            await channel.set_permissions(claimer, send_messages=True, view_channel=True, read_message_history=True)
        # Admins always retain access via their role overwrite (not restricted here).

    async def _unlock_staff(self, channel: discord.TextChannel):
        guild = channel.guild
        staff_role = guild.get_role(gc.get(guild.id, "staff-role"))
        if staff_role:
            await channel.set_permissions(staff_role, send_messages=True, view_channel=True, read_message_history=True)

    # ---------------- close + transcript ----------------

    async def close(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        ticket = db.get_ticket(interaction.channel_id)
        if not ticket:
            return await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)

        await interaction.response.send_message(
            "Are you sure you want to close this ticket?",
            view=CloseConfirmView(self, interaction.user.id),
            ephemeral=True,
        )

    async def perform_close(self, interaction: discord.Interaction):
        channel = interaction.channel
        ticket = db.get_ticket(channel.id)
        if not ticket:
            return await interaction.response.edit_message(content="This isn't a ticket channel anymore.", view=None)

        await interaction.response.edit_message(content="Closing ticket and saving transcript...", view=None)

        transcript_lines = []
        async for msg in channel.history(limit=None, oldest_first=True):
            content = msg.content or ""
            if msg.attachments:
                content += " " + " ".join(a.url for a in msg.attachments)
            transcript_lines.append(f"[{msg.created_at.strftime('%Y-%m-%d %H:%M:%S')}] {msg.author} ({msg.author.id}): {content}")
        transcript_text = "\n".join(transcript_lines) or "(no messages)"

        db.close_ticket(channel.id)

        log_channel = self.bot.get_channel(gc.get(channel.guild.id, "transcript-channel"))
        if log_channel:
            file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{channel.name}.txt")
            embed = discord.Embed(
                title=f"Ticket Closed: {channel.name}",
                description=f"Opened by: <@{ticket['user_id']}>\nClosed by: {interaction.user.mention}",
                color=discord.Color.dark_grey(),
            )
            await log_channel.send(embed=embed, file=file)

        # Remove the ticket opener's access now that it's closed - they no longer need to see it.
        opener = channel.guild.get_member(ticket["user_id"])
        if opener:
            try:
                await channel.set_permissions(opener, overwrite=None, reason="Ticket closed")
            except discord.HTTPException:
                pass

        close_embed = discord.Embed(
            title="🔒 Ticket Closed",
            description=(
                f"Closed by {interaction.user.mention}. Transcript saved" +
                (f" to {log_channel.mention}." if log_channel else ".") +
                "\n\nClick below to permanently delete this ticket."
            ),
            color=discord.Color.dark_grey(),
        )
        await channel.send(embed=close_embed, view=DeleteTicketView())


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
