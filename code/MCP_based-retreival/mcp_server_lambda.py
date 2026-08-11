import os
import base64
from typing import Any

import httpx
from mangum import Mangum
from mcp.server.fastmcp import FastMCP


# ============================================================
# Configuration
# ============================================================

GITHUB_OWNER = os.environ.get(
    "GITHUB_OWNER",
    "abhinavcloud",
)

GITHUB_REPO = os.environ.get(
    "GITHUB_REPO",
    "PersonalWebsite",
)

GITHUB_BRANCH = os.environ.get(
    "GITHUB_BRANCH",
    "main",
)

BLOG_PATH = os.environ.get(
    "BLOG_PATH",
    "src/content/blogs",
)

GITHUB_API = "https://api.github.com"

# Optional safety limit.
# Prevents accidentally returning enormous files.
MAX_BLOG_SIZE = int(
    os.environ.get(
        "MAX_BLOG_SIZE",
        str(500_000),  # 500 KB
    )
)


# ============================================================
# GitHub API helper
# ============================================================

async def github_get(path: str) -> Any:
    """
    Read a file or directory from the public GitHub repository.
    """

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
        "ref": GITHUB_BRANCH,
    }

    async with httpx.AsyncClient(
        timeout=15.0
    ) as client:

        response = await client.get(
            url,
            headers=headers,
            params=params,
        )

    if response.status_code == 404:
        raise ValueError(
            f"GitHub path not found: {path}"
        )

    if response.status_code == 403:
        raise ValueError(
            "GitHub API rate limit or access restriction."
        )

    response.raise_for_status()

    return response.json()


# ============================================================
# Security helpers
# ============================================================

def normalize_path(path: str) -> str:
    """
    Normalize a repository path and prevent path traversal.
    """

    normalized = path.strip().lstrip("/")

    if ".." in normalized.split("/"):
        raise ValueError(
            "Invalid path."
        )

    return normalized


def is_blog_path(path: str) -> bool:
    """
    Ensure the requested file is inside the configured
    blog directory.
    """

    normalized_path = normalize_path(path)
    normalized_root = normalize_path(BLOG_PATH)

    return (
        normalized_path.startswith(
            normalized_root + "/"
        )
        and normalized_path.lower().endswith(
            (".md", ".mdx")
        )
    )


# ============================================================
# MCP server factory
# ============================================================

def create_mcp_server() -> FastMCP:

    mcp = FastMCP(
        name="Abhinav Personal Website",
        stateless_http=True,
        json_response=True,
    )

    # ========================================================
    # Tool 1: list_blogs
    # ========================================================

    @mcp.tool()
    async def list_blogs() -> list[dict[str, str]]:
        """
        List all blog posts currently present in the
        PersonalWebsite repository.

        Use this tool when the user asks:
        - What blogs have I written?
        - What articles have I written?
        - What technical blogs are on my website?
        - Which blogs do I have?
        - Find my blogs about a topic.

        The repository is the source of truth. The list is
        generated dynamically from GitHub and does not require
        a manually maintained index.
        """

        data = await github_get(
            BLOG_PATH
        )

        if not isinstance(data, list):
            raise ValueError(
                f"'{BLOG_PATH}' is not a directory."
            )

        blogs = []

        for item in data:

            if item.get("type") != "file":
                continue

            name = item.get(
                "name",
                "",
            )

            path = item.get(
                "path",
                "",
            )

            if not name.lower().endswith(
                (".md", ".mdx")
            ):
                continue

            blogs.append(
                {
                    "name": name,
                    "path": path,
                }
            )

        blogs.sort(
            key=lambda x: x["name"].lower()
        )

        return blogs

    # ========================================================
    # Tool 2: read_blog
    # ========================================================

    @mcp.tool()
    async def read_blog(
        path: str,
    ) -> str:
        """
        Read the complete contents of a blog from the
        PersonalWebsite repository.

        The path must be a Markdown or MDX file returned
        by list_blogs().

        Use this tool when the user asks:
        - Summarize one of my blogs.
        - What did I write about X?
        - Explain my blog about Y.
        - What does my blog say about Z?
        - Give me the key points from my blog.
        """

        normalized_path = normalize_path(
            path
        )

        # ----------------------------------------------------
        # Security boundary
        # ----------------------------------------------------

        if not is_blog_path(
            normalized_path
        ):
            raise ValueError(
                "Access denied. "
                "read_blog() can only access Markdown "
                "files inside the configured blog directory."
            )

        # ----------------------------------------------------
        # Fetch file
        # ----------------------------------------------------

        data = await github_get(
            normalized_path
        )

        if data.get("type") != "file":
            raise ValueError(
                f"'{path}' is not a file."
            )

        encoded_content = data.get(
            "content"
        )

        if not encoded_content:
            raise ValueError(
                f"GitHub returned no content for '{path}'."
            )

        # GitHub returns base64 content.
        content_bytes = base64.b64decode(
            encoded_content.replace(
                "\n",
                "",
            )
        )

        # ----------------------------------------------------
        # Size protection
        # ----------------------------------------------------

        if len(content_bytes) > MAX_BLOG_SIZE:
            raise ValueError(
                f"Blog '{path}' is larger than the "
                f"configured {MAX_BLOG_SIZE} byte limit."
            )

        # ----------------------------------------------------
        # Decode Markdown
        # ----------------------------------------------------

        try:
            content = content_bytes.decode(
                "utf-8"
            )
        except UnicodeDecodeError:
            raise ValueError(
                f"Blog '{path}' is not valid UTF-8 text."
            )

        return content

    return mcp


# ============================================================
# Lambda handler
# ============================================================

def lambda_handler(
    event,
    context,
):
    """
    AWS Lambda entry point.

    Each invocation gets a fresh stateless MCP server.
    """

    mcp = create_mcp_server()

    app = mcp.streamable_http_app()

    handler = Mangum(
        app,
        lifespan="auto",
    )

    return handler(
        event,
        context,
    )