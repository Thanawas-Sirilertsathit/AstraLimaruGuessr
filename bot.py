"""Astra Magical Girl: daily landmark hunt Discord bot.
Requirements: discord.py>=2.4, python-decouple>=3.8
Run: python bot.py

"""

from __future__ import annotations
import asyncio
import json
import logging
import math
import io
from PIL import Image, ImageDraw, ImageFont
import os
import random
import re
import tempfile
import time
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
MAP_FILE = BASE / "map_image.png"
MAP_X_LEFT, MAP_X_RIGHT = -9317, -1955
MAP_Z_TOP, MAP_Z_BOTTOM = 4033, 9450
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
    """Atomically save JSON, tolerating brief Windows file locks.

    A unique temporary file prevents collisions with other in-process saves.
    Windows may briefly block os.replace while an antivirus or editor scans
    the destination. Persistent permission problems are still reported.
    """
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False
        ) as f:
            temporary = Path(f.name)
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()

        for attempt in range(5):
            try:
                os.replace(temporary, path)
                temporary = None
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
            raise ValueError(
                "player_data.json must contain an object with a 'days' object")
        self.lock = asyncio.Lock()
        # Practice sessions are private, unranked, and never written to player_data.json.
        self.practice = {}

    async def setup_hook(self):
        # Enable RESET_COMMANDS=true in .env for ONE restart to remove stale
        # global slash commands and register the current set from scratch.
        if config("RESET_COMMANDS", default=False, cast=bool):
            registered = list(self.tree.get_commands(guild=None))
            self.tree.clear_commands(guild=None)
            # delete previously registered global commands
            await self.tree.sync(guild=None)
            for command in registered:
                self.tree.add_command(command)
            log.info(
                "Cleared old global commands; restoring %d commands.", len(registered))
        synced = await self.tree.sync(guild=None)
        log.info("Synced %d global slash commands: %s", len(synced),
                 ", ".join(command.name for command in synced))
        self.date_check.start()

    async def close(self):
        self.date_check.cancel()
        await super().close()

    def today(self) -> str:
        return datetime.now(GAME_TZ).date().isoformat()

    def ensure_today(self) -> dict:
        """Create/clean daily records, saving only when the data changes."""
        current_date = datetime.now(GAME_TZ).date()
        today = current_date.isoformat()
        yesterday = (current_date - timedelta(days=1)).isoformat()
        days = self.state["days"]
        changed = False
        for date in list(days):
            if date not in {today, yesterday}:
                del days[date]
                changed = True
        if today not in days:
            start, target = random.sample(self.landmarks, 2)
            days[today] = {"start": start["name"],
                           "target": target["name"], "players": {}}
            changed = True
        if changed:
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


def render_progress_map(start_name: str, visited: list[str]) -> io.BytesIO:
    """Overlay a player's journey; never draw Nova's hidden destination."""
    with Image.open(MAP_FILE) as source:
        canvas = source.convert("RGB")
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(12, width // 65))
    except OSError:
        font = ImageFont.load_default()

    def pixel(name: str) -> tuple[int, int]:
        landmark = bot.by_name[normalize(name)]
        x, z = landmark["coords"]["x"], landmark["coords"]["z"]
        px = round((x - MAP_X_LEFT) / (MAP_X_RIGHT - MAP_X_LEFT) * (width - 1))
        py = round((z - MAP_Z_TOP) / (MAP_Z_BOTTOM - MAP_Z_TOP) * (height - 1))
        return px, py

    route = [start_name, *visited]
    positions = [pixel(name) for name in route]
    # Draw only segments whose endpoints fall within the supplied map.
    def inside(point):
        return 0 <= point[0] < width and 0 <= point[1] < height

    for a, b in zip(positions, positions[1:]):
        if inside(a) and inside(b):
            draw.line((a, b), fill=(20, 20, 20), width=8)
            draw.line((a, b), fill=(255, 210, 60), width=5)
    for index, point in enumerate(positions):
        if not inside(point):
            continue
        x, y = point
        radius = 13 if index == len(positions) - 1 else 10
        fill = (48, 210, 105) if index == 0 else (60, 155, 255)
        if index == len(positions) - 1 and index > 0:
            fill = (255, 105, 65)
        draw.ellipse((x-radius-2, y-radius-2, x+radius+2, y+radius+2),
                     fill=(10, 20, 35))
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=fill)
        label = "S" if index == 0 else str(index)
        draw.text((x, y), label, font=font, anchor="mm", fill="white",
                  stroke_width=1, stroke_fill="black")
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)
    return output


