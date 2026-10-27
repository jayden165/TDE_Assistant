"""
Minimal Minecraft RCON client (the Source RCON protocol), no extra dependencies.

    from rcon import run, RconError
    replies = await run(["wl Steve"])          # one connection, one reply per command

Settings come from .env (see config.py): RCON_HOST, RCON_PORT, RCON_PASSWORD. The password is never
logged. Commands are sent one connection at a time (a lock), each with a timeout, so a dead server
fails fast with an RconError instead of hanging the bot.
"""
import asyncio
import re
import struct

import config

TIMEOUT = 8  # seconds, for connecting and for every read
_AUTH, _COMMAND, _AUTH_RESPONSE = 3, 2, 2
_COLOR_CODES = re.compile("§.")  # Minecraft colour codes (§a, §l ...) in replies
_lock = asyncio.Lock()


class RconError(Exception):
    """Couldn't talk to the server: not configured, unreachable, wrong password, or a broken connection."""


def configured() -> bool:
    return bool(config.RCON_HOST and config.RCON_PASSWORD)


def clean(text: str) -> str:
    return _COLOR_CODES.sub("", text).strip()


def _pack(request_id: int, packet_type: int, body: str) -> bytes:
    payload = body.encode("utf-8") + b"\x00\x00"
    return struct.pack("<iii", len(payload) + 8, request_id, packet_type) + payload


async def _read(reader: asyncio.StreamReader) -> tuple[int, int, str]:
    (size,) = struct.unpack("<i", await asyncio.wait_for(reader.readexactly(4), TIMEOUT))
    data = await asyncio.wait_for(reader.readexactly(size), TIMEOUT)
    request_id, packet_type = struct.unpack("<ii", data[:8])
    return request_id, packet_type, data[8:-2].decode("utf-8", "replace")


async def run(commands: list[str]) -> list[str]:
    """Runs the commands in order over one connection and returns the server's reply to each."""
    if not configured():
        raise RconError("RCON isn't configured (set RCON_HOST and RCON_PASSWORD in .env)")
    async with _lock:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.RCON_HOST, config.RCON_PORT), TIMEOUT)
        except (OSError, asyncio.TimeoutError) as e:
            raise RconError(f"couldn't connect to {config.RCON_HOST}:{config.RCON_PORT} ({type(e).__name__})") from e
        try:
            writer.write(_pack(1, _AUTH, config.RCON_PASSWORD))
            await writer.drain()
            while True:  # some servers send an empty packet before the real auth reply
                request_id, packet_type, _ = await _read(reader)
                if packet_type == _AUTH_RESPONSE:
                    if request_id == -1:
                        raise RconError("the RCON password was rejected")
                    break
            replies = []
            for i, command in enumerate(commands, start=2):
                writer.write(_pack(i, _COMMAND, command))
                await writer.drain()
                while True:
                    request_id, _packet_type, text = await _read(reader)
                    if request_id == i:
                        replies.append(clean(text))
                        break
            return replies
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, OSError, struct.error) as e:
            raise RconError(f"the connection to the server broke ({type(e).__name__})") from e
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
