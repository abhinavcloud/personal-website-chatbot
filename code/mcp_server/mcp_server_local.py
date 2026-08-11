import os
import re
import base64
import httpx
import yaml
from mcp.server.fastmcp import FastMCP


GITHUB_OWNER = "abhinavcloud"
GITHUB_REPO = "PersonalWebsite"
GITHUB_BRANCH = "main"
BLOG_PATH = "site/blog/posts"
PROJECTS_PATH = "site/projects/project"
RESUME_PATH = "site/resume/resume.md"
GITHUB_API = "https://api.github.com"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)

mcp = FastMCP(
    "Abhinav Personal Website"
)


async def github_get(path: str):
    url = (
        f"{GITHUB_API}/repos/"
        f"{GITHUB_OWNER}/"
        f"{GITHUB_REPO}/contents/"
        f"{path.lstrip('/')}"
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "abhinav-personal-website-mcp",
    }
    params = {
        "ref": GITHUB_BRANCH
    }
    async with httpx.AsyncClient(
        timeout=15
    ) as client:
        response = await client.get(
            url,
            headers=headers,
            params=params,
        )
    response.raise_for_status()
    return response.json()


async def github_get_raw_content(path: str) -> str:
    """Fetch a file from GitHub and decode its base64 content to text."""
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


async def list_markdown_entries(dir_path: str) -> list[dict]:
    """
    List markdown files in a GitHub directory, fetching and parsing
    each file's frontmatter metadata (title, subtitle, date, readingTime,
    tags, icon).
    """
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
        })

    # Newest first; entries with no/unparsable date sort last.
    return sorted(
        entries,
        key=lambda x: x.get("date") or "",
        reverse=True,
    )


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


@mcp.tool()
async def list_blogs() -> list[dict]:
    """
    List all blogs currently present in the website repository,
    including title, subtitle, date, reading time, tags, and icon
    parsed from each post's frontmatter. Sorted newest first.
    """
    return await list_markdown_entries(BLOG_PATH)


@mcp.tool()
async def read_blog(path: str) -> dict:
    """
    Read the complete contents of a blog, including its frontmatter
    metadata (title, subtitle, date, readingTime, tags, icon) and the
    markdown body with the frontmatter stripped out.
    """
    return await read_markdown_entry(path, BLOG_PATH, "blog")


@mcp.tool()
async def list_projects() -> list[dict]:
    """
    List all projects currently present in the website repository,
    including title, subtitle, date, reading time, tags, and icon
    parsed from each project's frontmatter. Sorted newest first.
    """
    return await list_markdown_entries(PROJECTS_PATH)


@mcp.tool()
async def read_projects(path: str) -> dict:
    """
    Read the complete contents of a project, including its frontmatter
    metadata (title, subtitle, date, readingTime, tags, icon) and the
    markdown body with the frontmatter stripped out.
    """
    return await read_markdown_entry(path, PROJECTS_PATH, "project")

@mcp.tool()
async def read_resume() -> dict:
    """
    Read the complete contents of Abhinav's resume, including his contact details, LinkedIn profile,
    personal website, GitHub profile, skills, certifications, projects, work experience, and companies
    he has worked for.
    """
    content = await github_get_raw_content(RESUME_PATH)
    metadata = parse_frontmatter(content)
    body = strip_frontmatter(content)

    return {
        "path": RESUME_PATH,
        "content": body,
    }


if __name__ == "__main__":

    mcp.run(
        transport="stdio"
    )