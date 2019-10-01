import logging
import textwrap
from typing import Awaitable, Dict, Optional, Union

import dateutil.parser
from discord import (
    Colour, Embed, Forbidden, HTTPException, Member, NotFound, Object, User
)
from discord.ext.commands import BadUnionArgument, Bot, Cog, Context, command

from bot import constants
from bot.api import ResponseCodeError
from bot.constants import Colours, Event, Icons
from bot.converters import Duration
from bot.decorators import respect_role_hierarchy
from bot.utils.checks import with_role_check
from bot.utils.scheduling import Scheduler
from bot.utils.time import format_infraction, wait_until
from .modlog import ModLog
from .utils import (
    Infraction, MemberObject, already_has_active_infraction, post_infraction, proxy_user
)

log = logging.getLogger(__name__)

# apply icon, pardon icon
INFRACTION_ICONS = {
    "mute": (Icons.user_mute, Icons.user_unmute),
    "kick": (Icons.sign_out, None),
    "ban": (Icons.user_ban, Icons.user_unban),
    "warning": (Icons.user_warn, None),
    "note": (Icons.user_warn, None),
}
RULES_URL = "https://pythondiscord.com/pages/rules"
APPEALABLE_INFRACTIONS = ("ban", "mute")


MemberConverter = Union[Member, User, proxy_user]


