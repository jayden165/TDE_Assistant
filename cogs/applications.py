import asyncio
import copy
import json
import discord
from discord import app_commands
from discord.ext import commands

from permissions import is_owner
import command_perms
import database as db
import guild_config as gc

# Fallback defaults for any server that hasn't customized its applications yet - keyed by
# name -> {"emoji": ..., "questions": [...]}. A server's real config lives in the generic
# settings table under the "application_types" key as JSON, see get_types()/save_types().
DEFAULT_APPLICATIONS = {
    "Staff Team": {
        "emoji": "🛡️",
        "questions": [
            "How old are you?",
            "What is your timezone?",
            "How active can you be weekly?",
            "Do you have any previous staff experience? If yes, please explain where and what position you had.",
            "Why do you want to become staff?",
            "Why should we choose you?",
            "How would you handle 2 members fighting?",
            "How would you handle community members repeatedly breaking the server rules?",
            "How would you handle a community ad that breaks the server rules/Discord TOS?",
            "Is there anything else we should know about you?",
        ],
    },
    "Developer": {
        "emoji": "💻",
        "questions": [
            "How old are you?",
            "What is your timezone?",
            "How active can you be weekly?",
            "Do you have any previous dev experience? If yes, please explain where and what position you had.",
            "Why do you want to become a developer here?",
            "Why should we choose you?",
            "How would you handle a bot bugging?",
            "How would you handle a bot having a bug where a user can use a command they shouldn't have access to?",
            "How would you handle a community ad that breaks the server rules/Discord TOS?",
            "Is there anything else we should know about you?",
        ],
    },
}

ANSWER_HINT = "-# To answer this question, please send a message to the bot with your response."
MAX_APPLICATION_TYPES = 25  # Discord Select menus cap out at 25 options


