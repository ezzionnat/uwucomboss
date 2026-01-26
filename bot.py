import os
from typing import Optional

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands


token = os.getenv("discord_token", "")
database_url = os.getenv("database_url", "")
guild_id_raw = os.getenv("guild_id", "")
owner_ids_raw = os.getenv("owner_ids", "")

embed_color = discord.Color.green()
valid_roles = {"owners", "manager", "staff"}


def parse_owner_ids(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except Exception:
            pass
    return out


owner_ids = parse_owner_ids(owner_ids_raw)


def is_int(s: str) -> bool:
    try:
        int(s)
        return True
    except Exception:
        return False


def format_credits(n: int) -> str:
    return f"{n:,}"


def make_embed(title: str, lines: list[str]) -> discord.Embed:
    return discord.Embed(title=title, description="\n".join(lines), color=embed_color)


class credit_bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5)

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
                create table if not exists whitelist_roles (
                    user_id bigint not null,
                    role text not null,
                    primary key (user_id, role)
                );
                """
            )

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

async def resolve_target_user(interaction: discord.Interaction, option_name: str) -> Optional[discord.User]:
    data = getattr(interaction, "data", None) or {}

    # best case: discord includes resolved objects
    resolved = data.get("resolved") or {}
    users = resolved.get("users") or {}
    members = resolved.get("members") or {}

    options = data.get("options") or []
    for opt in options:
        if opt.get("name") != option_name:
            continue

        raw = opt.get("value")
        if raw is None:
            return None

        try:
            uid = int(raw)
        except Exception:
            # sometimes it comes as "<@id>" or "<@!id>"
            s = str(raw).replace("<@!", "").replace("<@", "").replace(">", "").strip()
            if not s.isdigit():
                return None
            uid = int(s)

        # try resolved user first
        u = users.get(str(uid))
        if u is not None:
            try:
                return discord.User(state=interaction.client._connection, data=u)  # type: ignore
            except Exception:
                pass

        # if member resolved exists, still fetch as user
        if str(uid) in members:
            try:
                return await interaction.client.fetch_user(uid)
            except Exception:
                return None

        # final fallback: fetch from api
        try:
            return await interaction.client.fetch_user(uid)
        except Exception:
            return None

    return None

async def get_credits(user_id: int) -> int:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow("select credits from credits where user_id = $1;", user_id)
        return int(row["credits"]) if row else 0


async def set_credits(user_id: int, amount: int) -> int:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow(
            """
            insert into credits (user_id, credits)
            values ($1, $2)
            on conflict (user_id) do update set credits = excluded.credits
            returning credits;
            """,
            user_id,
            amount,
        )
    return int(row["credits"])


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
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        row = await con.fetchrow(
            """
            insert into credits (user_id, credits)
            values ($1, 0)
            on conflict (user_id) do update
            set credits = greatest(credits.credits - $2, 0)
            returning credits;
            """,
            user_id,
            delta,
        )
    return int(row["credits"])


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


async def get_user_roles(user_id: int) -> set[str]:
    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        rows = await con.fetch(
            "select role from whitelist_roles where user_id = $1;",
            user_id,
        )
    return {str(r["role"]) for r in rows}


def resolve_level_from_roles(roles: set[str]) -> str:
    if "owners" in roles:
        return "owners"
    if "manager" in roles:
        return "manager"
    if "staff" in roles:
        return "staff"
    return "none"


async def get_access_level(user_id: int) -> str:
    if user_id in owner_ids:
        return "owners"
    roles = await get_user_roles(user_id)
    return resolve_level_from_roles(roles)


def can_use_command(level: str, command: str) -> bool:
    if command in {"credits", "creditsleaderboard"}:
        return True

    if level == "owners":
        return True

    if level == "manager":
        return command not in {"whitelist", "unwhitelist"}

    if level == "staff":
        return command not in {"setcredits", "whitelist", "unwhitelist"}

    return False


async def require_access(interaction: discord.Interaction, command: str) -> bool:
    uid = int(interaction.user.id)
    level = await get_access_level(uid)
    if not can_use_command(level, command):
        if interaction.response.is_done():
            await interaction.followup.send("you do not have permission to use this command.", ephemeral=True)
        else:
            await interaction.response.send_message("you do not have permission to use this command.", ephemeral=True)
        return False
    return True


def role_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name="owners", value="owners"),
        app_commands.Choice(name="manager", value="manager"),
        app_commands.Choice(name="staff", value="staff"),
    ]


@bot.tree.command(name="credits", description="check credits for a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to check (defaults to you)")
async def credits_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    if not await require_access(interaction, "credits"):
        return

    target = user
    if target is None:
        target = await resolve_target_user(interaction, "user")
    if target is None:
        target = interaction.user

    amount = await get_credits(int(target.id))
    e = make_embed("timedeal credits", [f"**{format_credits(amount)} credits**"])
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="creditsleaderboard", description="show credits leaderboard")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def creditsleaderboard_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "creditsleaderboard"):
        return

    rows = await leaderboard_rows()
    if not rows:
        e = make_embed("credits leaderboard", ["no one has credits yet."])
        await interaction.response.send_message(embed=e)
        return

    lines: list[str] = []
    for i, r in enumerate(rows, start=1):
        uid = int(r["user_id"])
        amt = int(r["credits"])
        lines.append(f"{i}. <@{uid}> - {format_credits(amt)} credits")

    e = make_embed("credits leaderboard", lines)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="addcredits", description="add credits to a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to add credits to (defaults to you)", amount="amount to add")
async def addcredits_cmd(interaction: discord.Interaction, amount: int, user: Optional[discord.User] = None):
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
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to subtract credits from (defaults to you)", amount="amount to subtract")
async def subcredits_cmd(interaction: discord.Interaction, amount: int, user: Optional[discord.User] = None):
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
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to set credits for", amount="new credits amount")
async def setcredits_cmd(interaction: discord.Interaction, user: discord.User, amount: int):
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


@bot.tree.command(name="whitelist", description="give a stored whitelist role to a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to whitelist", role="which role to add")
@app_commands.choices(role=role_choices())
async def whitelist_cmd(interaction: discord.Interaction, user: discord.User, role: app_commands.Choice[str]):
    if not await require_access(interaction, "whitelist"):
        return

    role_value = str(role.value).lower().strip()
    if role_value not in valid_roles:
        await interaction.response.send_message("invalid role.", ephemeral=True)
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        await con.execute(
            """
            insert into whitelist_roles (user_id, role)
            values ($1, $2)
            on conflict do nothing;
            """,
            int(user.id),
            role_value,
        )

    await interaction.response.send_message(
        f"added stored role `{role_value}` to <@{int(user.id)}>*.",
        ephemeral=True,
    )


@bot.tree.command(name="unwhitelist", description="remove all stored whitelist roles from a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to unwhitelist")
async def unwhitelist_cmd(interaction: discord.Interaction, user: discord.User):
    if not await require_access(interaction, "unwhitelist"):
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        res = await con.execute("delete from whitelist_roles where user_id = $1;", int(user.id))

    await interaction.response.send_message(
        f"removed stored roles from <@{int(user.id)}>* ({res.lower()}).",
        ephemeral=True,
    )


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
