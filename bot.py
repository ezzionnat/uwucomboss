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

# roblox open cloud
roblox_api_key = os.getenv("roblox_api_key", "").strip()
print("roblox_api_key present:", bool(roblox_api_key))
print("roblox_api_key length:", len(roblox_api_key))

# fixed
ROBLOX_GROUP_ID = "174571331"

# logging channel
LOG_CHANNEL_ID = 1466623945514942506

embed_color = discord.Color.green()
valid_roles = {"owners", "manager", "staff", "tag_manager"}

ROBLOX_BASE = "https://apis.roblox.com/cloud/v2"
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

def pretty_level(level: str) -> str:
    if level == "owners":
        return "Owner"
    if level == "tag_manager":
        return "Tag Manager"
    if level == "manager":
        return "Manager"
    if level == "staff":
        return "Staff"
    return "None"


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


def parse_membership_id_from_path(path: str) -> Optional[str]:
    if not path:
        return None
    parts = str(path).split("/")
    if len(parts) < 4:
        return None
    if parts[-2] != "memberships":
        return None
    mid = parts[-1].strip()
    return mid or None


async def roblox_list_roles(client: httpx.AsyncClient) -> list[dict]:
    r = await client.get(f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/roles", headers=roblox_headers())
    r.raise_for_status()
    data = r.json() if r.content else {}
    return data.get("groupRoles") or data.get("roles") or []


async def roblox_get_membership(client: httpx.AsyncClient, user_id: int) -> Optional[dict]:
    params = {"maxPageSize": "10", "filter": f"user == 'users/{int(user_id)}'"}
    r = await client.get(
        f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships",
        headers=roblox_headers(),
        params=params,
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    memberships = data.get("groupMemberships") or data.get("memberships") or []
    if not memberships:
        return None
    return memberships[0]

async def roblox_avatar_url(client: httpx.AsyncClient, user_id: int) -> str:
    try:
        r = await client.get(
            "https://thumbnails.roblox.com/v1/users/avatar-headshot",
            params={
                "userIds": str(user_id),
                "size": "150x150",
                "format": "Png",
                "isCircular": "true",
            },
            timeout=10,
        )
        data = r.json()
        return data["data"][0]["imageUrl"]
    except Exception:
        return ""

async def roblox_members_in_role(client: httpx.AsyncClient, role_id: int) -> list[dict]:
    members: list[dict] = []
    page_token: str | None = None

    while True:
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token

        r = await client.get(
            f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships",
            headers=roblox_headers(),
            params=params,
        )
        r.raise_for_status()
        data = r.json()

        for m in data.get("groupMemberships", []):
            role_path = str(m.get("role") or "")
            rid = parse_role_id_from_path(role_path)
            if rid == role_id:
                members.append(m)

        page_token = data.get("nextPageToken")
        if not page_token:
            break

    return members


async def roblox_set_role_by_membership_id(client: httpx.AsyncClient, membership_id: str, role_id: int) -> None:
    body = {"role": f"groups/{ROBLOX_GROUP_ID}/roles/{int(role_id)}"}
    r = await client.patch(
        f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships/{membership_id}",
        headers=roblox_headers(),
        json=body,
    )

    if r.status_code >= 400:
        try:
            data = r.json()
        except Exception:
            data = None
        if data:
            raise RuntimeError(f"roblox error {r.status_code}: {data}")
        txt = (r.text or "")[:300]
        raise RuntimeError(f"roblox error {r.status_code}: {txt}")


class credit_bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pool: Optional[asyncpg.Pool] = None
        self.rbx_http: Optional[httpx.AsyncClient] = None

        self._rbx_roles: list[dict] = []
        self._rbx_lowest_assignable_role_id: Optional[int] = None

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
        rows = await con.fetch("select role from whitelist_roles where user_id = $1;", user_id)
    return {str(r["role"]) for r in rows}


def resolve_level_from_roles(roles: set[str]) -> str:
    if "owners" in roles:
        return "owners"
    if "tag_manager" in roles:
        return "tag_manager"
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

    if command in {"role", "unrole", "roles", "rolecheck"}:
        return level in {"owners", "tag_manager"}

    if level == "owners":
        return True

    if level == "manager":
        return command not in {"whitelist", "unwhitelist", "wipe"}

    if level == "staff":
        return command not in {"setcredits", "whitelist", "unwhitelist", "wipe"}

    return False


async def require_access(interaction: discord.Interaction, command: str, ephemeral: bool = True) -> bool:
    uid = int(interaction.user.id)
    level = await get_access_level(uid)
    if not can_use_command(level, command):
        if interaction.response.is_done():
            await interaction.followup.send("you do not have permission to use this command.", ephemeral=ephemeral)
        else:
            await interaction.response.send_message("you do not have permission to use this command.", ephemeral=ephemeral)
        return False
    return True


def role_choices() -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name="owners", value="owners"),
        app_commands.Choice(name="manager", value="manager"),
        app_commands.Choice(name="staff", value="staff"),
        app_commands.Choice(name="tag_manager", value="tag_manager"),
    ]


