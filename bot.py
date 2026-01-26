import os
import asyncio
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands


token = os.getenv("DISCORD_TOKEN", "")
database_url = os.getenv("DATABASE_URL", "")
guild_id_raw = os.getenv("GUILD_ID", "")

owners_role_name = "owners"
manager_role_name = "manager"
staff_role_name = "staff"

embed_color = discord.Color.green()


def is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except Exception:
        return False


def format_credits(n: int) -> str:
    return f"{n:,}"


def credits_embed(title: str, lines: list[str]) -> discord.Embed:
    e = discord.Embed(title=title, description="\n".join(lines), color=embed_color)
    return e


def has_role(member: discord.Member, role_name: str) -> bool:
    return any(r.name == role_name for r in member.roles)


def get_access_level(member: discord.Member) -> str:
    if has_role(member, owners_role_name):
        return "owners"
    if has_role(member, manager_role_name):
        return "manager"
    if has_role(member, staff_role_name):
        return "staff"
    return "none"


def can_use_command(member: discord.Member, command: str) -> bool:
    level = get_access_level(member)

    # everyone can view
    if command in {"credits", "creditsleaderboard"}:
        return True

    if level == "owners":
        return True

    if level == "manager":
        # manager cannot whitelist or unwhitelist
        return command not in {"whitelist", "unwhitelist"}

    if level == "staff":
        # staff cannot setcredits, whitelist, unwhitelist
        return command not in {"setcredits", "whitelist", "unwhitelist"}

    return False


class credit_bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        # connect db
        self.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

        # migrate
        async with self.pool.acquire() as con:
            await con.execute(
                """
                create table if not exists credits (
                    user_id bigint primary key,
                    credits bigint not null default 0
                );
                """
            )
            await con.execute(
                """
                create table if not exists whitelists (
                    user_id bigint not null,
                    role_id bigint not null,
                    primary key (user_id, role_id)
                );
                """
            )

        # sync commands
        if guild_id_raw and is_int(guild_id_raw):
            guild = discord.Object(id=int(guild_id_raw))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print("synced commands to guild")
        else:
            await self.tree.sync()
            print("synced commands globally")

    async def close(self):
        if self.pool:
            await self.pool.close()
        await super().close()


bot = credit_bot()


async def get_credits(user_id: int) -> int:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow("select credits from credits where user_id = $1;", user_id)
        if not row:
            return 0
        return int(row["credits"])


async def set_credits(user_id: int, amount: int) -> int:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        await con.execute(
            """
            insert into credits (user_id, credits)
            values ($1, $2)
            on conflict (user_id) do update set credits = excluded.credits;
            """,
            user_id,
            amount,
        )
    return amount


async def add_credits(user_id: int, delta: int) -> int:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow(
            """
            insert into credits (user_id, credits)
            values ($1, $2)
            on conflict (user_id) do update set credits = credits.credits + $2
            returning credits;
            """,
            user_id,
            delta,
        )
    return int(row["credits"])


async def sub_credits(user_id: int, delta: int) -> int:
    # subtract but never go below 0
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow("select credits from credits where user_id = $1;", user_id)
        cur = int(row["credits"]) if row else 0
        new_val = cur - delta
        if new_val < 0:
            new_val = 0
        await set_credits(user_id, new_val)
    return new_val


async def leaderboard_rows() -> list[asyncpg.Record]:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        rows = await con.fetch(
            """
            select user_id, credits
            from credits
            where credits > 0
            order by credits desc, user_id asc;
            """
        )
    return rows


def role_choice_autocomplete() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name="owners", value="owners"),
        app_commands.Choice(name="manager", value="manager"),
        app_commands.Choice(name="staff", value="staff"),
    ]


async def require_access(interaction: discord.Interaction, command: str) -> bool:
    if not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("this command can only be used in a server.", ephemeral=True)
        return False

    member: discord.Member = interaction.user
    if not can_use_command(member, command):
        await interaction.response.send_message("you do not have permission to use this command.", ephemeral=True)
        return False

    return True


@bot.tree.command(name="credits", description="check credits for a user")
@app_commands.describe(user="the user to check (defaults to you)")
async def credits_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    if not await require_access(interaction, "credits"):
        return

    target = user or interaction.user
    amount = await get_credits(int(target.id))

    e = credits_embed(
        "timedeal credits",
        [f"**{format_credits(amount)} credits**"],
    )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="creditsleaderboard", description="show credits leaderboard")
