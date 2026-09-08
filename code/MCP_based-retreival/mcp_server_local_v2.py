import os
import re
import time
import base64
import httpx
import yaml
from mcp.server.fastmcp import FastMCP

from dotenv import load_dotenv
load_dotenv(".env")

GITHUB_OWNER = os.getenv("GITHUB_OWNER")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
BLOG_PATH = os.getenv("BLOG_PATH")
PROJECTS_PATH = os.getenv("PROJECTS_PATH")
RESUME_PATH = os.getenv("RESUME_PATH")
GITHUB_API = os.getenv("GITHUB_API")
JSDELIVR_BASE = f"https://cdn.jsdelivr.net/gh/{GITHUB_OWNER}/{GITHUB_REPO}@{GITHUB_BRANCH}"

# Directory-listing cache: avoids re-fetching every file's content on every
# list/first/last/latest/oldest call within a session. Content rarely changes
# mid-conversation, so a short TTL is safe and collapses N redundant calls
# into 1 per directory per window.
CACHE_TTL_SECONDS = 300
_entry_cache: dict[str, tuple[float, list[dict]]] = {}

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)

mcp = FastMCP(
    "Abhinav Personal Website"
)


def _github_headers() -> dict:
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "abhinav-personal-website-mcp",
    }


async def github_get(path: str):
    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/contents/"
        f"{path.lstrip('/')}"
    )
    params = {
        "ref": GITHUB_BRANCH
    }
    async with httpx.AsyncClient(
        timeout=15
    ) as client:
        response = await client.get(
            url,
            headers=_github_headers(),
            params=params,
        )

    if response.status_code == 403 and "rate limit" in response.text.lower():
        remaining = response.headers.get("x-ratelimit-remaining", "unknown")
        reset = response.headers.get("x-ratelimit-reset", "unknown")
        raise RuntimeError(
            "GITHUB_RATE_LIMIT_EXCEEDED: The website data source (GitHub API) has "
            f"hit its request rate limit (remaining={remaining}, reset_epoch={reset}). "
            "This is a temporary infrastructure issue, not missing data. Do not guess, "
            "infer, or fabricate an answer. Tell the user plainly that live data is "
            "temporarily unavailable due to rate limiting and to try again shortly."
        )
    response.raise_for_status()
    return response.json()


async def jsdelivr_get_raw_content(path: str) -> str:
    """Fetch raw file content from jsDelivr's GitHub CDN (no auth, high rate limit)."""
    url = f"{JSDELIVR_BASE}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url)
    response.raise_for_status()
    return response.text


async def github_get_raw_content(path: str) -> str:
    """
    Fetch a file's content as text. Tries jsDelivr first (no rate limit
    concerns); falls back to the GitHub Contents API + base64 decode only
    if jsDelivr fails (e.g. very recent push not yet mirrored/cached).
    """
    try:
        return await jsdelivr_get_raw_content(path)
    except Exception:
        data = await github_get(path)
        if data.get("type") != "file":
            raise ValueError(f"{path} is not a file.")
        encoded = data.get("content")
        if not encoded:
            raise ValueError(f"No content returned for {path}")
        return base64.b64decode(encoded.replace("\n", "")).decode("utf-8")


def parse_frontmatter(content: str) -> dict:
    """
    Extract YAML frontmatter (delimited by --- ... ---) from markdown content.
    Returns an empty dict if no frontmatter is found or it fails to parse.
    """
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}
    try:
        metadata = yaml.safe_load(match.group(1))
        return metadata if isinstance(metadata, dict) else {}
    except yaml.YAMLError:
        return {}


def strip_frontmatter(content: str) -> str:
    """Return markdown content with the frontmatter block removed."""
    return FRONTMATTER_RE.sub("", content, count=1).lstrip("\n")


def _first_present(metadata: dict, *keys, default=""):
    """
    Return the value of the first key present (and non-empty/non-None) in
    metadata. Frontmatter field names drift between "email"/"emails",
    "phone"/"telephone"/"mobile", etc. Silently returning "" on a name
    mismatch is how the earlier version produced empty/undisclosed contact
    fields even when the resume actually had the data under a slightly
    different key. This does not fabricate anything - it only widens the
    set of accepted spellings for the SAME field.
    """
    for key in keys:
        if key in metadata and metadata[key] not in (None, "", []):
            return metadata[key]
    return default