async def ensure_roblox_roles_loaded(force: bool = False) -> None:
    if not roblox_api_key:
        return
    if bot.rbx_http is None:
        return
    if bot._rbx_roles and not force:
        return

    roles = await roblox_list_roles(bot.rbx_http)
    bot._rbx_roles = roles

    lowest_rank: Optional[int] = None
    lowest_role_id: Optional[int] = None

    for r in roles:
        display = str(r.get("displayName") or "").strip().lower()
        if display == "guest":
            continue

        try:
            rk = int(r.get("rank"))
        except Exception:
            continue
        if rk <= 0:
            continue

        rid: Optional[int] = None
        if "id" in r:
            try:
                rid = int(r.get("id"))
            except Exception:
                rid = None
        if rid is None:
            role_path = str(r.get("path") or r.get("name") or "")
            rid = parse_role_id_from_path(role_path)
        if rid is None:
            continue

        if lowest_rank is None or rk < lowest_rank:
            lowest_rank = rk
            lowest_role_id = rid

    bot._rbx_lowest_assignable_role_id = lowest_role_id


def rbx_role_info_by_id(role_id: int) -> tuple[str, str]:
    for r in bot._rbx_roles:
        rid: Optional[int] = None
        if "id" in r:
            try:
                rid = int(r.get("id"))
            except Exception:
                rid = None
        if rid is None:
            role_path = str(r.get("path") or r.get("name") or "")
            rid = parse_role_id_from_path(role_path)

        if rid == int(role_id):
            display = str(r.get("displayName") or "unknown")
            rank = str(r.get("rank") or "unknown")
            return display, rank

    return "unknown", "unknown"


async def ranking_autocomplete(interaction: discord.Interaction, current: str):
    try:
        await ensure_roblox_roles_loaded()
    except Exception:
        return []

    current = (current or "").lower().strip()
    out: list[app_commands.Choice[str]] = []

    for r in bot._rbx_roles:
        display = str(r.get("displayName") or "").strip()
        if not display:
            continue

        if current and current not in display.lower():
            continue

        rid: Optional[int] = None
        if "id" in r:
            try:
                rid = int(r.get("id"))
            except Exception:
                rid = None
        if rid is None:
            role_path = str(r.get("path") or r.get("name") or "")
            rid = parse_role_id_from_path(role_path)

        if rid is None:
            continue

        out.append(app_commands.Choice(name=f"{display} ({rid})", value=str(rid)))
        if len(out) >= 25:
            break

    return out


async def send_role_log(interaction: discord.Interaction, text: str) -> None:
    # logs even if the command was used in dms or outside a guild
    try:
        ch = interaction.client.get_channel(LOG_CHANNEL_ID)
        if ch is None:
            ch = await interaction.client.fetch_channel(LOG_CHANNEL_ID)
        await ch.send(text)
    except Exception:
        return

