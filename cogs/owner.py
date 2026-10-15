import discord
from discord import app_commands
from discord.ext import commands

import command_perms
import permissions


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="owner", description="[Owner] Manage who has owner permissions")
    @app_commands.describe(action="Add, remove or list owners", user="The user (not needed for List)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Add", value="add"),
        app_commands.Choice(name="Remove", value="remove"),
        app_commands.Choice(name="List", value="list"),
    ])
    async def owner(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                    user: discord.User = None):
        if not permissions.is_owner(interaction.user.id):
            return await interaction.response.send_message("you dont have the perms", ephemeral=True)

        if action.value == "list":
            lines = [f"<@{uid}>" + (" (root)" if permissions.is_root(uid) else "")
                     for uid in sorted(permissions.owner_ids())]
            return await interaction.response.send_message(
                embed=discord.Embed(title="👑 Owners", description="\n".join(lines), color=discord.Color.gold()),
                ephemeral=True,
            )

        if not permissions.is_root(interaction.user.id):
            return await interaction.response.send_message(
                "you dont have the perms", ephemeral=True)
        if user is None:
            return await interaction.response.send_message("Pick a user.", ephemeral=True)

        if action.value == "add":
            if permissions.add_owner(user.id):
                msg = f"✅ {user.mention} now has owner permissions."
            else:
                msg = f"{user.mention} is already an owner."
        else:
            if permissions.is_root(user.id):
                msg = "The root owner can't be removed."
            elif permissions.remove_owner(user.id):
                msg = f"🗑️ {user.mention} no longer has owner permissions."
            else:
                msg = f"{user.mention} isn't on the owner list."
        await interaction.response.send_message(msg, ephemeral=True)

    # ---------------- /commandperms ----------------

    @app_commands.command(name="commandperms",
                          description="[Owner] Choose which roles can use which commands")
    @app_commands.describe(
        action="Allow a role, remove a role, list the rules, or reset a command to its default",
        command="The command, e.g. purge, say, or \"role add\"",
        role="The role (for Allow / Remove)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Allow role", value="allow"),
        app_commands.Choice(name="Remove role", value="remove"),
        app_commands.Choice(name="List", value="list"),
        app_commands.Choice(name="Reset to default", value="reset"),
    ])
    async def commandperms(self, interaction: discord.Interaction, action: app_commands.Choice[str],
                           command: str = None, role: discord.Role = None):
        if not permissions.is_owner(interaction.user.id):
            return await interaction.response.send_message(command_perms.NO_PERMS_MESSAGE, ephemeral=True)
        if interaction.guild is None:
            return await interaction.response.send_message("Use this in the server.", ephemeral=True)
        gid = interaction.guild.id

        if action.value == "list":
            return await interaction.response.send_message(
                embed=self._rules_embed(interaction.guild, command), ephemeral=True)

        name = command_perms.normalize(command)
        if not name:
            return await interaction.response.send_message("Pick a command.", ephemeral=True)
        if name not in command_perms.all_command_names(self.bot.tree):
            return await interaction.response.send_message(
                f"I don't have a command called `/{name}`. Start typing in the command box to pick one.",
                ephemeral=True)
        if name.split(" ", 1)[0] in command_perms.NOT_CONFIGURABLE:
            return await interaction.response.send_message(
                f"`/{name}` is hard-wired to the owner list and can't be given to roles.", ephemeral=True)

        if action.value == "reset":
            had = command_perms.reset(gid, name)
            msg = (f"♻️ `/{name}` is back to its default"
                   + (" (owner only)." if name.split(" ", 1)[0] in command_perms.OWNER_ONLY_DEFAULT
                      else " permissions.")
                   if had else f"`/{name}` has no custom rule.")
        else:
            if role is None:
                return await interaction.response.send_message("Pick a role.", ephemeral=True)
            if action.value == "allow":
                if command_perms.add_role(gid, name, role.id):
                    msg = (f"✅ {role.mention} can now use `/{name}`. "
                           "From now on only owners and the roles on its list can use it.")
                else:
                    msg = f"{role.mention} can already use `/{name}`."
            else:
                if command_perms.remove_role(gid, name, role.id):
                    left = command_perms.get_roles(gid, name)
                    msg = (f"🗑️ {role.mention} can no longer use `/{name}`."
                           + ("" if left else " It has no roles left, so it's back to its default."))
                else:
                    msg = f"{role.mention} isn't on the list for `/{name}`."

        # Command visibility in Discord's picker depends on the rules, so re-apply and re-sync.
        await interaction.response.defer(ephemeral=True)
        try:
            self.bot._apply_command_permissions()
            await self.bot.sync_commands()
        except discord.HTTPException:
            msg += "\n⚠️ Saved, but I couldn't re-sync the command list - run `!sync` to finish."
        await interaction.followup.send(msg, ephemeral=True)

    @commandperms.autocomplete("command")
    async def _command_autocomplete(self, interaction: discord.Interaction, current: str):
        current = command_perms.normalize(current)
        names = [n for n in command_perms.all_command_names(self.bot.tree)
                 if n.split(" ", 1)[0] not in command_perms.NOT_CONFIGURABLE and current in n]
        return [app_commands.Choice(name=f"/{n}", value=n) for n in names[:25]]

    @staticmethod
    def _rules_embed(guild: discord.Guild, command: str = None) -> discord.Embed:
        rules = command_perms.all_rules(guild.id)
        lines = []

        def fmt(ids):
            return ", ".join(f"<@&{i}>" if guild.get_role(i) else f"~~deleted role {i}~~" for i in ids)

        if command:
            name = command_perms.normalize(command)
            ids = command_perms.effective_roles(guild.id, name)
            if ids:
                lines.append(f"`/{name}` - {fmt(ids)} (and owners)")
            elif name.split(" ", 1)[0] in command_perms.OWNER_ONLY_DEFAULT:
                lines.append(f"`/{name}` - owner only (default)")
            else:
                lines.append(f"`/{name}` - no custom rule, default permissions")
        else:
            for n in sorted(command_perms.OWNER_ONLY_DEFAULT):
                if n not in rules:
                    lines.append(f"`/{n}` - owner only (default)")
            for n, ids in sorted(rules.items()):
                lines.append(f"`/{n}` - {fmt(ids)} (and owners)")
            if not lines:
                lines.append("No rules yet.")
        body = "\n".join(lines)[:4000]
        return discord.Embed(title="🔐 Command permissions", description=body, color=discord.Color.gold())


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