def is_staff(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    role_ids = {r.id for r in member.roles}
    return gc.get(member.guild.id, "staff-role") in role_ids or gc.get(member.guild.id, "admin-role") in role_ids


def is_admin_perm(member: discord.Member) -> bool:
    if is_owner(member.id):
        return True
    if command_perms.granted():
        return True
    return member.guild_permissions.administrator


def get_types(guild_id: int) -> dict:
    """This server's application types, or a COPY of the built-in defaults if it hasn't
    customized any. Must be a copy: callers commonly do `types = get_types(...)`, mutate
    the dict in place, then save_types(guild_id, types) - if this ever returned the live
    DEFAULT_APPLICATIONS object, the first server to customize anything would permanently
    corrupt the fallback every other unconfigured server sees, for the life of the process."""
    raw = db.get_setting(guild_id, "application_types")
    if raw:
        try:
            data = json.loads(raw)
            if data:
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return copy.deepcopy(DEFAULT_APPLICATIONS)


def save_types(guild_id: int, types: dict):
    db.set_setting(guild_id, "application_types", json.dumps(types))


async def type_name_autocomplete(interaction: discord.Interaction, current: str):
    types = get_types(interaction.guild_id)
    return [
        app_commands.Choice(name=name, value=name)
        for name in types
        if current.lower() in name.lower()
    ][:25]


class ApplicationPanelView(discord.ui.View):
    """Posted by /application-panel. Built fresh per-guild each time it's posted, so it
    reflects that server's current custom types - not persistent-registration-time state."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        types = get_types(guild_id)
        options = [
            discord.SelectOption(label=name, value=name, emoji=entry.get("emoji"))
            for name, entry in list(types.items())[:25]
        ]
        select = discord.ui.Select(
            placeholder="Select an application to apply for...",
            options=options or [discord.SelectOption(label="No applications configured", value="__none__")],
            disabled=not options,
            custom_id="application_panel_select",
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        app_type = interaction.data["values"][0]
        cog: "Applications" = interaction.client.get_cog("Applications")
        await cog.begin_application(interaction, app_type, interaction.guild_id)


class ConfirmStartView(discord.ui.View):
    def __init__(self, cog: "Applications", app_type: str, user: discord.User, origin_guild_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.app_type = app_type
        self.user = user
        self.origin_guild_id = origin_guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user.id

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=None)
        await self.cog.run_questions(interaction, self.app_type, self.origin_guild_id)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            description=f"Cancelled the application for **{self.app_type}**.",
            color=discord.Color.red(),
        )
        await interaction.response.edit_message(embed=embed, view=None)
        self.cog.active.discard(self.user.id)


class DecisionReasonModal(discord.ui.Modal):
    def __init__(self, view: "ApplicationDecisionView", decision: str):
        super().__init__(title=f"{decision} with Reason")
        self.view_ref = view
        self.decision = decision
        self.reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        await self.view_ref.apply_decision(interaction, self.decision, str(self.reason.value).strip())


class ApplicationDecisionView(discord.ui.View):
    def __init__(self, app_id: int):
        super().__init__(timeout=None)
        self.app_id = app_id

    async def apply_decision(self, interaction: discord.Interaction, decision: str, reason: str = None):
        db.set_application_status(self.app_id, decision.lower())
        embed = interaction.message.embeds[0]
        header = f"**{decision} by {interaction.user.mention}**"
        if reason:
            header += f" with reason: {reason}"
        embed.description = header + "\n\n" + (embed.description or "")
        embed.color = discord.Color.green() if decision == "Accepted" else discord.Color.red()

        for item in self.children:
            item.disabled = True

        # If this server has an Accepted / Denied channel set, move the decided application
        # there and delete it from the review channel. Unset (or same channel as the review
        # one) -> behaves as before and just edits the message in place.
        dest = None
        if interaction.guild:
            dest_key = "application-accepted-channel" if decision == "Accepted" else "application-denied-channel"
            dest_id = gc.get(interaction.guild.id, dest_key)
            dest = interaction.guild.get_channel(dest_id) if dest_id else None
            if dest is not None and dest.id == interaction.channel_id:
                dest = None

        moved = False
        if dest is not None:
            if not interaction.response.is_done():
                await interaction.response.defer()  # acknowledge now; the move below can take a moment
            try:
                await dest.send(embed=embed)
                moved = True
            except discord.HTTPException:
                moved = False  # can't post there (permissions/deleted) - keep the original instead
            if moved:
                try:
                    await interaction.message.delete()
                except discord.HTTPException:
                    pass

        if not moved:
            if interaction.response.is_done():
                await interaction.message.edit(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)

        row = db.get_application(self.app_id)
        if row:
            user = interaction.client.get_user(row["user_id"])
            if user:
                try:
                    dm = discord.Embed(
                        title=f"Application {decision}",
                        description=f"Your **{row['app_type']}** application was **{decision.lower()}**."
                                     + (f"\n\n**Reason:** {reason}" if reason else ""),
                        color=embed.color,
                    )
                    await user.send(embed=dm)
                except discord.HTTPException:
                    pass

            if decision == "Accepted" and interaction.guild:
                entry = get_types(row["guild_id"]).get(row["app_type"], {})
                role_id = entry.get("role_id")
                if role_id:
                    member = interaction.guild.get_member(row["user_id"])
                    role = interaction.guild.get_role(int(role_id))
                    if member and role:
                        try:
                            await member.add_roles(role, reason=f"Application '{row['app_type']}' accepted")
                        except discord.Forbidden:
                            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, custom_id="app_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await self.apply_decision(interaction, "Accepted")

    @discord.ui.button(label="Accept w/ Reason", style=discord.ButtonStyle.success, custom_id="app_accept_reason")
    async def accept_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal(self, "Accepted"))

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="app_deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await self.apply_decision(interaction, "Denied")

    @discord.ui.button(label="Deny w/ Reason", style=discord.ButtonStyle.danger, custom_id="app_deny_reason")
    async def deny_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        await interaction.response.send_modal(DecisionReasonModal(self, "Denied"))

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.primary, emoji="🎫", custom_id="app_ticket")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        row = db.get_application(self.app_id)
        if not row:
            return await interaction.response.send_message("Couldn't find that application.", ephemeral=True)
        member = interaction.guild.get_member(row["user_id"])
        if not member:
            return await interaction.response.send_message("That user isn't in the server anymore.", ephemeral=True)

        tickets_cog = interaction.client.get_cog("Tickets")
        if not tickets_cog:
            return await interaction.response.send_message("Ticket system isn't loaded.", ephemeral=True)
        channel = await tickets_cog.create_ticket_channel(
            interaction.guild, member,
            name_override=f"{member.name}-application-ticket",
        )
        if channel is None:
            return await interaction.response.send_message("Couldn't create the ticket channel.", ephemeral=True)
        await interaction.response.send_message(f"Ticket opened: {channel.mention}", ephemeral=True)


class Applications(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bot.add_view(ApplicationPanelView(0))  # routing-only template, see tickets.py for why guild_id=0 is fine here
        self.bot.add_view(ApplicationDecisionView(0))
        self.active: set[int] = set()   # user IDs mid-application, to stop double-starting

    @app_commands.command(name="application-panel", description="[Staff] Post the application panel in this channel")
    async def application_panel(self, interaction: discord.Interaction):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if not types:
            return await interaction.response.send_message(
                "No application types are configured for this server yet — use /applications create first.",
                ephemeral=True,
            )
        embed = discord.Embed(
            title="📋 Applications",
            description=(
                "Select an application below to get started. "
                "The bot will DM you the questions one at a time.\n\n"
                + "\n".join(f"{entry.get('emoji', '📋')} **{name}**" for name, entry in types.items())
            ),
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed, view=ApplicationPanelView(interaction.guild_id))
        await interaction.response.send_message("Application panel posted.", ephemeral=True)

    async def begin_application(self, interaction: discord.Interaction, app_type: str, guild_id: int):
        if app_type not in get_types(guild_id):
            return await interaction.response.send_message(
                "That application isn't available anymore — ask staff to re-post the panel.", ephemeral=True,
            )
        if interaction.user.id in self.active:
            return await interaction.response.send_message(
                "You already have an application in progress — check your DMs.", ephemeral=True,
            )
        self.active.add(interaction.user.id)

        embed = discord.Embed(
            title="Confirm Application",
            description=f"Are you sure you would like to apply for **{app_type}**?",
            color=discord.Color.blue(),
        )
        try:
            await interaction.user.send(embed=embed, view=ConfirmStartView(self, app_type, interaction.user, guild_id))
            await interaction.response.send_message("Check your DMs to continue!", ephemeral=True)
        except discord.Forbidden:
            self.active.discard(interaction.user.id)
            await interaction.response.send_message(
                "I couldn't DM you — please enable DMs from server members and try again.", ephemeral=True,
            )

    async def run_questions(self, interaction: discord.Interaction, app_type: str, guild_id: int):
        user = interaction.user
        dm = interaction.channel  # this is the DM channel, since the confirm button was clicked in DMs

        # guild_id was captured back when the panel was clicked (in-guild), NOT from this DM
        # interaction, which has no .guild at all - this is what makes per-guild custom
        # question sets (and correct review-channel routing) work at all from inside a DM.
        entry = get_types(guild_id).get(app_type)
        if not entry:
            self.active.discard(user.id)
            return await dm.send(embed=discord.Embed(
                description=f"The **{app_type}** application was removed while you were filling it out. "
                             "Ask staff to re-post the panel if you'd like to try a different one.",
                color=discord.Color.red(),
            ))
        questions = entry["questions"]
        answers = []

        def check(m: discord.Message):
            return m.author.id == user.id and isinstance(m.channel, discord.DMChannel)

        for idx, question in enumerate(questions, start=1):
            embed = discord.Embed(
                title=f"Question {idx} of {len(questions)}",
                description=f"{question}\n\n{ANSWER_HINT}",
                color=discord.Color.blue(),
            )
            await dm.send(embed=embed)
            try:
                reply = await self.bot.wait_for("message", check=check, timeout=900)
            except asyncio.TimeoutError:
                self.active.discard(user.id)
                timeout_embed = discord.Embed(
                    description=f"Your **{app_type}** application timed out. Start again whenever you're ready.",
                    color=discord.Color.red(),
                )
                return await dm.send(embed=timeout_embed)
            answers.append(reply.content)

        app_id = db.create_application(guild_id, user.id, app_type, answers)

        done_embed = discord.Embed(
            description=f"Your application for **{app_type}** has been submitted!",
            color=discord.Color.green(),
        )
        await dm.send(embed=done_embed)
        self.active.discard(user.id)

        review_embed = discord.Embed(
            title=f"{entry.get('emoji', '📋')} New {app_type} Application",
            color=discord.Color.blue(),
        )
        review_embed.add_field(name="Applicant", value=f"{user.mention} ({user})", inline=False)
        review_embed.add_field(name="Application ID", value=f"#{app_id}", inline=True)
        for q, a in zip(questions, answers):
            review_embed.add_field(name=q[:256], value=(a or "*no answer*")[:1024], inline=False)

        log_channel_id = entry.get("log_channel_id") or gc.get(guild_id, "application-log-channel")
        review_channel = self.bot.get_channel(log_channel_id) if log_channel_id else None
        if review_channel:
            await review_channel.send(embed=review_embed, view=ApplicationDecisionView(app_id))

    # ---------------- /applications (customization) ----------------

    applications_group = app_commands.Group(
        name="applications", description="[Admin] Customize this server's application types and questions",
    )

    @applications_group.command(name="list", description="See all application types configured for this server")
    async def apps_list(self, interaction: discord.Interaction):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if not types:
            return await interaction.response.send_message("No application types configured yet.", ephemeral=True)
        embed = discord.Embed(title="📋 Application Types", color=discord.Color.blurple())
        for name, entry in types.items():
            role_id = entry.get("role_id")
            role_line = f"\nGrants: <@&{role_id}>" if role_id else ""
            log_id = entry.get("log_channel_id")
            log_line = f"\nLog channel: <#{log_id}>" if log_id else ""
            embed.add_field(
                name=f"{entry.get('emoji', '📋')} {name}",
                value=f"{len(entry.get('questions', []))} question(s){role_line}{log_line}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @applications_group.command(name="create", description="Create a new application type for this server")
    @app_commands.describe(name="The application's name (shown in the panel)", emoji="Optional emoji to show next to it")
    async def apps_create(self, interaction: discord.Interaction, name: str, emoji: str = "📋"):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name in types:
            return await interaction.response.send_message(f"**{name}** already exists.", ephemeral=True)
        if len(types) >= MAX_APPLICATION_TYPES:
            return await interaction.response.send_message(
                f"This server already has the max of {MAX_APPLICATION_TYPES} application types.", ephemeral=True,
            )
        types[name] = {"emoji": emoji, "questions": []}
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(
            f"✅ Created **{name}**. Use `/applications add-question` to add questions to it.", ephemeral=True,
        )

    @applications_group.command(name="delete", description="Delete an application type from this server")
    @app_commands.describe(name="Which application type to delete")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_delete(self, interaction: discord.Interaction, name: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name not in types:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        del types[name]
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(f"🗑️ Deleted **{name}**.", ephemeral=True)

    @applications_group.command(name="set-emoji", description="Change the emoji shown for an application type")
    @app_commands.describe(name="Which application type", emoji="The new emoji")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_set_emoji(self, interaction: discord.Interaction, name: str, emoji: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name not in types:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        types[name]["emoji"] = emoji
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(f"✅ **{name}** now shows {emoji}.", ephemeral=True)

    @applications_group.command(name="set-role", description="Set the role auto-given when this application type is accepted")
    @app_commands.describe(name="Which application type", role="Role to grant on acceptance")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_set_role(self, interaction: discord.Interaction, name: str, role: discord.Role):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name not in types:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message(
                "That role is above my highest role — I wouldn't be able to assign it. Move my role higher and try again.",
                ephemeral=True,
            )
        types[name]["role_id"] = role.id
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(
            f"✅ Applicants accepted for **{name}** will now be given {role.mention}.", ephemeral=True,
        )

    @applications_group.command(name="clear-role", description="Stop auto-giving a role when this application type is accepted")
    @app_commands.describe(name="Which application type")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_clear_role(self, interaction: discord.Interaction, name: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        if name not in types:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        types[name].pop("role_id", None)
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(f"✅ **{name}** no longer auto-assigns a role on acceptance.", ephemeral=True)

    @applications_group.command(name="set-log-channel", description="Set which channel this application type's submissions get posted to")
    @app_commands.describe(name="Which application type", channel="Where submissions of this type should be posted for review")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_set_log_channel(self, interaction: discord.Interaction, name: str, channel: discord.TextChannel):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        entry = types.get(name)
        if not entry:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        entry["log_channel_id"] = channel.id
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(
            f"✅ **{name}** applications will now be posted to {channel.mention}.", ephemeral=True,
        )

    @applications_group.command(name="clear-log-channel", description="Stop overriding the log channel for an application type")
    @app_commands.describe(name="Which application type")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_clear_log_channel(self, interaction: discord.Interaction, name: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        entry = types.get(name)
        if not entry:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        entry.pop("log_channel_id", None)
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(
            f"✅ **{name}** will now use the server's default application log channel.", ephemeral=True,
        )

    @applications_group.command(name="questions", description="List the numbered questions for an application type")
    @app_commands.describe(name="Which application type")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_questions(self, interaction: discord.Interaction, name: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        entry = types.get(name)
        if not entry:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        questions = entry.get("questions", [])
        if not questions:
            return await interaction.response.send_message(f"**{name}** has no questions yet.", ephemeral=True)
        listing = "\n".join(f"**{i}.** {q}" for i, q in enumerate(questions, start=1))
        embed = discord.Embed(title=f"{entry.get('emoji', '📋')} {name} — Questions", description=listing[:4000],
                               color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @applications_group.command(name="add-question", description="Add a question to an application type")
    @app_commands.describe(name="Which application type", question="The question text")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_add_question(self, interaction: discord.Interaction, name: str, question: str):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        entry = types.get(name)
        if not entry:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        entry.setdefault("questions", []).append(question)
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(
            f"✅ Added as question #{len(entry['questions'])} for **{name}**.", ephemeral=True,
        )

    @applications_group.command(name="remove-question", description="Remove a question from an application type by its number")
    @app_commands.describe(name="Which application type", question_number="The question's number - see /applications questions")
    @app_commands.autocomplete(name=type_name_autocomplete)
    async def apps_remove_question(self, interaction: discord.Interaction, name: str, question_number: int):
        if not is_admin_perm(interaction.user):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)
        types = get_types(interaction.guild_id)
        entry = types.get(name)
        if not entry:
            return await interaction.response.send_message(f"No application type called **{name}**.", ephemeral=True)
        questions = entry.get("questions", [])
        if not (1 <= question_number <= len(questions)):
            return await interaction.response.send_message(
                f"**{name}** only has {len(questions)} question(s) — use a number between 1 and {len(questions)}.",
                ephemeral=True,
            )
        removed = questions.pop(question_number - 1)
        save_types(interaction.guild_id, types)
        await interaction.response.send_message(f"🗑️ Removed question #{question_number}: {removed[:200]}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Applications(bot))