async def roblox_list_memberships_page(client: httpx.AsyncClient, page_token: str | None = None) -> dict:
    params: dict[str, str] = {"maxPageSize": "100"}
    if page_token:
        params["pageToken"] = page_token

    r = await client.get(
        f"{ROBLOX_BASE}/groups/{ROBLOX_GROUP_ID}/memberships",
        headers=roblox_headers(),
        params=params,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


async def roblox_iter_memberships(client: httpx.AsyncClient):
    page_token: str | None = None
    while True:
        data = await roblox_list_memberships_page(client, page_token)
        items = data.get("groupMemberships") or data.get("memberships") or []
        for m in items:
            yield m
        page_token = data.get("nextPageToken") or None
        if not page_token:
            break


def parse_membership_id_from_path(membership_path: str) -> Optional[str]:
    # examples:
    # "groups/174571331/memberships/XXXXXXXX"
    # "groups/174571331/memberships/MjUwNzIyNjE4x0A"
    if not membership_path:
        return None
    s = str(membership_path).strip()
    last = s.split("/")[-1].strip()
    return last or None


# -------------------------
# roblox commands
# -------------------------

@bot.tree.command(name="roles", description="list all roles in the roblox group")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roles_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "roles", ephemeral=False):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=False)
        return

    if bot.rbx_http is None:
        await interaction.response.send_message("roblox http client not ready.", ephemeral=False)
        return

    await interaction.response.defer(thinking=True)

    try:
        await ensure_roblox_roles_loaded(force=True)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    if not bot._rbx_roles:
        await interaction.followup.send("no roles returned.", ephemeral=False)
        return

    lines: list[str] = []
    for r in bot._rbx_roles[:50]:
        display = str(r.get("displayName") or "unknown")
        rank = str(r.get("rank") or "unknown")

        rid: Optional[int] = None
        if "id" in r:
            try:
                rid = int(r.get("id"))
            except Exception:
                rid = None
        if rid is None:
            role_path = str(r.get("path") or r.get("name") or "")
            rid = parse_role_id_from_path(role_path)

        rid_str = str(rid) if rid is not None else "unknown"
        lines.append(f"- {display} | rank {rank} | role_id `{rid_str}`")

    e = make_embed("roblox group roles", lines)
    await interaction.followup.send(embed=e, ephemeral=False)


@bot.tree.command(name="rolecheck", description="check a roblox user's current group role")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(id="roblox user id (or username)")
async def rolecheck_cmd(interaction: discord.Interaction, id: str):
    # rolecheck must be invisible
    if not await require_access(interaction, "rolecheck", ephemeral=True):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=True)
        return

    if bot.rbx_http is None:
        await interaction.response.send_message("roblox http client not ready.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True, ephemeral=True)

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

    current_role_path = str(m.get("role") or "")
    current_role_id = parse_role_id_from_path(current_role_path)

    if current_role_id is None:
        await interaction.followup.send(f"user `{target_user_id}` role: unknown", ephemeral=True)
        return

    name, rank = rbx_role_info_by_id(int(current_role_id))
    await interaction.followup.send(
        f"user `{target_user_id}` role: {name} (rank {rank}) | role_id `{current_role_id}`",
        ephemeral=True,
    )


@bot.tree.command(name="role", description="rank a roblox user to a role in the group")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(id="roblox user id (or username)", ranking="pick a role (autocomplete)")
@app_commands.autocomplete(ranking=ranking_autocomplete)
async def role_cmd(interaction: discord.Interaction, id: str, ranking: str):
    if not await require_access(interaction, "role", ephemeral=False):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=False)
        return

    if bot.rbx_http is None:
        await interaction.response.send_message("roblox http client not ready.", ephemeral=False)
        return

    await interaction.response.defer(thinking=True)

    raw = (id or "").strip()
    target_user_id: Optional[int] = None
    if raw.isdigit():
        target_user_id = int(raw)
    else:
        target_user_id = await roblox_username_to_user_id(bot.rbx_http, raw)

    if not target_user_id:
        await interaction.followup.send("invalid id. provide a roblox user id or username.", ephemeral=False)
        return

    if not is_digits(ranking):
        await interaction.followup.send("invalid ranking selection.", ephemeral=False)
        return

    role_id = int(ranking)

    try:
        await ensure_roblox_roles_loaded()
    except Exception:
        pass

    try:
        m = await roblox_get_membership(bot.rbx_http, int(target_user_id))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    if not m:
        await interaction.followup.send("user is not in the group.", ephemeral=False)
        return

    membership_path = str(m.get("path") or "")
    membership_id = parse_membership_id_from_path(membership_path)
    if not membership_id:
        await interaction.followup.send(f"could not read membership id. path: `{membership_path}`", ephemeral=False)
        return

    current_role_path = str(m.get("role") or "")
    current_role_id = parse_role_id_from_path(current_role_path)

    base_role = bot._rbx_lowest_assignable_role_id
    
    old_name = None
    if current_role_id is not None:
        old_name, _ = rbx_role_info_by_id(int(current_role_id))

    try:
        await roblox_set_role_by_membership_id(bot.rbx_http, membership_id, role_id)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    new_name, _ = rbx_role_info_by_id(role_id)

        # public response
    await interaction.followup.send(
        f"roled `{target_user_id}` to `{new_name}`",
        ephemeral=False
    )

    # log message
    # if they already had a real role (not base), log it as a change
    if (
        base_role is not None
        and current_role_id is not None
        and int(current_role_id) != int(base_role)
        and old_name
    ):
        log_msg = (
            f"{interaction.user.mention} changed `{target_user_id}` "
            f"from `{old_name}` to `{new_name}`"
        )
    else:
        log_msg = (
            f"{interaction.user.mention} has roled `{target_user_id}` "
            f"to `{new_name}`"
        )
    
    await send_role_log(interaction, log_msg)



