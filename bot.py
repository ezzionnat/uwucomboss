import os
from typing import Optional

import asyncpg
import discord
import httpx
from discord import app_commands
from discord.ext import commands


token = os.getenv("discord_token", "")
database_url = os.getenv("database_url", "")
guild_id_raw = os.getenv("guild_id", "")
owner_ids_raw = os.getenv("owner_ids", "")

# roblox open cloud (do not paste in code)
roblox_api_key = os.getenv("roblox_api_key", "").strip()
print("roblox_api_key present:", bool(roblox_api_key))
print("roblox_api_key length:", len(roblox_api_key))

# group id is fixed per your request
ROBLOX_GROUP_ID = "174571331"

embed_color = discord.Color.green()
valid_roles = {"owners", "manager", "staff"}

ROBLOX_BASE = "https://apis.roblox.com/cloud/v2"

# username -> id endpoint (optional, but useful)
ROBLOX_USERS = "https://users.roblox.com/v1"


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


def is_digits(s: str) -> bool:
    return bool(s) and s.isdigit()


async def roblox_username_to_user_id(client: httpx.AsyncClient, username: str) -> Optional[int]:
    username = (username or "").strip()
    if not username:
        return None

    payload = {"usernames": [username], "excludeBannedUsers": False}
    try:
        r = await client.post(f"{ROBLOX_USERS}/usernames/users", json=payload)
    except Exception:
        return None

    if r.status_code >= 400:
        return None

    data = r.json() if r.content else {}
    users = data.get("data") or []
    if not users:
        return None

    try:
        return int(users[0].get("id"))
    except Exception:
        return None


def roblox_headers() -> dict:
    return {"x-api-key": roblox_api_key, "content-type": "application/json"}


def parse_role_id_from_path(role_path: str) -> Optional[int]:
    if not role_path:
        return None
    try:
        last = str(role_path).split("/")[-1]
        return int(last) if str(last).isdigit() else None
    except Exception:
        return None


async def roblox_list_roles(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/roles", headers=roblox_headers())
    r.raise_for_status()
    data = r.json() if r.content else {}
    return data.get("roles") or []


async def roblox_get_membership(client: httpx.AsyncClient, user_id: int) -> Optional[dict]:
    params = {
        "maxPageSize": "10",
        "filter": f"user == 'users/{int(user_id)}'",
    }
    r = await client.get(
        f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships",
        headers=roblox_headers(),
        params=params,
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    memberships = data.get("memberships") or []
    if not memberships:
        return None
    return memberships[0]


async def roblox_set_role_by_membership_id(client: httpx.AsyncClient, membership_id: str, role_id: int) -> None:
    body = {"role": f"groups/{ROBLOX_GROUP_ID}/roles/{int(role_id)}"}
    r = await client.patch(
        f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships/{membership_id}",
        headers=roblox_headers(),
        json=body,
    )
    if r.status_code >= 400:
        txt = (r.text or "")[:300]
        raise RuntimeError(f"roblox error {r.status_code}: {txt}")


class credit_bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None
        self.rbx_http: Optional[httpx.AsyncClient] = None

        self._rbx_roles: list[dict] = []
        self._rbx_lowest_role_id: Optional[int] = None

    async def setup_hook(self):
        self.rbx_http = httpx.AsyncClient(timeout=25)

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
        if self.rbx_http:
            await self.rbx_http.aclose()
        if self.pool:
            await self.pool.close()
        await super().close()


bot = credit_bot()


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
        return command not in {"whitelist", "unwhitelist", "wipe"}

    if level == "staff":
        return command not in {"setcredits", "whitelist", "unwhitelist", "wipe"}

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


async def resolve_target_user(interaction: discord.Interaction, option_name: str) -> Optional[discord.User]:
    data = getattr(interaction, "data", None) or {}

    resolved = data.get("resolved") or {}
    users = resolved.get("users") or {}

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
            s = str(raw).replace("<@!", "").replace("<@", "").replace(">", "").strip()
            if not s.isdigit():
                return None
            uid = int(s)

        u = users.get(str(uid))
        if u is not None:
            try:
                return discord.User(state=interaction.client._connection, data=u)  # type: ignore
            except Exception:
                pass

        try:
            return await interaction.client.fetch_user(uid)
        except Exception:
            return None

    return None


async def ensure_roblox_roles_loaded(force: bool = False) -> None:
    if not roblox_api_key:
        return
    if bot.rbx_http is None:
        return

    if bot._rbx_roles and not force:
        return

    roles = await roblox_list_roles(bot.rbx_http)
    bot._rbx_roles = roles

    lowest_id: Optional[int] = None
    lowest_rank: Optional[int] = None
    for r in roles:
        role_path = str(r.get("name") or r.get("path") or "")
        rid = parse_role_id_from_path(role_path)
        if rid is None:
            continue
        try:
            rk = int(r.get("rank"))
        except Exception:
            continue
        if lowest_rank is None or rk < lowest_rank:
            lowest_rank = rk
            lowest_id = rid

    bot._rbx_lowest_role_id = lowest_id


async def ranking_autocomplete(interaction: discord.Interaction, current: str):
    try:
        await ensure_roblox_roles_loaded()
    except Exception:
        return []

    current = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []

    for r in bot._rbx_roles:
        display = str(r.get("displayName") or "").strip()
        role_path = str(r.get("name") or r.get("path") or "")
        rid = parse_role_id_from_path(role_path)
        if not display or rid is None:
            continue

        if current and current not in display.lower():
            continue

        out.append(app_commands.Choice(name=f"{display} ({rid})", value=str(rid)))
        if len(out) >= 25:
            break

    return out


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
    e = make_embed(f"{target.name} credits", [f"**{format_credits(amount)} credits**"])
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

    target = user
    if target is None:
        target = await resolve_target_user(interaction, "user")
    if target is None:
        target = interaction.user

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

    target = user
    if target is None:
        target = await resolve_target_user(interaction, "user")
    if target is None:
        target = interaction.user

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


@bot.tree.command(name="wipe", description="wipe all credits")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def wipe_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "wipe"):
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        await con.execute("delete from credits;")

    await interaction.response.send_message("wiped all credits.", ephemeral=True)


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


@bot.tree.command(name="roles", description="list all roles in the roblox group")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roles_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "whitelist"):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=True)
        return

    assert bot.rbx_http is not None
    await interaction.response.defer(ephemeral=True)

    try:
        await ensure_roblox_roles_loaded(force=True)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    if not bot._rbx_roles:
        await interaction.followup.send("no roles returned.", ephemeral=True)
        return

    lines: list[str] = []
    for r in bot._rbx_roles[:50]:
        display = str(r.get("displayName") or "unknown")
        rank = str(r.get("rank") or "unknown")
        role_path = str(r.get("name") or r.get("path") or "")
        rid = parse_role_id_from_path(role_path)
        rid_str = str(rid) if rid is not None else "unknown"
        lines.append(f"- {display} | rank {rank} | role_id `{rid_str}`")

    e = make_embed("roblox group roles", lines)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="role", description="rank a roblox user to a role in the group")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(id="roblox user id (or username)", ranking="pick a role (autocomplete)")