async def send_progress_map(interaction: discord.Interaction, mode: str) -> None:
    """Send a private route map after a daily or practice action."""
    if not MAP_FILE.is_file():
        log.warning("Missing map image: %s", MAP_FILE)
        return
    async with bot.lock:
        if mode == "practice":
            session = bot.practice.get(str(interaction.user.id))
            if session is None:
                return
            start = session["start"]
            visited = list(session["visited"])
        else:
            day = bot.ensure_today()
            session = day["players"].get(str(interaction.user.id))
            if session is None:
                return
            start = day["start"]
            visited = list(session["visited"])
    try:
        image = render_progress_map(start, visited)
        embed = discord.Embed(title="🗺️ Astra's Journey Map",
                              description=f"Start: 🟢 · Guesses: 🔵 · Latest: 🟠 · {len(visited)} guesses",
                              color=discord.Color.purple())
        embed.set_image(url="attachment://journey_map.png")
        await interaction.followup.send(embed=embed,
                                        file=discord.File(image, filename="journey_map.png"),
                                        ephemeral=True)
    except (OSError, ValueError, KeyError, discord.HTTPException):
        log.exception("Unable to render/send %s journey map", mode)



def landmark_page(items: list, index: int, title: str) -> discord.Embed:
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    index = max(0, min(index, pages - 1))
    embed = discord.Embed(title=title, color=discord.Color.purple())
    chunk = items[index * PAGE_SIZE: (index + 1) * PAGE_SIZE]
    if not chunk:
        embed.description = "No matching landmarks found."
    for entry in chunk:
        c = entry["coords"]
        embed.add_field(
            name=entry["name"], value=f"{entry.get('region', 'Unknown region')}\nX: {c['x']} · Z: {c['z']}", inline=False)
    embed.set_footer(text=f"Page {index + 1}/{pages} · {len(items)} landmarks" + (
        " · React ⬅️ / ➡️ to turn pages" if pages > 1 else ""))
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
    results = [p for p in bot.landmarks if term in normalize(
        p["name"]) or term in normalize(p.get("region", ""))]
    await paginated(interaction, results, f"🔎 Search: {text[:80]}")


@bot.tree.command(name="daily", description="Start or resume today's hunt for Nova.")
async def daily_command(interaction: discord.Interaction):
    async with bot.lock:
        day = bot.ensure_today()
        p = bot.player(day, interaction.user)
        start = bot.by_name[normalize(day["start"])]
        bot.state["days"][bot.today()] = day
        save_json(PLAYER_FILE, bot.state)
        msg = (f"✨ Astra starts at **{start['name']}** ({start.get('region', 'Unknown')}).\n"
               f"🔤 {count_hint(start['name'], day['target'])}\n"
               f"📍 Current position: **{p['current']}**\n"
               f"🎯 Guesses: **{p['guesses']}** · Travel: **{p['distance']:.1f} blocks**\n"
               + ("✅ You already found Nova today!" if p["solved"] else "Use `/guess` to travel to a landmark."))
    await interaction.response.send_message(msg, ephemeral=True)
    await send_progress_map(interaction, "daily")


async def landmark_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    term = normalize(current)
    candidates = [p["name"]
                  for p in bot.landmarks if term in normalize(p["name"])]
    return [app_commands.Choice(name=name[:100], value=name[:100]) for name in candidates[:25]]