@bot.tree.command(name="unrole", description="remove a user's rank (sets them to the lowest assignable group role)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(id="roblox user id (or username)")
async def unrole_cmd(interaction: discord.Interaction, id: str):
    if not await require_access(interaction, "unrole", ephemeral=False):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=False)
        return

    if bot.rbx_http is None:
        await interaction.response.send_message("roblox http client not ready.", ephemeral=False)
        return

    await interaction.response.defer(thinking=True)

    raw = (id or "").strip()
    target_user_id: Optional[int] = None
    if raw.isdigit():
        target_user_id = int(raw)
    else:
        target_user_id = await roblox_username_to_user_id(bot.rbx_http, raw)

    if not target_user_id:
        await interaction.followup.send("invalid id. provide a roblox user id or username.", ephemeral=False)
        return

    try:
        await ensure_roblox_roles_loaded(force=True)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    base_role = bot._rbx_lowest_assignable_role_id
    if base_role is None:
        await interaction.followup.send("could not determine lowest assignable role in group.", ephemeral=False)
        return

    try:
        m = await roblox_get_membership(bot.rbx_http, int(target_user_id))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    if not m:
        await interaction.followup.send("user is not in the group.", ephemeral=False)
        return

    membership_path = str(m.get("path") or "")
    membership_id = parse_membership_id_from_path(membership_path)
    if not membership_id:
        await interaction.followup.send(f"could not read membership id. path: `{membership_path}`", ephemeral=False)
        return

    try:
        await roblox_set_role_by_membership_id(bot.rbx_http, membership_id, int(base_role))
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    role_name, _rank = rbx_role_info_by_id(int(base_role))

    # public response (NOT the same as log)
    await interaction.followup.send(f"successfully cleared roles for `{target_user_id}`", ephemeral=False)

    # log message (minimalistic)
    log_msg = f"{interaction.user.mention} has unroled `{target_user_id}` and their role is now set to `{role_name}`"
    await send_role_log(interaction, log_msg)


# -------------------------
# credits + whitelist cmds
# -------------------------

@bot.tree.command(name="credits", description="check credits for a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to check (defaults to you)")
async def credits_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    if not await require_access(interaction, "credits", ephemeral=True):
        return

    target = user or interaction.user
    amount = await get_credits(int(target.id))
    e = make_embed(f"{target.name} credits", [f"**{format_credits(amount)} credits**"])
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="creditsleaderboard", description="show credits leaderboard")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def creditsleaderboard_cmd(interaction: discord.Interaction):
    if not await require_access(interaction, "creditsleaderboard", ephemeral=True):
        return

    rows = await leaderboard_rows()
    if not rows:
        e = make_embed("credits leaderboard", ["no one has credits yet."])
        await interaction.response.send_message(embed=e, ephemeral=True)
        return

    lines: list[str] = []
    for i, r in enumerate(rows, start=1):
        uid = int(r["user_id"])
        amt = int(r["credits"])
        lines.append(f"{i}. <@{uid}> - {format_credits(amt)} credits")

    e = make_embed("credits leaderboard", lines)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="addcredits", description="add credits to a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to add credits to (defaults to you)", amount="amount to add")
