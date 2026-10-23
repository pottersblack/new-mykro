"""
MYKRO Discord Integration
Listens for mentions and routes messages to the local MYKRO Gateway.

Usage:
1. pip install discord.py
2. Set up a bot at https://discord.com/developers/applications
3. Enable "Message Content Intent" in the bot settings.
4. Replace YOUR_DISCORD_BOT_TOKEN_HERE or set the DISCORD_BOT_TOKEN env variable.
5. Run: python discord_bot.py
"""

# Tests and integrations in this module may catch broad exceptions from third-party
# libraries (discord.py / aiohttp). Keep a focused pylint suppression with a note.
# pylint: disable=broad-exception-caught

import os
import logging
from typing import Any

try:
    import discord  # type: ignore[import-not-found]
except ImportError:
    discord = None  # type: ignore[assignment]

try:
    import aiohttp  # type: ignore[import-not-found]
except ImportError:
    aiohttp = None  # type: ignore[assignment]

from system_prompts import get_system_prompt
from hub import get_hub, get_circuit_registry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("discord_mykro")


def _get_gateway_url() -> str:
    """Read gateway URL from config.json; fall back to default."""
    import json
    from pathlib import Path
    cfg_path = Path(__file__).resolve().parent / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        gw = cfg.get("gateway", {})
        host = gw.get("host", "127.0.0.1")
        port = int(gw.get("port", 8090))
        return f"http://{host}:{port}"
    except Exception:
        return "http://127.0.0.1:8090"


def _register_discord_wires():
    """Register discord_bot → gateway wire and enforce circuit gate."""
    reg = get_circuit_registry()
    reg.require_circuit("CKT-DISC-001")
    reg.activate_circuit("CKT-DISC-001")

    hub = get_hub()
    gateway_url = _get_gateway_url()
    hub.register_wire(
        name="discord_bot→gateway",
        source="discord_bot",
        target="gateway",
        transport="http",
        endpoint=f"{gateway_url}/v1/chat/completions",
    )
    hub.log_event("discord_bot_started", "discord_bot→gateway", {"gateway_url": gateway_url})


client: Any = None
if discord is not None:
    # Setup Intents
    intents = discord.Intents.default()
    # Crucial: Must be enabled in the Discord Developer Portal for the bot!
    intents.message_content = True
    client = discord.Client(intents=intents)

# Allow environment variable or hardcoded token
TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN_HERE")
GATEWAY_URL = _get_gateway_url() + "/v1/chat/completions"

if client is not None:
    @client.event
    async def on_ready():
        log.info("MYKRO Discord Bot connected as %s", client.user)
        log.info("Listening for mentions (@botname) in accessible channels...")

    @client.event
    async def on_message(message):
        # Ignore messages from the bot itself
        if message.author == client.user:
            return

        # Check if the bot is mentioned
        if client.user.mentioned_in(message):
            # Create a clean message by removing the bot's mention string
            mention_str = f"@{client.user.name}"
            clean_msg = message.clean_content.replace(mention_str, "").strip()

            # In case the bot was mentioned with no ensuing text
            if not clean_msg:
                return

            username = message.author.display_name

            log.info("Received query from %s: %s", username, clean_msg)

            # Show Discord's "bot is typing..." indicator
            async with message.channel.typing():
                try:
                    if aiohttp is None:
                        await message.reply(
                            "Discord HTTP dependency missing (`aiohttp`). Install requirements first."
                        )
                        return

                    # Wrap the user in an instruction specifying they are communicating from Discord
                    sys_prompt = get_system_prompt("household")
                    sys_prompt += (
                        f"\n\nYou are answering a live Discord message from the user '{username}'. "
                        "Keep your formatting Discord-friendly (`code`, **bold**, *italics*), use modern "
                        "internet parlance naturally, and keep responses relatively concise unless asked for detail."
                    )

                    payload = {
                        "messages": [
                            {"role": "system", "content": sys_prompt},
                            {"role": "user", "content": clean_msg},
                        ],
                        "model": "mykro-auto",  # Let the Gateway pick the fastest/best available
                        "stream": False,
                        "temperature": 0.7,
                        "max_tokens": 2048,
                    }

                    # Use aiohttp to call MYKRO Gateway
                    async with aiohttp.ClientSession() as session:
                        async with session.post(GATEWAY_URL, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as response:
                            response.raise_for_status()
                            data = await response.json()

                            choices = data.get("choices") if isinstance(data, dict) else None
                            if isinstance(choices, list) and choices:
                                reply = choices[0].get("message", {}).get("content", "")
                                if not isinstance(reply, str) or not reply.strip():
                                    await message.reply("MYKRO returned an empty response.")
                                    log.error("Unexpected Gateway payload (missing content): %s", data)
                                    return

                                # Discord caps messages at 2000 characters; split if necessary
                                if len(reply) > 2000:
                                    chunks = [reply[i:i+1950] for i in range(0, len(reply), 1950)]
                                    for chunk in chunks:
                                        await message.reply(chunk)
                                else:
                                    await message.reply(reply)
                                log.info("Successfully responded to Discord query.")
                            else:
                                await message.reply(
                                    "MYKRO Gateway returned an unexpected format. No response generated."
                                )
                                log.error("Unexpected Gateway payload: %s", data)

                except Exception as exc:  # pylint: disable=broad-exception-caught
                    if aiohttp is not None and isinstance(exc, aiohttp.ClientError):
                        log.error("Error reaching MYKRO Gateway: %s", exc)
                        await message.reply(
                            "Sorry, MYKRO Gateway is unreachable right now. "
                            f"Is `start-all.ps1` running? ({exc})"
                        )
                    elif isinstance(exc, (ValueError, KeyError, TypeError)):
                        log.error("Gateway payload parse error: %s", exc)
                        await message.reply("Sorry, MYKRO returned an invalid payload.")
                    elif isinstance(exc, RuntimeError):
                        log.error("Discord bot runtime error: %s", exc)
                        await message.reply(f"Sorry, an internal bot runtime error occurred: {exc}")
                    else:
                        log.exception("Unexpected Discord bot exception: %s", exc)
                        await message.reply("Sorry, an internal bot error occurred.")

if __name__ == "__main__":
    if discord is None:
        log.error("Missing dependency `discord.py`. Install with requirements and retry.")
    elif aiohttp is None:
        log.error("Missing dependency `aiohttp`. Install with requirements and retry.")
    elif TOKEN == "YOUR_DISCORD_BOT_TOKEN_HERE":
        log.error("Please enter your Discord Bot Token where `YOUR_DISCORD_BOT_TOKEN_HERE` is written, or export `DISCORD_BOT_TOKEN`.")
    else:
        _register_discord_wires()
        log.info("Starting Discord connection...")
        if client is not None:
            client.run(TOKEN)