@bot.tree.command(name="guess", description="Guess where Nova is hiding and travel there.")
@app_commands.describe(landmark="Landmark name; choose from autocomplete")
@app_commands.autocomplete(landmark=landmark_autocomplete)
async def guess_command(interaction: discord.Interaction, landmark: str):
    # Discord needs the interaction acknowledged quickly, before image upload or disk IO.
    await interaction.response.defer(ephemeral=True, thinking=True)
    destination = bot.by_name.get(normalize(landmark))
    if destination is None:
        await interaction.followup.send(
            "Unknown landmark. Use `/search` or select an autocomplete suggestion.",
            ephemeral=True,
        )
        return
    success_landmark = None
    guesses = 0
    distance_traveled = 0.0
    game_date = ""
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
                guesses = p["guesses"]
                distance_traveled = p["distance"]
                game_date = bot.today()
            else:
                target = bot.by_name[normalize(day["target"])]
                parts = [
                    f"✨ Astra traveled to **{destination['name']}**!",
                    f"🛣️ +{travelled:.1f} blocks · Total **{p['distance']:.1f} blocks** · Guesses **{p['guesses']}**",
                    f"🔤 {count_hint(destination['name'], target['name'])}",
                ]
                same_count = len(letters(destination["name"])) == len(
                    letters(target["name"]))
                hint_type = random.choice(
                    ["pattern", "direction"]) if same_count else "direction"
                if hint_type == "pattern":
                    parts.append(
                        f"🧩 `{pattern(destination['name'], target['name'])}`")
                else:
                    parts.append(
                        f"🧭 Nova is to the **{direction(destination, target)}**.")
                msg = "\n".join(parts)
        save_json(PLAYER_FILE, bot.state)
    if success_landmark is None:
        await interaction.followup.send(msg, ephemeral=True)
        await send_progress_map(interaction, "daily")
        return
    # PRIVATE victory result: reveal target and image to the player only.
    private_embed = discord.Embed(
        title=f"🎉 Astra found Nova at {success_landmark['name']}!",
        description=(
            f"📍 **Region:** {success_landmark.get('region', 'Unknown')}\n"
            f"🎯 **Guesses:** {guesses}\n"
            f"🛣️ **Total travel:** {distance_traveled:.1f} blocks"
        ),
        color=discord.Color.purple(),
    )
    picture = attached_landmark_image(success_landmark, private_embed)
    if picture is None:
        private_embed.set_footer(
            text="Landmark picture unavailable in images/.")
    # Deliver private victory first. Even with no file, the win is still recorded.
    try:
        if picture is not None:
            await interaction.followup.send(embed=private_embed, file=picture, ephemeral=True)
        else:
            await interaction.followup.send(embed=private_embed, ephemeral=True)
    except (discord.HTTPException, OSError):
        log.exception(
            "Could not deliver private victory image; retrying without image")
        private_embed.set_image(url=None)
        await interaction.followup.send(embed=private_embed, ephemeral=True)
    await send_progress_map(interaction, "daily")
    # PUBLIC: keep the destination and picture completely secret.
    public_embed = discord.Embed(
        title="🌟 Astra Magical Girl — Daily Result",
        description=f"{interaction.user.mention} successfully completed today's mystery!",
        color=discord.Color.gold(),
    )
    public_embed.add_field(name="🎯 Number of guesses",
                           value=str(guesses), inline=True)
    public_embed.add_field(name="🛣️ Distance traveled",
                           value=f"{distance_traveled:.1f} blocks", inline=True)
    public_embed.set_footer(
        text=f"{game_date} (GMT+8) • Nova's location remains a secret")
    # Explicitly post to the channel, avoiding ambiguity about whether the
    # deferred ephemeral interaction could affect followup visibility.
    if interaction.channel is None:
        log.error(
            "Unable to publish the public result: no channel for interaction %s", interaction.id)
        return
    try:
        await interaction.channel.send(embed=public_embed)
    except discord.HTTPException:
        log.exception("Unable to post public result for player %s",
                      interaction.user.id)