async def addcredits_cmd(interaction: discord.Interaction, amount: int, user: Optional[discord.User] = None):
    if not await require_access(interaction, "addcredits", ephemeral=True):
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
    if not await require_access(interaction, "subcredits", ephemeral=True):
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
    if not await require_access(interaction, "setcredits", ephemeral=True):
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
    # wipe must be invisible
    if not await require_access(interaction, "wipe", ephemeral=True):
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        await con.execute("delete from credits;")

    await interaction.response.send_message("wiped all credits.", ephemeral=True)

@bot.tree.command(
    name="user-to-id",
    description="convert a roblox username to a user id"
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(username="roblox username")
async def user_to_id_cmd(interaction: discord.Interaction, username: str):
    if not roblox_api_key:
        await interaction.response.send_message(
            "roblox api key is missing.",
            ephemeral=True
        )
        return

    assert bot.rbx_http is not None

    await interaction.response.defer(ephemeral=False)

    user_id = await roblox_username_to_user_id(bot.rbx_http, username)

    if not user_id:
        await interaction.followup.send(
            f"could not find roblox user `{username}`.",
            ephemeral=False
        )
        return

    await interaction.followup.send(
        f"roblox user `{username}` → id `{user_id}`",
        ephemeral=False
    )


@bot.tree.command(name="whitelist", description="give a stored whitelist role to a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to whitelist", role="which role to add")
@app_commands.choices(role=role_choices())
async def whitelist_cmd(interaction: discord.Interaction, user: discord.User, role: app_commands.Choice[str]):
    if not await require_access(interaction, "whitelist", ephemeral=True):
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
        f"granted `{role_value}` to {user.mention} meow",
        ephemeral=False,
    )


@bot.tree.command(name="unwhitelist", description="remove all stored whitelist roles from a user")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(user="the user to unwhitelist")
async def unwhitelist_cmd(interaction: discord.Interaction, user: discord.User):
    if not await require_access(interaction, "unwhitelist", ephemeral=True):
        return

    assert bot.pool is not None
    async with bot.pool.acquire() as con:
        res = await con.execute("delete from whitelist_roles where user_id = $1;", int(user.id))

    await interaction.response.send_message(
        f"removed stored roles from <@{int(user.id)}>* ({res.lower()}).",
        ephemeral=True,
    )

@bot.tree.command(name="inrole", description="list members in a roblox group role")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(role="pick a role (autocomplete)")
@app_commands.autocomplete(role=ranking_autocomplete)
async def inrole_cmd(interaction: discord.Interaction, role: str):
    if not await require_access(interaction, "whitelist"):
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox api key.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)

    if not role.isdigit():
        await interaction.followup.send("invalid role.", ephemeral=False)
        return

    role_id = int(role)

    try:
        await ensure_roblox_roles_loaded()
    except Exception:
        pass

    # role name
    role_name = "unknown role"
    for r in bot._rbx_roles:
        rp = str(r.get("path") or r.get("name") or "")
        rid = parse_role_id_from_path(rp)
        if rid == role_id:
            role_name = str(r.get("displayName") or r.get("name") or role_name).strip()
            break

    try:
        members = await roblox_members_in_role(bot.rbx_http, role_id)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    if not members:
        await interaction.followup.send(f"no members found in **{role_name}**.", ephemeral=False)
        return

    lines: list[str] = []

    for m in members:
        user_path = str(m.get("user") or "")
        if not user_path:
            continue

        try:
            user_id = int(user_path.split("/")[-1])
        except Exception:
            continue

        # fix date
        raw_time = str(m.get("updateTime") or "")
        date = "unknown"
        if raw_time and not raw_time.startswith("0001-01-01"):
            date = raw_time.split("T")[0]

        # avatar url + profile url
        avatar_url = await roblox_avatar_url(bot.rbx_http, user_id)
        profile_url = f"https://www.roblox.com/users/{user_id}/profile"

        icon = "icon"
        if avatar_url:
            icon = f"[icon]({avatar_url})"

        lines.append(f"{icon} [{user_id}]({profile_url}) - roled: `{date}`")

    # chunk to avoid embed limit
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0

    for line in lines:
        if cur_len + len(line) + 1 > 3500 and cur:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append(line)
        cur_len += len(line) + 1

    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks, start=1):
        title = f"Members of {role_name}" if len(chunks) == 1 else f"Members of {role_name} ({i}/{len(chunks)})"
        e = make_embed(title, chunk)
        await interaction.followup.send(embed=e, ephemeral=False)