async def list_markdown_entries(dir_path: str, force_refresh: bool = False) -> list[dict]:
    """
    List markdown files in a GitHub directory, fetching and parsing
    each file's frontmatter metadata (title, subtitle, date, readingTime,
    tags, icon). Results are cached per directory for CACHE_TTL_SECONDS to
    avoid redundant GitHub API calls within a session.

    NOTE: this returns METADATA ONLY, never the article body. Callers that
    need to summarize, quote, or describe the actual content of an entry
    MUST separately call read_blog / read_projects with the entry's "path".
    """
    now = time.time()
    if not force_refresh:
        cached = _entry_cache.get(dir_path)
        if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

    data = await github_get(dir_path)
    if not isinstance(data, list):
        raise ValueError(f"{dir_path} is not a directory")

    entries = []
    for item in data:
        if item.get("type") != "file":
            continue
        name = item.get("name", "")
        path = item.get("path", "")
        if not name.lower().endswith((".md", ".mdx")):
            continue

        content = await github_get_raw_content(path)
        metadata = parse_frontmatter(content)

        entries.append({
            "name": name,
            "path": path,
            "title": metadata.get("title", name),
            "subtitle": metadata.get("subtitle", ""),
            "date": str(metadata.get("date", "")),
            "readingTime": metadata.get("readingTime", ""),
            "tags": metadata.get("tags", []),
            "icon": metadata.get("icon", ""),
            "has_full_content": True,  # signal to the model: call read_blog/read_projects for the body
        })

    # Newest first; entries with no/unparsable date sort last.
    sorted_entries = sorted(
        entries,
        key=lambda x: x.get("date") or "",
        reverse=True,
    )
    _entry_cache[dir_path] = (now, sorted_entries)
    return sorted_entries


async def read_markdown_entry(path: str, root: str, kind: str) -> dict:
    """
    Read a single markdown file, returning its parsed frontmatter
    metadata separately from the body content.
    """
    normalized = path.strip("/")
    root_normalized = root.strip("/")
    if not normalized.startswith(root_normalized + "/"):
        raise ValueError(
            f"Access denied: path is outside the {kind} directory."
        )
    if not normalized.lower().endswith((".md", ".mdx")):
        raise ValueError(
            "Only Markdown/MDX files are allowed."
        )

    content = await github_get_raw_content(normalized)
    metadata = parse_frontmatter(content)
    body = strip_frontmatter(content)

    return {
        "path": normalized,
        "title": metadata.get("title", ""),
        "subtitle": metadata.get("subtitle", ""),
        "date": str(metadata.get("date", "")),
        "readingTime": metadata.get("readingTime", ""),
        "tags": metadata.get("tags", []),
        "icon": metadata.get("icon", ""),
        "content": body,
    }


def _pick_extreme(entries: list[dict], newest: bool) -> dict:
    """
    Deterministically pick the newest or oldest entry by date.
    Recomputes from the given list rather than assuming any prior sort order.
    """
    if not entries:
        return {}
    dated = [e for e in entries if e.get("date")]
    pool = dated or entries
    return (
        max(pool, key=lambda x: x.get("date") or "")
        if newest
        else min(pool, key=lambda x: x.get("date") or "")
    )


def _top_n(entries: list[dict], n: int, newest: bool) -> list[dict]:
    """Deterministically return the top-n entries by date, newest or oldest first."""
    dated = [e for e in entries if e.get("date")]
    pool = dated or entries
    ordered = sorted(pool, key=lambda x: x.get("date") or "", reverse=newest)
    return ordered[: max(0, n)]


# ---------------------------------------------------------------------------
# Blog tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_blogs() -> list[dict]:
    """
    List all blogs currently present in the website repository,
    including title, subtitle, date, reading time, tags, and icon
    parsed from each post's frontmatter. Sorted newest first.

    This returns METADATA ONLY - no article body. To summarize, quote, or
    describe what a blog actually says, you must additionally call
    read_blog(path) with the "path" field from the entry you want.

    Use this for browsing/listing all blogs. For "first/oldest blog" or
    "most recent N blogs" questions, prefer get_first_blog / get_last_blog /
    get_latest_blogs / get_oldest_blogs instead - do not compute min/max
    yourself from this list.
    """
    return await list_markdown_entries(BLOG_PATH)


@mcp.tool()
async def get_first_blog() -> dict:
    """
    Return the single oldest blog (the first one Abhinav ever wrote),
    determined by comparing dates across ALL blogs, not just recently
    discussed ones. Always use this tool - do not infer the first blog
    from a partial list already seen in conversation.

    Returns METADATA ONLY. Call read_blog(path) on the returned "path" if
    you need to summarize, quote, or describe the actual article content.
    """
    entries = await list_markdown_entries(BLOG_PATH)
    return _pick_extreme(entries, newest=False)


@mcp.tool()
async def get_last_blog() -> dict:
    """
    Return the single most recent blog Abhinav has written, determined
    by comparing dates across ALL blogs.

    Returns METADATA ONLY. Call read_blog(path) on the returned "path" if
    you need to summarize, quote, or describe the actual article content.
    """
    entries = await list_markdown_entries(BLOG_PATH)
    return _pick_extreme(entries, newest=True)


@mcp.tool()
async def get_latest_blogs(n: int = 4) -> list[dict]:
    """
    Return the n most recent blogs, newest first, computed by sorting
    ALL blogs by date. Use this instead of manually picking from list_blogs
    output.

    Returns METADATA ONLY per entry. Call read_blog(path) on a specific
    entry's "path" if you need to summarize, quote, or describe its content.
    """
    entries = await list_markdown_entries(BLOG_PATH)
    return _top_n(entries, n, newest=True)


@mcp.tool()
async def get_oldest_blogs(n: int = 4) -> list[dict]:
    """
    Return the n oldest blogs, oldest first, computed by sorting ALL
    blogs by date.

    Returns METADATA ONLY per entry. Call read_blog(path) on a specific
    entry's "path" if you need to summarize, quote, or describe its content.
    """
    entries = await list_markdown_entries(BLOG_PATH)
    return _top_n(entries, n, newest=False)