# Practice Mode uses independent private sessions and never updates daily scores.
# /practice starts a new round, /practiceguess guesses, /practicestatus shows progress.


def practice_hint(current: dict, target: dict) -> str:
    parts = [f"🔤 {count_hint(current['name'], target['name'])}"]
    same_count = len(letters(current["name"])) == len(letters(target["name"]))
    hint_type = random.choice(["pattern", "direction"]) if same_count else "direction"
    if hint_type == "pattern":
        parts.append(f"🧩 `{pattern(current['name'], target['name'])}`")
    else:
        parts.append(f"🧭 Nova is to the **{direction(current, target)}**.")
    return "\n".join(parts)


async def send_practice_image(interaction: discord.Interaction, embed: discord.Embed,
                              landmark: dict) -> None:
    """Reply privately with a landmark image, falling back if unavailable."""
    picture = attached_landmark_image(landmark, embed)
    if picture is None:
        embed.set_footer(text="Landmark picture unavailable in images/.")
    try:
        if picture:
            await interaction.followup.send(embed=embed, file=picture, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)
    except (discord.HTTPException, OSError):
        log.exception("Could not deliver practice landmark image")
        embed.set_image(url=None)
        await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="practice", description="Start a new private, unranked practice hunt.")
async def practice_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.lock:
        uid = str(interaction.user.id)
        old = bot.practice.get(uid)
        start, target = random.sample(bot.landmarks, 2)
        # Avoid repeating the previous origin/destination pair when possible.
        if old and len(bot.landmarks) > 2:
            for _ in range(20):
                if (start["name"], target["name"]) != (old["start"], old["target"]):
                    break
                start, target = random.sample(bot.landmarks, 2)
        bot.practice[uid] = {
            "start": start["name"], "target": target["name"],
            "current": start["name"], "guesses": 0, "distance": 0.0,
            "solved": False, "visited": [],
        }
        hint = count_hint(start["name"], target["name"])
    embed = discord.Embed(
        title="✨ Astra's Practice Hunt — New Round",
        description=(
            f"📍 Starting location: **{start['name']}** ({start.get('region', 'Unknown')})\n"
            f"🔤 {hint}\n"
            "🎯 Guesses: **0** · Travel: **0.0 blocks**\n"
            "Use `/practiceguess` to travel to a landmark.\n"
            "Use `/practice` again whenever you want a fresh round."
        ),
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Private practice • Unranked • Never shared")
    await send_practice_image(interaction, embed, start)
    await send_progress_map(interaction, "practice")


@bot.tree.command(name="practiceguess", description="Guess a landmark in your private practice hunt.")
@app_commands.describe(landmark="Landmark name; choose from autocomplete")
@app_commands.autocomplete(landmark=landmark_autocomplete)
async def practiceguess_command(interaction: discord.Interaction, landmark: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    destination = bot.by_name.get(normalize(landmark))
    if destination is None:
        await interaction.followup.send(
            "Unknown landmark. Use `/search` or autocomplete.", ephemeral=True)
        return
    async with bot.lock:
        p = bot.practice.get(str(interaction.user.id))
        if p is None:
            message = "Start a private round with `/practice` first."
            success = False
        elif p["solved"]:
            message = "✅ You found Nova! Use `/practice` to start another round."
            success = False
        else:
            previous = bot.by_name[normalize(p["current"])]
            travelled = distance(previous, destination)
            p["distance"] += travelled
            p["guesses"] += 1
            p["current"] = destination["name"]
            p["visited"].append(destination["name"])
            target = bot.by_name[normalize(p["target"])]
            if normalize(destination["name"]) == normalize(target["name"]):
                p["solved"] = True
                success = True
                guesses = p["guesses"]
                traveled_total = p["distance"]
            else:
                success = False
                message = (
                    f"✨ Astra traveled to **{destination['name']}**!\n"
                    f"🛣️ +{travelled:.1f} blocks · Total **{p['distance']:.1f} blocks** "
                    f"· Guesses **{p['guesses']}**\n"
                    f"{practice_hint(destination, target)}"
                )
    if not success:
        await interaction.followup.send(message, ephemeral=True)
        await send_progress_map(interaction, "practice")
        return
    embed = discord.Embed(
        title=f"🎉 Practice Complete — Nova was at {destination['name']}!",
        description=(
            f"📍 **Region:** {destination.get('region', 'Unknown')}\n"
            f"🎯 **Guesses:** {guesses}\n"
            f"🛣️ **Total travel:** {traveled_total:.1f} blocks\n\n"
            "Use `/practice` to play again with new locations."
        ),
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Private practice • Unranked • Never shared")
    await send_practice_image(interaction, embed, destination)
    await send_progress_map(interaction, "practice")


@bot.tree.command(name="practicestatus", description="View your private practice progress and location image.")
async def practicestatus_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with bot.lock:
        p = bot.practice.get(str(interaction.user.id))
        if p is None:
            details = None
        else:
            details = (p["current"], p["guesses"], p["distance"], p["solved"])
    if details is None:
        await interaction.followup.send("Start a round with `/practice` first.", ephemeral=True)
        return
    current, guesses, traveled_total, solved = details
    landmark = bot.by_name[normalize(current)]
    embed = discord.Embed(
        title="✨ Astra's Practice Journey",
        description=(
            f"📍 **Current location:** {current}\n"
            f"🎯 **Guesses:** {guesses}\n"
            f"🛣️ **Total travel:** {traveled_total:.1f} blocks\n"
            f"{'✅ Nova found! Use `/practice` for another round.' if solved else '🔍 Still searching for Nova.'}"
        ),
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Private practice • Unranked • Never shared")
    await send_practice_image(interaction, embed, landmark)
    await send_progress_map(interaction, "practice")


@bot.tree.command(name="checkimages", description="Check whether landmark pictures are installed.")
async def checkimages_command(interaction: discord.Interaction):
    missing = []
    installed = 0
    for landmark in bot.landmarks:
        filename = landmark.get("image")
        if isinstance(filename, str) and filename and Path(filename).name == filename and (IMAGE_DIR / filename).is_file():
            installed += 1
        else:
            missing.append(
                f"{landmark['name']}: {filename or '(filename missing)'}")
    description = (
        f"📷 Found **{installed}/{len(bot.landmarks)}** landmark pictures in `images/`.\n")
    if missing:
        description += "\n**Missing pictures:**\n" + \
            "\n".join(f"• {item}" for item in missing[:20])
        if len(missing) > 20:
            description += f"\n... and {len(missing)-20} more"
    else:
        description += "✅ All landmark pictures are installed."
    await interaction.response.send_message(description[:1900], ephemeral=True)


def attached_landmark_image(landmark: dict, embed: discord.Embed) -> discord.File | None:
    """Attach an image from images/<JSON image filename> when available."""
    filename = landmark.get("image")
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        return None
    path = IMAGE_DIR / filename
    if not path.is_file():
        log.warning("Landmark image is missing: %s", path)
        return None
    embed.set_image(url=f"attachment://{filename}")
    return discord.File(path, filename=filename)


@bot.tree.command(name="status", description="View your progress and current landmark picture.")
async def status_command(interaction: discord.Interaction):
    async with bot.lock:
        day = bot.ensure_today()
        p = bot.player(day, interaction.user)
        current_name = p["current"]
        guesses = p["guesses"]
        distance_traveled = p["distance"]
        solved = p["solved"]
        save_json(PLAYER_FILE, bot.state)
    current_landmark = bot.by_name[normalize(current_name)]
    embed = discord.Embed(
        title="✨ Astra's Daily Journey",
        description=(
            f"📍 **Current location:** {current_name}\n"
            f"🎯 **Guesses:** {guesses}\n"
            f"🛣️ **Total travel:** {distance_traveled:.1f} blocks\n"
            f"{'✅ Nova found!' if solved else '🔍 Still searching for Nova.'}"
        ),
        color=discord.Color.purple(),
    )
    picture = attached_landmark_image(current_landmark, embed)
    if picture:
        await interaction.response.send_message(embed=embed, file=picture, ephemeral=True)
    else:
        embed.set_footer(
            text="Current landmark picture unavailable in images/.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    await send_progress_map(interaction, "daily")


@bot.tree.command(name="share", description="Share your completed result without revealing Nova's location.")
async def share_command(interaction: discord.Interaction):
    async with bot.lock:
        day = bot.ensure_today()
        player = day["players"].get(str(interaction.user.id))
        if player is None or not player.get("solved"):
            ready = False
        else:
            ready = True
            guesses = player["guesses"]
            distance_traveled = player["distance"]
            game_date = bot.today()
    if not ready:
        await interaction.response.send_message(
            "🔍 Find Nova first using `/guess`, then you can share your result!",
            ephemeral=True,
        )
        return
    embed = discord.Embed(
        title="🌟 Astra Magical Girl — Daily Result",
        description=f"{interaction.user.mention} successfully completed today's mystery!",
        color=discord.Color.gold(),
    )
    embed.add_field(name="🎯 Number of guesses",
                    value=str(guesses), inline=True)
    embed.add_field(name="🛣️ Distance traveled",
                    value=f"{distance_traveled:.1f} blocks", inline=True)
    embed.set_footer(
        text=f"{game_date} (GMT+8) • Nova's location remains a secret")
    # Explicitly do not include destination name, region, coordinates or picture.
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="scoreboard", description="View today's or yesterday's scoreboard.")
@app_commands.choices(day=[app_commands.Choice(name="Today", value="today"),
                           app_commands.Choice(name="Yesterday", value="yesterday")])
async def scoreboard_command(interaction: discord.Interaction, day: app_commands.Choice[str] | None = None):
    requested = day.value if day else "today"
    async with bot.lock:
        current = datetime.now(GAME_TZ).date()
        data = bot.ensure_today()
        day_key = (current - timedelta(days=1)
                   ).isoformat() if requested == "yesterday" else current.isoformat()
        selected = bot.state["days"].get(day_key)
        if selected is None:
            rows = []
        else:
            rows = list(selected["players"].items())
        finished = sorted([(uid, p) for uid, p in rows if p.get(
            "solved")], key=lambda x: (x[1]["guesses"], x[1]["distance"], x[0]))
        unfinished = sorted([(uid, p) for uid, p in rows if not p.get(
            "solved")], key=lambda x: (x[1]["guesses"], x[1]["distance"], x[0]))
    embed = discord.Embed(
        title=f"🏆 Astra scoreboard — {day_key} (GMT+8)", color=discord.Color.gold())
    if not rows:
        embed.description = "No players recorded for this day."
    else:
        lines = []
        for index, (uid, p) in enumerate(finished, 1):
            lines.append(
                f"**{index}.** <@{uid}> — {p['guesses']} guesses · {p['distance']:.1f} blocks ✅")
        if unfinished:
            lines.append("\n**Still searching (unranked)**")
            for uid, p in unfinished:
                lines.append(
                    f"• <@{uid}> — {p['guesses']} guesses · {p['distance']:.1f} blocks")
        # Embed description limit is 4096 characters.
        embed.description = "\n".join(lines)[:4000]
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user,
             bot.user.id if bot.user else "?")


if __name__ == "__main__":
    token = config("DISCORD_TOKEN", default="")
    if not token:
        raise SystemExit("Missing DISCORD_TOKEN in .env")
    bot.run(token)