class Infractions(Scheduler, Cog):
    """Server moderation tools."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self._muted_role = Object(constants.Roles.muted)
        super().__init__()

    @property
    def mod_log(self) -> ModLog:
        """Get currently loaded ModLog cog instance."""
        return self.bot.get_cog("ModLog")

    @Cog.listener()
    async def on_ready(self) -> None:
        """Schedule expiration for previous infractions."""
        infractions = await self.bot.api_client.get(
            'bot/infractions',
            params={'active': 'true'}
        )
        for infraction in infractions:
            if infraction["expires_at"] is not None:
                self.schedule_task(self.bot.loop, infraction["id"], infraction)

    # region: Permanent infractions

    @command()
    async def warn(self, ctx: Context, user: MemberConverter, *, reason: str = None) -> None:
        """Warn a user for the given reason."""
        infraction = await post_infraction(ctx, user, reason, "warning")
        if infraction is None:
            return

        await self.apply_infraction(ctx, infraction, user)

    @command()
    async def kick(self, ctx: Context, user: Member, *, reason: str = None) -> None:
        """Kick a user for the given reason."""
        await self.apply_kick(ctx, user, reason)

    @command()
    async def ban(self, ctx: Context, user: MemberConverter, *, reason: str = None) -> None:
        """Permanently ban a user for the given reason."""
        await self.apply_ban(ctx, user, reason)

    # endregion
    # region: Temporary infractions

    @command(aliases=('mute',))
    async def tempmute(self, ctx: Context, user: Member, duration: Duration, *, reason: str = None) -> None:
        """
        Temporarily mute a user for the given reason and duration.

        A unit of time should be appended to the duration:
        y (years), m (months), w (weeks), d (days), h (hours), M (minutes), s (seconds)
        """
        await self.apply_mute(ctx, user, reason, expires_at=duration)

    @command()
    async def tempban(self, ctx: Context, user: MemberConverter, duration: Duration, *, reason: str = None) -> None:
        """
        Temporarily ban a user for the given reason and duration.

        A unit of time should be appended to the duration:
        y (years), m (months), w (weeks), d (days), h (hours), M (minutes), s (seconds)
        """
        await self.apply_ban(ctx, user, reason, expires_at=duration)

    # endregion
    # region: Permanent shadow infractions

    @command(hidden=True)
    async def note(self, ctx: Context, user: MemberConverter, *, reason: str = None) -> None:
        """Create a private note for a user with the given reason without notifying the user."""
        infraction = await post_infraction(ctx, user, reason, "note", hidden=True)
        if infraction is None:
            return

        await self.apply_infraction(ctx, infraction, user)

    @command(hidden=True, aliases=['shadowkick', 'skick'])
    async def shadow_kick(self, ctx: Context, user: Member, *, reason: str = None) -> None:
        """Kick a user for the given reason without notifying the user."""
        await self.apply_kick(ctx, user, reason, hidden=True)

    @command(hidden=True, aliases=['shadowban', 'sban'])
    async def shadow_ban(self, ctx: Context, user: MemberConverter, *, reason: str = None) -> None:
        """Permanently ban a user for the given reason without notifying the user."""
        await self.apply_ban(ctx, user, reason, hidden=True)

    # endregion
    # region: Temporary shadow infractions

    @command(hidden=True, aliases=["shadowtempmute, stempmute", "shadowmute", "smute"])
    async def shadow_tempmute(
        self, ctx: Context, user: Member, duration: Duration, *, reason: str = None
    ) -> None:
        """
        Temporarily mute a user for the given reason and duration without notifying the user.

        A unit of time should be appended to the duration:
        y (years), m (months), w (weeks), d (days), h (hours), M (minutes), s (seconds)
        """
        await self.apply_mute(ctx, user, reason, expires_at=duration, hidden=True)

    @command(hidden=True, aliases=["shadowtempban, stempban"])
    async def shadow_tempban(
        self, ctx: Context, user: MemberConverter, duration: Duration, *, reason: str = None
    ) -> None:
        """
        Temporarily ban a user for the given reason and duration without notifying the user.

        A unit of time should be appended to the duration:
        y (years), m (months), w (weeks), d (days), h (hours), M (minutes), s (seconds)
        """
        await self.apply_ban(ctx, user, reason, expires_at=duration, hidden=True)

    # endregion
    # region: Remove infractions (un- commands)

    @command()
    async def unmute(self, ctx: Context, user: MemberConverter) -> None:
        """Deactivates the active mute infraction for a user."""
        await self.pardon_infraction(ctx, "mute", user)

    @command()
    async def unban(self, ctx: Context, user: MemberConverter) -> None:
        """Deactivates the active ban infraction for a user."""
        await self.pardon_infraction(ctx, "ban", user)

    # endregion
    # region: Base infraction functions

    async def apply_mute(self, ctx: Context, user: Member, reason: str, **kwargs) -> None:
        """Apply a mute infraction with kwargs passed to `post_infraction`."""
        if await already_has_active_infraction(ctx, user, "mute"):
            return

        infraction = await post_infraction(ctx, user, "mute", reason, **kwargs)
        if infraction is None:
            return

        self.mod_log.ignore(Event.member_update, user.id)

        action = user.add_roles(self._muted_role, reason=reason)
        await self.apply_infraction(ctx, infraction, user, action)

    @respect_role_hierarchy()
    async def apply_kick(self, ctx: Context, user: Member, reason: str, **kwargs) -> None:
        """Apply a kick infraction with kwargs passed to `post_infraction`."""
        infraction = await post_infraction(ctx, user, type="kick", **kwargs)
        if infraction is None:
            return

        self.mod_log.ignore(Event.member_remove, user.id)

        action = user.kick(reason=reason)
        await self.apply_infraction(ctx, infraction, user, action)

    @respect_role_hierarchy()
    async def apply_ban(self, ctx: Context, user: MemberObject, reason: str, **kwargs) -> None:
        """Apply a ban infraction with kwargs passed to `post_infraction`."""
        if await already_has_active_infraction(ctx, user, "ban"):
            return

        infraction = await post_infraction(ctx, user, reason, "ban", **kwargs)
        if infraction is None:
            return

        self.mod_log.ignore(Event.member_ban, user.id)
        self.mod_log.ignore(Event.member_remove, user.id)

        action = ctx.guild.ban(user, reason=reason, delete_message_days=0)
        await self.apply_infraction(ctx, infraction, user, action)

    # endregion
    # region: Utility functions

    def cancel_expiration(self, infraction_id: str) -> None:
        """Un-schedules a task set to expire a temporary infraction."""
        task = self.scheduled_tasks.get(infraction_id)
        if task is None:
            log.warning(f"Failed to unschedule {infraction_id}: no task found.")
            return
        task.cancel()
        log.debug(f"Unscheduled {infraction_id}.")
        del self.scheduled_tasks[infraction_id]

    async def _scheduled_task(self, infraction: Infraction) -> None:
        """
        Marks an infraction expired after the delay from time of scheduling to time of expiration.

        At the time of expiration, the infraction is marked as inactive on the website and the
        expiration task is cancelled.
        """
        _id = infraction["id"]

        expiry = dateutil.parser.isoparse(infraction["expires_at"]).replace(tzinfo=None)
        await wait_until(expiry)

        log.debug(f"Marking infraction {_id} as inactive (expired).")
        await self.deactivate_infraction(infraction)
        self.cancel_task(_id)

    async def deactivate_infraction(
        self,
        infraction: Infraction,
        send_log: bool = True
    ) -> Dict[str, str]:
        """
        Deactivate an active infraction and return a dictionary of lines to send in a mod log.

        The infraction is removed from Discord and then marked as inactive in the database.
        Any scheduled expiration tasks for the infractions are NOT cancelled or unscheduled.

        If `send_log` is True, a mod log is sent for the deactivation of the infraction.

        Supported infraction types are mute and ban. Other types will raise a ValueError.
        """
        guild = self.bot.get_guild(constants.Guild.id)
        user_id = infraction["user"]
        _type = infraction["type"]
        _id = infraction["id"]
        reason = f"Infraction #{_id} expired or was pardoned."

        log_text = {
            "Member": str(user_id),
            "Actor": str(self.bot)
        }

        try:
            if _type == "mute":
                user = guild.get_member(user_id)
                if user:
                    # Remove the muted role.
                    self.mod_log.ignore(Event.member_update, user.id)
                    await user.remove_roles(self._muted_role, reason=reason)

                    # DM the user about the expiration.
                    notified = await self.notify_pardon(
                        user=user,
                        title="You have been unmuted.",
                        content="You may now send messages in the server.",
                        icon_url=INFRACTION_ICONS["mute"][1]
                    )

                    log_text["DM"] = "Sent" if notified else "**Failed**"
                else:
                    log.info(f"Failed to unmute user {user_id}: user not found")
                    log_text["Failure"] = "User was not found in the guild."
            elif _type == "ban":
                user = Object(user_id)
                try:
                    await guild.unban(user, reason=reason)
                except NotFound:
                    log.info(f"Failed to unban user {user_id}: no active ban found on Discord")
                    log_text["Failure"] = "No active ban found on Discord."
            else:
                raise ValueError(
                    f"Attempted to deactivate an unsupported infraction #{_id} ({_type})!"
                )
        except Forbidden:
            log.warning(f"Failed to deactivate infraction #{_id} ({_type}): bot lacks permissions")
            log_text["Failure"] = f"The bot lacks permissions to do this (role hierarchy?)"
        except HTTPException as e:
            log.exception(f"Failed to deactivate infraction #{_id} ({_type})")
            log_text["Failure"] = f"HTTPException with code {e.code}."

        try:
            # Mark infraction as inactive in the database.
            await self.bot.api_client.patch(
                f"bot/infractions/{_id}",
                json={"active": False}
            )
        except ResponseCodeError as e:
            log.exception(f"Failed to deactivate infraction #{_id} ({_type})")
            log_line = f"API request failed with code {e.status}."

            # Append to an existing failure message if possible
            if "Failure" in log_text:
                log_text["Failure"] += f" {log_line}"
            else:
                log_text["Failure"] = log_line

        # Send a log message to the mod log.
        if send_log:
            log_title = f"expiration failed" if "Failure" in log_text else "expired"

            await self.mod_log.send_log_message(
                icon_url=INFRACTION_ICONS[_type][1],
                colour=Colour(Colours.soft_green),
                title=f"Infraction {log_title}: {_type}",
                text="\n".join(f"{k}: {v}" for k, v in log_text.items()),
                footer=f"Infraction ID: {_id}",
            )

        return log_text

    async def notify_infraction(
        self,
        user: MemberObject,
        infr_type: str,
        expires_at: Optional[str] = None,
        reason: Optional[str] = None
    ) -> bool:
        """
        Attempt to notify a user, via DM, of their fresh infraction.

        Returns a boolean indicator of whether the DM was successful.
        """
        embed = Embed(
            description=textwrap.dedent(f"""
                **Type:** {infr_type.capitalize()}
                **Expires:** {expires_at or "N/A"}
                **Reason:** {reason or "No reason provided."}
                """),
            colour=Colour(Colours.soft_red)
        )

        icon_url = INFRACTION_ICONS[infr_type][0]
        embed.set_author(name="Infraction Information", icon_url=icon_url, url=RULES_URL)
        embed.title = f"Please review our rules over at {RULES_URL}"
        embed.url = RULES_URL

        if infr_type in APPEALABLE_INFRACTIONS:
            embed.set_footer(text="To appeal this infraction, send an e-mail to appeals@pythondiscord.com")

        return await self.send_private_embed(user, embed)

    async def notify_pardon(
        self,
        user: MemberObject,
        title: str,
        content: str,
        icon_url: str = Icons.user_verified
    ) -> bool:
        """
        Attempt to notify a user, via DM, of their expired infraction.

        Optionally returns a boolean indicator of whether the DM was successful.
        """
        embed = Embed(
            description=content,
            colour=Colour(Colours.soft_green)
        )

        embed.set_author(name=title, icon_url=icon_url)

        return await self.send_private_embed(user, embed)

    async def send_private_embed(self, user: MemberObject, embed: Embed) -> bool:
        """
        A helper method for sending an embed to a user's DMs.

        Returns a boolean indicator of DM success.
        """
        try:
            # sometimes `user` is a `discord.Object`, so let's make it a proper user.
            user = await self.bot.fetch_user(user.id)

            await user.send(embed=embed)
            return True
        except (HTTPException, Forbidden, NotFound):
            log.debug(
                f"Infraction-related information could not be sent to user {user} ({user.id}). "
                "The user either could not be retrieved or probably disabled their DMs."
            )
            return False

    async def apply_infraction(
        self,
        ctx: Context,
        infraction: Infraction,
        user: MemberObject,
        action_coro: Optional[Awaitable] = None
    ) -> None:
        """Apply an infraction to the user, log the infraction, and optionally notify the user."""
        infr_type = infraction["type"]
        icon = INFRACTION_ICONS[infr_type][0]
        reason = infraction["reason"]
        expiry = infraction["expires_at"]

        if expiry:
            expiry = format_infraction(expiry)

        confirm_msg = f":ok_hand: applied"
        expiry_msg = f" until {expiry}" if expiry else " permanently"
        dm_result = ""
        dm_log_text = ""
        expiry_log_text = f"Expires: {expiry}" if expiry else ""
        log_title = "applied"
        log_content = None

        if not infraction["hidden"]:
            if await self.notify_infraction(user, infr_type, expiry, reason):
                dm_result = ":incoming_envelope: "
                dm_log_text = "\nDM: Sent"
            else:
                dm_log_text = "\nDM: **Failed**"
                log_content = ctx.author.mention

        if action_coro:
            try:
                await action_coro
                if expiry:
                    self.schedule_task(ctx.bot.loop, infraction["id"], infraction)
            except Forbidden:
                confirm_msg = f":x: failed to apply"
                expiry_msg = ""
                log_content = ctx.author.mention
                log_title = "failed to apply"

        await ctx.send(f"{dm_result}{confirm_msg} **{infr_type}** to {user.mention}{expiry_msg}.")

        await self.mod_log.send_log_message(
            icon_url=icon,
            colour=Colour(Colours.soft_red),
            title=f"Infraction {log_title}: {infr_type}",
            thumbnail=user.avatar_url_as(static_format="png"),
            text=textwrap.dedent(f"""
                Member: {user.mention} (`{user.id}`)
                Actor: {ctx.message.author}{dm_log_text}
                Reason: {reason}
                {expiry_log_text}
            """),
            content=log_content,
            footer=f"ID {infraction['id']}"
        )

    async def pardon_infraction(self, ctx: Context, infr_type: str, user: MemberObject) -> None:
        """Prematurely end an infraction for a user and log the action in the mod log."""
        # Check the current active infraction
        response = await self.bot.api_client.get(
            'bot/infractions',
            params={
                'active': 'true',
                'type': infr_type,
                'user__id': user.id
            }
        )

        if not response:
            await ctx.send(f":x: There's no active {infr_type} infraction for user {user.mention}.")
            return

        # Deactivate the infraction and cancel its scheduled expiration task.
        log_text = await self.deactivate_infraction(response[0], send_log=False)
        if response[0]["expires_at"] is not None:
            self.cancel_expiration(response[0]["id"])

        log_text["Member"] = f"{user.mention}(`{user.id}`)"
        log_text["Actor"] = str(ctx.message.author)
        log_content = None
        footer = f"Infraction ID: {response[0]['id']}"

        # If multiple active infractions were found, mark them as inactive in the database
        # and cancel their expiration tasks.
        if len(response) > 1:
            log.warning(f"Found more than one active {infr_type} infraction for user {user.id}")

            footer = f"Infraction IDs: {', '.join(str(infr['id']) for infr in response)}"
            log_text["Note"] = f"Found multiple **active** {infr_type} infractions in the database."

            # deactivate_infraction() is not called again because:
            #     1. Discord cannot store multiple active bans or assign multiples of the same role
            #     2. It would send a pardon DM for each active infraction, which is redundant
            for infraction in response[1:]:
                _id = infraction['id']
                try:
                    # Mark infraction as inactive in the database.
                    await self.bot.api_client.patch(
                        f"bot/infractions/{_id}",
                        json={"active": False}
                    )
                except ResponseCodeError:
                    log.exception(f"Failed to deactivate infraction #{_id} ({infr_type})")
                    # This is simpler and cleaner than trying to concatenate all the errors.
                    log_text["Failure"] = "See bot's logs for details."

                # Cancel pending expiration tasks.
                if infraction["expires_at"] is not None:
                    self.cancel_expiration(infraction["id"])

        # Accordingly display whether the user was successfully notified via DM.
        dm_emoji = ""
        if log_text.get("DM") == "Sent":
            dm_emoji = ":incoming_envelope: "
        elif "DM" in log_text:
            # Mention the actor because the DM failed to send.
            log_content = ctx.author.mention

        # Accordingly display whether the pardon failed.
        if "Failure" in log_text:
            confirm_msg = ":x: failed to pardon"
            log_title = "pardon failed"
            log_content = ctx.author.mention
        else:
            confirm_msg = f":ok_hand: pardoned"
            log_title = "pardoned"

        # Send the confirmation message to the invoking context.
        await ctx.send(
            f"{dm_emoji}{confirm_msg} infraction **{infr_type}** for {user.mention}. "
            f"{log_text.get('Failure', '')}"
        )

        # Send a log message to the mod log.
        await self.mod_log.send_log_message(
            icon_url=INFRACTION_ICONS[infr_type][1],
            colour=Colour(Colours.soft_green),
            title=f"Infraction {log_title}: {infr_type}",
            thumbnail=user.avatar_url_as(static_format="png"),
            text="\n".join(f"{k}: {v}" for k, v in log_text.items()),
            footer=footer,
            content=log_content,
        )

    # endregion

    # This cannot be static (must have a __func__ attribute).
    def cog_check(self, ctx: Context) -> bool:
        """Only allow moderators to invoke the commands in this cog."""
        return with_role_check(ctx, *constants.MODERATION_ROLES)

    # This cannot be static (must have a __func__ attribute).
    async def cog_command_error(self, ctx: Context, error: Exception) -> None:
        """Send a notification to the invoking context on a Union failure."""
        if isinstance(error, BadUnionArgument):
            if User in error.converters:
                await ctx.send(str(error.errors[0]))
                error.handled = True