@bot.tree.command(name="rankinglist", description="list everyone whitelisted in the bot")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def rankinglist_cmd(interaction: discord.Interaction):
    # i’m making this owners only (same power level as whitelist/unwhitelist)
    if not await require_access(interaction, "whitelist"):
        return

    assert bot.pool is not None

    await interaction.response.defer(ephemeral=False)

    async with bot.pool.acquire() as con:
        rows = await con.fetch(
            """
            select user_id, role
            from whitelist_roles
            order by user_id asc, role asc;
            """
        )

    if not rows:
        await interaction.followup.send("no one is whitelisted.", ephemeral=False)
        return

    by_user: dict[int, set[str]] = {}
    for r in rows:
        uid = int(r["user_id"])
        role = str(r["role"]).lower().strip()
        by_user.setdefault(uid, set()).add(role)

    # build lines
    lines: list[str] = []
    for uid, roles in sorted(by_user.items(), key=lambda x: x[0]):
        level = resolve_level_from_roles(roles)
        lines.append(f"• <@{uid}> | {uid} | {pretty_level(level)}")

    # discord embed description limit is ~4096 chars, so chunk it
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) + 1 > 3800 and cur:
            chunks.append(cur)
            cur = []
            cur_len = 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        chunks.append(cur)

    for i, chunk in enumerate(chunks, start=1):
        title = "Whitelists to the bot" if len(chunks) == 1 else f"Whitelists to the bot ({i}/{len(chunks)})"
        e = make_embed(title, chunk)
        await interaction.followup.send(embed=e, ephemeral=False)

@bot.tree.command(
    name="group-wipe",
    description="reset everyone's role in the roblox group to the lowest role (owners only)"
)
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.describe(confirm="type true to confirm")
async def group_wipe_cmd(interaction: discord.Interaction, confirm: bool):
    # owners only, hard stop
    level = await get_access_level(int(interaction.user.id))
    if level != "owners":
        await interaction.response.send_message("you do not have permission to use this command.", ephemeral=True)
        return

    if not confirm:
        await interaction.response.send_message("set confirm to true to run `/group-wipe`.", ephemeral=True)
        return

    if not roblox_api_key:
        await interaction.response.send_message("missing roblox_api_key in environment variables.", ephemeral=True)
        return

    assert bot.rbx_http is not None

    await interaction.response.defer(ephemeral=False)

    # make sure we know the lowest role
    try:
        await ensure_roblox_roles_loaded(force=True)
    except Exception as e:
        await interaction.followup.send(f"failed: {e}", ephemeral=False)
        return

    lowest = bot._rbx_lowest_assignable_role_id
    if lowest is None:
        await interaction.followup.send("could not determine lowest role in group.", ephemeral=False)
        return

    lowest_name, _ = rbx_role_info_by_id(int(lowest))

    changed = 0
    scanned = 0
    failed = 0

    try:
        async for m in roblox_iter_memberships(bot.rbx_http):
            scanned += 1

            membership_path = str(m.get("path") or m.get("name") or "")
            membership_id = parse_membership_id_from_path(membership_path)
            if not membership_id:
                failed += 1
                continue

            current_role_path = str(m.get("role") or "")
            current_role_id = parse_role_id_from_path(current_role_path)

            # skip if already lowest
            if current_role_id is not None and int(current_role_id) == int(lowest):
                continue

            try:
                await roblox_set_role_by_membership_id(bot.rbx_http, membership_id, int(lowest))
                changed += 1
            except Exception:
                failed += 1

    except Exception as e:
        await interaction.followup.send(f"failed while scanning: {e}", ephemeral=False)
        return

    # public response
    await interaction.followup.send(
        f"group wipe complete. set `{changed}` users to `{lowest_name}`. scanned `{scanned}`. failed `{failed}`.",
        ephemeral=False,
    )

    # log
    await send_role_log(
        interaction,
        f"{interaction.user.mention} ran group wipe. set `{changed}` users to `{lowest_name}` (failed `{failed}`)",
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