@mcp.tool()
async def read_blog(path: str) -> dict:
    """
    Read the COMPLETE contents of a single blog post, including its
    frontmatter metadata (title, subtitle, date, readingTime, tags, icon)
    and the full markdown body (frontmatter stripped out) in the
    "content" field.

    This is the ONLY tool that returns actual article text. Call this
    whenever the user wants a summary, description, quote, or the full
    content of a specific blog - list_blogs/get_last_blog/etc. do not
    contain the body and are not a substitute for this call.
    """
    return await read_markdown_entry(path, BLOG_PATH, "blog")


# ---------------------------------------------------------------------------
# Project tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_projects() -> list[dict]:
    """
    List all projects currently present in the website repository,
    including title, subtitle, date, reading time, tags, and icon
    parsed from each project's frontmatter. Sorted newest first.

    This returns METADATA ONLY - no project body. To summarize, quote, or
    describe what a project actually says, you must additionally call
    read_projects(path) with the "path" field from the entry you want.

    For "first/oldest project" or "most recent N projects" questions,
    prefer get_first_project / get_last_project / get_latest_projects /
    get_oldest_projects instead of computing min/max yourself.
    """
    return await list_markdown_entries(PROJECTS_PATH)


@mcp.tool()
async def get_first_project() -> dict:
    """
    Return the single oldest project (the first one Abhinav worked on),
    determined by comparing dates across ALL projects.

    Returns METADATA ONLY. Call read_projects(path) on the returned "path"
    if you need to summarize, quote, or describe the actual project content.
    """
    entries = await list_markdown_entries(PROJECTS_PATH)
    return _pick_extreme(entries, newest=False)


@mcp.tool()
async def get_last_project() -> dict:
    """
    Return the single most recent project, determined by comparing
    dates across ALL projects.

    Returns METADATA ONLY. Call read_projects(path) on the returned "path"
    if you need to summarize, quote, or describe the actual project content.
    """
    entries = await list_markdown_entries(PROJECTS_PATH)
    return _pick_extreme(entries, newest=True)


@mcp.tool()
async def get_latest_projects(n: int = 4) -> list[dict]:
    """
    Return the n most recent projects, newest first.

    Returns METADATA ONLY per entry. Call read_projects(path) on a specific
    entry's "path" if you need to summarize, quote, or describe its content.
    """
    entries = await list_markdown_entries(PROJECTS_PATH)
    return _top_n(entries, n, newest=True)


@mcp.tool()
async def get_oldest_projects(n: int = 4) -> list[dict]:
    """
    Return the n oldest projects, oldest first.

    Returns METADATA ONLY per entry. Call read_projects(path) on a specific
    entry's "path" if you need to summarize, quote, or describe its content.
    """
    entries = await list_markdown_entries(PROJECTS_PATH)
    return _top_n(entries, n, newest=False)


@mcp.tool()
async def read_projects(path: str) -> dict:
    """
    Read the COMPLETE contents of a single project, including its
    frontmatter metadata (title, subtitle, date, readingTime, tags, icon)
    and the full markdown body (frontmatter stripped out) in the
    "content" field.

    This is the ONLY tool that returns actual project text. Call this
    whenever the user wants a summary, description, quote, or the full
    content of a specific project - list_projects/get_last_project/etc.
    do not contain the body and are not a substitute for this call.
    """
    return await read_markdown_entry(path, PROJECTS_PATH, "project")


# ---------------------------------------------------------------------------
# Resume tool
# ---------------------------------------------------------------------------

@mcp.tool()
async def read_resume() -> dict:
    """
    Read Abhinav's resume. Returns structured contact fields (name, title,
    location, phone, email, linkedin, github, website) separately from the
    free-text body (skills, experience, projects, etc.).

    Call this tool - and quote its "contact" fields verbatim - for ANY
    question about Abhinav's phone number, email address, LinkedIn,
    GitHub, or personal website. Never answer a contact-info question
    from memory or by guessing/pattern-completing a number or address;
    always ground the answer in this tool's output.
    """
    content = await github_get_raw_content(RESUME_PATH)
    metadata = parse_frontmatter(content)
    body = strip_frontmatter(content)

    return {
        "path": RESUME_PATH,
        "contact": {
            "name": _first_present(metadata, "name", "full_name"),
            "title": _first_present(metadata, "title", "role", "headline"),
            "location": _first_present(metadata, "location", "city"),
            "phone": _first_present(metadata, "phone", "telephone", "mobile", "phone_number"),
            "email": _first_present(metadata, "email", "emails", "email_address", default=[]),
            "linkedin": _first_present(metadata, "linkedin", "linkedIn", "linkedin_url"),
            "github": _first_present(metadata, "github", "github_url"),
            "website": _first_present(metadata, "website", "site", "url"),
        },
        # skills/experience/projects prose; contact data intentionally not duplicated here
        "content": body,
    }


if __name__ == "__main__":

    mcp.run(
        transport="stdio"
    )