async def leaderboard_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "creditsleaderboard"):
        return

    rows = await leaderboard_rows()
    if not rows:
        e = credits_embed("credits leaderboard", ["no one has credits yet."])
        await interaction.response.send_message(embed=e)
        return

    lines: list[str] = []
    for i, r in enumerate(rows, start=1):
        uid = int(r["user_id"])
        amt = int(r["credits"])
        lines.append(f"{i}. <@{uid}> \u2014 {format_credits(amt)} credits")

    e = credits_embed("credits leaderboard", lines)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="addcredits", description="add credits to a user")
@app_commands.describe(user="the user to add credits to (defaults to you)", amount="amount to add")
async def addcredits_cmd(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
    if not await require_access(interaction, "addcredits"):
        return

    if amount <= 0:
        await interaction.response.send_message("amount must be greater than 0.", ephemeral=True)
        return

    target = user or interaction.user
    new_val = await add_credits(int(target.id), int(amount))
    await interaction.response.send_message(
        f"added {format_credits(amount)} credits to <@{int(target.id)}>. new total: {format_credits(new_val)}.",
        ephemeral=True,
    )


@bot.tree.command(name="subcredits", description="subtract credits from a user")
@app_commands.describe(user="the user to subtract credits from (defaults to you)", amount="amount to subtract")
async def subcredits_cmd(interaction: discord.Interaction, amount: int, user: Optional[discord.Member] = None):
    if not await require_access(interaction, "subcredits"):
        return

    if amount <= 0:
        await interaction.response.send_message("amount must be greater than 0.", ephemeral=True)
        return

    target = user or interaction.user
    new_val = await sub_credits(int(target.id), int(amount))
    await interaction.response.send_message(
        f"subtracted {format_credits(amount)} credits from <@{int(target.id)}>. new total: {format_credits(new_val)}.",
        ephemeral=True,
    )


@bot.tree.command(name="setcredits", description="set a user credits to an exact amount")
@app_commands.describe(user="the user to set credits for", amount="new credits amount")
async def setcredits_cmd(interaction: discord.Interaction, user: discord.Member, amount: int):
    if not await require_access(interaction, "setcredits"):
        return

    if amount < 0:
        await interaction.response.send_message("amount cannot be negative.", ephemeral=True)
        return

    new_val = await set_credits(int(user.id), int(amount))
    await interaction.response.send_message(
        f"set <@{int(user.id)}> credits to {format_credits(new_val)}.",
        ephemeral=True,
    )


@bot.tree.command(name="whitelist", description="add a whitelist role to a user")
@app_commands.describe(user="the user to role", role="which role to add")
@app_commands.choices(role=role_choice_autocomplete())
async def whitelist_cmd(interaction: discord.Interaction, user: discord.Member, role: app_commands.Choice[str]):
    if not await require_access(interaction, "whitelist"):
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("this command can only be used in a server.", ephemeral=True)
        return

    role_name = role.value
    target_role = discord.utils.get(guild.roles, name=role_name)
    if target_role is None:
        await interaction.response.send_message(f"role `{role_name}` was not found in this server.", ephemeral=True)
        return

    try:
        await user.add_roles(target_role, reason="whitelist command")
    except discord.Forbidden:
        await interaction.response.send_message("i do not have permission to add that role.", ephemeral=True)
        return

    # record so unwhitelist can remove what the bot added
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        await con.execute(
            """
            insert into whitelists (user_id, role_id)
            values ($1, $2)
            on conflict do nothing;
            """,
            int(user.id),
            int(target_role.id),
        )

    await interaction.response.send_message(f"added role `{role_name}` to <@{int(user.id)}>*.", ephemeral=True)


@bot.tree.command(name="unwhitelist", description="remove all whitelist roles added to a user")
@app_commands.describe(user="the user to unwhitelist")
async def unwhitelist_cmd(interaction: discord.Interaction, user: discord.Member):
    if not await require_access(interaction, "unwhitelist"):
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("this command can only be used in a server.", ephemeral=True)
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        rows = await con.fetch(
            "select role_id from whitelists where user_id = $1;",
            int(user.id),
        )

    if not rows:
        await interaction.response.send_message("no roles to remove for that user.", ephemeral=True)
        return

    removed = 0
    for r in rows:
        rid = int(r["role_id"])
        role_obj = guild.get_role(rid)
        if role_obj is None:
            continue
        try:
            if role_obj in user.roles:
                await user.remove_roles(role_obj, reason="unwhitelist command")
                removed += 1
        except discord.Forbidden:
            pass

    async with bot.pool.acquire() as con:
        await con.execute("delete from whitelists where user_id = $1;", int(user.id))

    await interaction.response.send_message(f"removed {removed} whitelist role(s) from <@{int(user.id)}>*.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"logged in as {bot.user} ({bot.user.id})")


def main():
    if not token:
        raise RuntimeError("missing discord_token")
    if not database_url:
        raise RuntimeError("missing database_url")
    bot.run(token)


if __name__ == "__main__":
    main()
