"""
Tiny YouTube helper for cogs/uploads.py - no API key needed.

    new_session()                              aiohttp session to pass to the functions below
    fetch_videos(session, channel_id)          latest videos AND Shorts (from the channel's public RSS feed)
    fetch_posts(session, channel_id, handle)   latest community posts (scraped from the public Posts tab)
    is_short(session, video_id)                True if a video is a Short

Every fetch_* returns a list of dicts, newest first:

    {"id": unique key, "kind": "video" | "short" | "post", "video_id": str (videos only),
     "title": str, "text": str, "url": str, "image": str | None, "published": str}

Community posts have no official API, so fetch_posts is best-effort: if YouTube changes its page it
raises (the cog shows the error in /upload status) instead of posting garbage.
"""
import json
import re
import xml.etree.ElementTree as ET

import aiohttp

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
POSTS_URL = "https://www.youtube.com/@{handle}/posts"
SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
POST_URL = "https://www.youtube.com/post/{post_id}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    # skips YouTube's cookie-consent page, which servers in the EU otherwise get
    "Cookie": "CONSENT=YES+1; SOCS=CAI",
}

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def new_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20))


def _clean_handle(handle: str) -> str:
    return (handle or "").strip().lstrip("@")


# ---------------- videos + Shorts (RSS) ----------------

async def fetch_videos(session: aiohttp.ClientSession, channel_id: str) -> list[dict]:
    if not channel_id:
        raise RuntimeError("YOUTUBE_CHANNEL_ID isn't set")
    async with session.get(FEED_URL.format(channel_id=channel_id)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"YouTube feed returned HTTP {resp.status}")
        body = await resp.read()
    return parse_feed(body)


def parse_feed(body: bytes) -> list[dict]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        raise RuntimeError(f"couldn't read YouTube feed: {e}") from e
    items = []
    for entry in root.findall("atom:entry", NS):
        video_id = entry.findtext("yt:videoId", default="", namespaces=NS)
        if not video_id:
            continue
        thumb = entry.find("media:group/media:thumbnail", NS)
        items.append({
            "id": video_id,
            "kind": "video",  # the cog flips this to "short" using is_short()
            "video_id": video_id,
            "title": entry.findtext("atom:title", default="", namespaces=NS),
            "text": entry.findtext("media:group/media:description", default="", namespaces=NS),
            "url": WATCH_URL.format(video_id=video_id),
            "image": thumb.get("url") if thumb is not None else f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "published": entry.findtext("atom:published", default="", namespaces=NS),
        })
    return items  # the feed is already newest-first


async def is_short(session: aiohttp.ClientSession, video_id: str) -> bool:
    """/shorts/<id> serves the page for a Short (200) but redirects normal videos to /watch (3xx)."""
    try:
        async with session.get(SHORTS_URL.format(video_id=video_id), allow_redirects=False) as resp:
            return resp.status == 200
    except aiohttp.ClientError:
        return False  # can't tell -> treat as a normal video


# ---------------- community posts (Posts tab) ----------------

async def fetch_posts(session: aiohttp.ClientSession, channel_id: str, handle: str) -> list[dict]:
    handle = _clean_handle(handle)
    if not handle:
        raise RuntimeError("YOUTUBE_CHANNEL_HANDLE isn't set")
    async with session.get(POSTS_URL.format(handle=handle)) as resp:
        if resp.status != 200:
            raise RuntimeError(f"YouTube Posts tab returned HTTP {resp.status}")
        html = await resp.text()
    return parse_posts(html)


def _initial_data(html: str) -> dict:
    match = re.search(r"var ytInitialData\s*=\s*", html) or re.search(r'window\["ytInitialData"\]\s*=\s*', html)
    if not match:
        raise RuntimeError("couldn't find the Posts data on the page (YouTube may have changed it)")
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end():])
    except ValueError as e:
        raise RuntimeError("couldn't read the Posts data on the page") from e
    return data


def _find_all(node, keys: tuple[str, ...]):
    """Yield every value stored under one of `keys`, anywhere in the nested JSON."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in keys and isinstance(v, dict):
                yield v
            else:
                yield from _find_all(v, keys)
    elif isinstance(node, list):
        for v in node:
            yield from _find_all(v, keys)


def _runs_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return node["simpleText"]
    return "".join(run.get("text", "") for run in node.get("runs", []))


def _best_thumbnail(node) -> str | None:
    """Largest thumbnail url found anywhere under `node`."""
    best, best_w = None, -1
    for thumbs in _find_all(node, ("image", "thumbnail")):
        for t in thumbs.get("thumbnails", []):
            if t.get("url") and t.get("width", 0) > best_w:
                best, best_w = t["url"], t.get("width", 0)
    if best and best.startswith("//"):
        best = "https:" + best
    return best


def parse_posts(html: str) -> list[dict]:
    data = _initial_data(html)
    items, seen = [], set()
    for post in _find_all(data, ("backstagePostRenderer", "postRenderer")):
        post_id = post.get("postId")
        if not post_id or post_id in seen:
            continue
        seen.add(post_id)
        text = _runs_text(post.get("contentText")).strip()
        published = _runs_text(post.get("publishedTimeText")).strip()
        image = _best_thumbnail(post.get("backstageAttachment") or {})
        first_line = text.split("\n", 1)[0].strip()
        title = (first_line[:100] + "…") if len(first_line) > 100 else first_line
        items.append({
            "id": f"post:{post_id}",
            "kind": "post",
            "title": title or "New community post",
            "text": text,
            "url": POST_URL.format(post_id=post_id),
            "image": image,
            "published": published,
        })
    return items  # the Posts tab lists newest first (pinned posts may sit on top; harmless)