@app_commands.autocomplete(ranking=ranking_autocomplete)
async def role_cmd(interaction: discord.Interaction, id: str, ranking: str):
    if not await require_access(interaction, "whitelist"):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=True)
        return

    assert bot.rbx_http is not None
    await interaction.response.defer(ephemeral=True)

    raw = (id or "").strip()
    target_user_id: Optional[int] = None
    if raw.isdigit():
        target_user_id = int(raw)
    else:
        target_user_id = await roblox_username_to_user_id(bot.rbx_http, raw)

    if not target_user_id:
        await interaction.followup.send("invalid id. provide a roblox user id or username.", ephemeral=True)
        return

    if not is_digits(ranking):
        await interaction.followup.send("invalid ranking selection.", ephemeral=True)
        return

    role_id = int(ranking)

    try:
        await ensure_roblox_roles_loaded()
    except Exception:
        pass

    try:
        m = await roblox_get_membership(bot.rbx_http, int(target_user_id))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    if not m:
        await interaction.followup.send("user is not in the group.", ephemeral=True)
        return

    membership_name = str(m.get("name") or "")
    membership_id = membership_name.split("/")[-1] if membership_name else ""
    if not membership_id:
        await interaction.followup.send("could not read membership id.", ephemeral=True)
        return

    current_role_path = str(m.get("role") or "")
    current_role_id = parse_role_id_from_path(current_role_path)

    lowest = bot._rbx_lowest_role_id

    if current_role_id is not None and lowest is not None and current_role_id != lowest:
        await interaction.followup.send("user already has a rank in group, use /unrole on them.", ephemeral=True)
        return

    try:
        await roblox_set_role_by_membership_id(bot.rbx_http, membership_id, role_id)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"done. set `{target_user_id}` to role `{role_id}`.", ephemeral=True)


@bot.tree.command(name="unrole", description="remove a user's rank (sets them to the lowest group role)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(id="roblox user id (or username)")
async def unrole_cmd(interaction: discord.Interaction, id: str):
    if not await require_access(interaction, "whitelist"):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=True)
        return

    assert bot.rbx_http is not None
    await interaction.response.defer(ephemeral=True)

    raw = (id or "").strip()
    target_user_id: Optional[int] = None
    if raw.isdigit():
        target_user_id = int(raw)
    else:
        target_user_id = await roblox_username_to_user_id(bot.rbx_http, raw)

    if not target_user_id:
        await interaction.followup.send("invalid id. provide a roblox user id or username.", ephemeral=True)
        return

    try:
        await ensure_roblox_roles_loaded(force=True)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    lowest = bot._rbx_lowest_role_id
    if lowest is None:
        await interaction.followup.send("could not determine lowest role in group.", ephemeral=True)
        return

    try:
        m = await roblox_get_membership(bot.rbx_http, int(target_user_id))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    if not m:
        await interaction.followup.send("user is not in the group.", ephemeral=True)
        return

    membership_name = str(m.get("name") or "")
    membership_id = membership_name.split("/")[-1] if membership_name else ""
    if not membership_id:
        await interaction.followup.send("could not read membership id.", ephemeral=True)
        return

    try:
        await roblox_set_role_by_membership_id(bot.rbx_http, membership_id, int(lowest))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"done. reset `{target_user_id}` to lowest role `{lowest}`.", ephemeral=True)


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
