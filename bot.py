"""Astra Magical Girl: daily landmark hunt Discord bot.

Requirements: discord.py>=2.4, python-decouple>=3.8
Environment: DISCORD_TOKEN in .env
Run: python bot.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from decouple import config
from discord import app_commands
from discord.ext import commands, tasks

BASE = Path(__file__).resolve().parent
LANDMARK_FILE = BASE / "landmark_data.json"
PLAYER_FILE = BASE / "player_data.json"
IMAGE_DIR = BASE / "images"
GAME_TZ = timezone(timedelta(hours=8))
PAGE_SIZE = 10
LEFT, RIGHT = "⬅️", "➡️"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("astra")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def normalize(value: str) -> str:
    return value.casefold().strip()


def letters(value: str) -> str:
    return "".join(c for c in value.casefold() if c.isalpha())


def distance(a: dict, b: dict) -> float:
    return math.hypot(b["coords"]["x"] - a["coords"]["x"], b["coords"]["z"] - a["coords"]["z"])


def direction(current: dict, target: dict) -> str:
    """Give just ONE valid Minecraft cardinal direction, even for diagonals."""
    dx = target["coords"]["x"] - current["coords"]["x"]
    dz = target["coords"]["z"] - current["coords"]["z"]
    choices = []
    if dx > 0:
        choices.append("East")
    elif dx < 0:
        choices.append("West")
    if dz > 0:
        choices.append("South")
    elif dz < 0:
        choices.append("North")
    return random.choice(choices) if choices else "Here"


def count_hint(guess: str, answer: str) -> str:
    g, a = len(letters(guess)), len(letters(answer))
    if a > g:
        return "Nova's landmark has **more** letters."
    if a < g:
        return "Nova's landmark has **fewer** letters."
    return "Nova's landmark has an **equal** number of letters."


def pattern(guess: str, answer: str) -> str:
    g, a = letters(guess), letters(answer)
    return " ".join(x.upper() if x == y else "_" for x, y in zip(g, a))


class AstraBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.reactions = True
        super().__init__(command_prefix="!", intents=intents)
        raw = load_json(LANDMARK_FILE, [])
        if not isinstance(raw, list):
            raise ValueError("landmark_data.json must contain a JSON list")
        self.landmarks = []
        self.by_name = {}
        for item in raw:
            try:
                name = item["name"].strip()
                float(item["coords"]["x"])
                float(item["coords"]["z"])
                if normalize(name) in self.by_name:
                    raise ValueError(f"Duplicate landmark name: {name}")
                self.landmarks.append(item)
                self.by_name[normalize(name)] = item
            except (KeyError, TypeError, AttributeError, ValueError) as exc:
                raise ValueError(f"Invalid landmark: {item!r}") from exc
        if len(self.landmarks) < 2:
            raise ValueError("At least two distinct landmarks are required")
        self.state = load_json(PLAYER_FILE, {"days": {}})
        if not isinstance(self.state, dict) or not isinstance(self.state.get("days"), dict):
            raise ValueError("player_data.json must contain an object with a 'days' object")
        self.lock = asyncio.Lock()

    async def setup_hook(self):
        await self.tree.sync()
        self.date_check.start()

    async def close(self):
        self.date_check.cancel()
        await super().close()

    def today(self) -> str:
        return datetime.now(GAME_TZ).date().isoformat()

    def ensure_today(self) -> dict:
        today = self.today()
        days = self.state["days"]
        for date in list(days):
            if date not in {today, (datetime.now(GAME_TZ).date() - timedelta(days=1)).isoformat()}:
                del days[date]
        if today not in days:
            start, target = random.sample(self.landmarks, 2)
            days[today] = {"start": start["name"], "target": target["name"], "players": {}}
        save_json(PLAYER_FILE, self.state)
        return days[today]

    def player(self, day: dict, user: discord.abc.User) -> dict:
        uid = str(user.id)
        players = day["players"]
        if uid not in players:
            players[uid] = {"name": user.display_name, "current": day["start"],
                            "guesses": 0, "distance": 0.0, "solved": False,
                            "visited": [], "revealed": {}}
        players[uid]["name"] = user.display_name
        return players[uid]

    @tasks.loop(minutes=1)
    async def date_check(self):
        async with self.lock:
            self.ensure_today()

    @date_check.before_loop
    async def before_check(self):
        await self.wait_until_ready()


bot = AstraBot()


def landmark_page(items: list, index: int, title: str) -> discord.Embed:
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    index = max(0, min(index, pages - 1))
    embed = discord.Embed(title=title, color=discord.Color.purple())
    chunk = items[index * PAGE_SIZE : (index + 1) * PAGE_SIZE]
    if not chunk:
        embed.description = "No matching landmarks found."
    for entry in chunk:
        c = entry["coords"]
        embed.add_field(name=entry["name"], value=f"{entry.get('region', 'Unknown region')}\nX: {c['x']} · Z: {c['z']}", inline=False)
    embed.set_footer(text=f"Page {index + 1}/{pages} · {len(items)} landmarks" + (" · React ⬅️ / ➡️ to turn pages" if pages > 1 else ""))
    return embed


async def paginated(interaction: discord.Interaction, items: list, title: str):
    await interaction.response.defer()
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    message = await interaction.followup.send(embed=landmark_page(items, 0, title), wait=True)
    if pages <= 1:
        return
    # The bot places both arrows on its message so players can click them.
    try:
        for emoji in (LEFT, RIGHT):
            await message.add_reaction(emoji)
    except discord.Forbidden:
        await interaction.followup.send("I need the Add Reactions permission to enable page navigation.", ephemeral=True)
        return
    index = 0

    def check(reaction: discord.Reaction, user: discord.User | discord.Member) -> bool:
        return (user.id == interaction.user.id and reaction.message.id == message.id
                and str(reaction.emoji) in {LEFT, RIGHT})

    while True:
        try:
            reaction, user = await bot.wait_for("reaction_add", timeout=120, check=check)
        except asyncio.TimeoutError:
            break
        index = (index + (1 if str(reaction.emoji) == RIGHT else -1)) % pages
        await message.edit(embed=landmark_page(items, index, title))
        try:
            await message.remove_reaction(reaction.emoji, user)
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass
    try:
        await message.clear_reactions()
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.tree.command(name="landmarks", description="Browse landmarks (10 per page).")
async def landmarks_command(interaction: discord.Interaction):
    await paginated(interaction, bot.landmarks, "🗺️ All landmarks")


@bot.tree.command(name="search", description="Search landmark names or regions.")
@app_commands.describe(text="Part of a landmark name or region")
async def search_command(interaction: discord.Interaction, text: str):
    term = normalize(text)
    if not term:
        await interaction.response.send_message("Enter a name or region to search.", ephemeral=True)
        return
    results = [p for p in bot.landmarks if term in normalize(p["name"]) or term in normalize(p.get("region", ""))]
    await paginated(interaction, results, f"🔎 Search: {text[:80]}")


@bot.tree.command(name="daily", description="Start or resume today's hunt for Nova.")
async def daily_command(interaction: discord.Interaction):
    async with bot.lock:
        day = bot.ensure_today()
        p = bot.player(day, interaction.user)
        start = bot.by_name[normalize(day["start"])]
        bot.state["days"][bot.today()] = day
        save_json(PLAYER_FILE, bot.state)
        msg = (f"✨ Astra starts at **{start['name']}** ({start.get('region','Unknown')}).\n"
               f"🔤 {count_hint(start['name'], day['target'])}\n"
               f"📍 Current position: **{p['current']}**\n"
               f"🎯 Guesses: **{p['guesses']}** · Travel: **{p['distance']:.1f} blocks**\n"
               + ("✅ You already found Nova today!" if p["solved"] else "Use `/guess` to travel to a landmark."))
    await interaction.response.send_message(msg, ephemeral=True)


async def landmark_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    term = normalize(current)
    candidates = [p["name"] for p in bot.landmarks if term in normalize(p["name"])]
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in candidates[:25]]


@bot.tree.command(name="guess", description="Guess where Nova is hiding and travel there.")
@app_commands.describe(landmark="Landmark name; choose from autocomplete")
@app_commands.autocomplete(landmark=landmark_autocomplete)
async def guess_command(interaction: discord.Interaction, landmark: str):
    destination = bot.by_name.get(normalize(landmark))
    if destination is None:
        await interaction.response.send_message("Unknown landmark. Use `/search` or select an autocomplete suggestion.", ephemeral=True)
        return
    success_landmark = None
    async with bot.lock:
        day = bot.ensure_today()
        p = bot.player(day, interaction.user)
        if p["solved"]:
            msg = "✅ You already found Nova today! Come back after the GMT+8 reset."
        else:
            previous = bot.by_name[normalize(p["current"])]
            travelled = distance(previous, destination)
            p["distance"] += travelled
            p["guesses"] += 1
            p["current"] = destination["name"]
            p["visited"].append(destination["name"])
            if normalize(destination["name"]) == normalize(day["target"]):
                p["solved"] = True
                success_landmark = destination
                msg = (f"🎉 **Astra found Nova at {destination['name']}!**\n"
                       f"Guesses: **{p['guesses']}**\nTotal travel: **{p['distance']:.1f} blocks**")
            else:
                target = bot.by_name[normalize(day["target"])]
                parts = [f"✨ Astra traveled to **{destination['name']}**!",
                         f"🛣️ +{travelled:.1f} blocks · Total **{p['distance']:.1f} blocks** · Guesses **{p['guesses']}**",
                         f"🔤 {count_hint(destination['name'], target['name'])}"]
                same_count = len(letters(destination["name"])) == len(letters(target["name"]))
                hint_type = random.choice(["pattern", "direction"]) if same_count else "direction"
                if hint_type == "pattern":
                    parts.append(f"🧩 `{pattern(destination['name'], target['name'])}`")
                else:
                    parts.append(f"🧭 Nova is to the **{direction(destination, target)}**.")
                msg = "\n".join(parts)
        save_json(PLAYER_FILE, bot.state)
    if success_landmark is not None:
        embed = discord.Embed(
            title=f"🎉 Astra found Nova at {success_landmark['name']}!",
            description=(
                f"📍 **Region:** {success_landmark.get('region', 'Unknown')}\n"
                f"🎯 **Guesses:** {p['guesses']}\n"
                f"🛣️ **Total travel:** {p['distance']:.1f} blocks"
            ),
            color=discord.Color.purple(),
        )
        # Image filename comes directly from landmark_data.json.
        image_name = success_landmark.get("image")
        if isinstance(image_name, str) and image_name and Path(image_name).name == image_name:
            image_path = IMAGE_DIR / image_name
            if image_path.is_file():
                picture = discord.File(image_path, filename=image_name)
                embed.set_image(url=f"attachment://{image_name}")
                await interaction.response.send_message(embed=embed, file=picture, ephemeral=True)
                return
        embed.set_footer(text="Landmark picture unavailable.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="status", description="View your current daily progress.")
async def status_command(interaction: discord.Interaction):
    async with bot.lock:
        day = bot.ensure_today()
        p = bot.player(day, interaction.user)
        msg = (f"📍 Current: **{p['current']}**\n🎯 Guesses: **{p['guesses']}**\n"
               f"🛣️ Total traveled: **{p['distance']:.1f} blocks**\n"
               f"{'✅ Nova found!' if p['solved'] else '🔍 Still searching for Nova.'}")
        save_json(PLAYER_FILE, bot.state)
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="scoreboard", description="View today's or yesterday's scoreboard.")
@app_commands.choices(day=[app_commands.Choice(name="Today", value="today"),
                          app_commands.Choice(name="Yesterday", value="yesterday")])
async def scoreboard_command(interaction: discord.Interaction, day: app_commands.Choice[str] | None = None):
    requested = day.value if day else "today"
    async with bot.lock:
        current = datetime.now(GAME_TZ).date()
        data = bot.ensure_today()
        day_key = (current - timedelta(days=1)).isoformat() if requested == "yesterday" else current.isoformat()
        selected = bot.state["days"].get(day_key)
        if selected is None:
            rows = []
        else:
            rows = list(selected["players"].items())
        finished = sorted([(uid,p) for uid,p in rows if p.get("solved")], key=lambda x:(x[1]["guesses"], x[1]["distance"], x[0]))
        unfinished = sorted([(uid,p) for uid,p in rows if not p.get("solved")], key=lambda x:(x[1]["guesses"], x[1]["distance"], x[0]))
    embed = discord.Embed(title=f"🏆 Astra scoreboard — {day_key} (GMT+8)", color=discord.Color.gold())
    if not rows:
        embed.description = "No players recorded for this day."
    else:
        lines = []
        for index, (uid, p) in enumerate(finished, 1):
            lines.append(f"**{index}.** <@{uid}> — {p['guesses']} guesses · {p['distance']:.1f} blocks ✅")
        if unfinished:
            lines.append("\n**Still searching (unranked)**")
            for uid,p in unfinished:
                lines.append(f"• <@{uid}> — {p['guesses']} guesses · {p['distance']:.1f} blocks")
        # Embed description limit is 4096 characters.
        embed.description = "\n".join(lines)[:4000]
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")


if __name__ == "__main__":
    token = config("DISCORD_TOKEN", default="")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(token)
