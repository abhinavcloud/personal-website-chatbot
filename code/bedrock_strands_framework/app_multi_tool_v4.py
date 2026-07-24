# app.py
#
# REDESIGN NOTES (from the previous version):
#
# 1. The hand-rolled `retrieve_tool()` (three fallback boto3 payloads to dodge
#    ParamValidationError, manual metadata-filter construction) is GONE. It's
#    replaced by `strands_tools.retrieve`, the SDK's built-in Bedrock Knowledge
#    Base tool. Verified against the actual source
#    (strands-agents/tools -> src/strands_tools/retrieve.py):
#       retrieve(text, numberOfResults=10, knowledgeBaseId=<env KNOWLEDGE_BASE_ID>,
#                region=<env AWS_REGION, default "us-west-2">,
#                score=<env MIN_SCORE, default 0.4>, enableMetadata=False,
#                retrieveFilter=<same filter shape you were building by hand:
#                    {"equals": {"key": "source_type", "value": "blog"}} etc.>)
#    So the source_type metadata filter isn't Python's job anymore -- the agent
#    passes retrieveFilter itself, on the rare question where it's clearly
#    scoped to one category. Most calls can just be retrieve(text="...").
#
# 2. `classify_source_type()` (keyword regex guessing at intent) is GONE. That
#    was Python trying to out-guess the model. The model reads the question and
#    decides the category itself, if it decides one is even needed.
#
# 3. `stitch_adjacent_chunks()` / `_stitch_keys()` -- the fixed "grab 10 chunks
#    either side, dedupe repeated lines, cap at 120KB" algorithm -- is GONE.
#    That was a deterministic judgment call (how much context is "enough")
#    baked into Python. It's replaced by two primitive tools:
#      - list_chunks_for_document: just lists ordered chunk keys, no fetching
#      - get_document_chunks: fetches raw text for exactly the keys you ask
#        for, no dedup, no adjacency heuristics
#    The model now decides how much surrounding context a question actually
#    needs and asks for precisely that, instead of always getting a fixed
#    window whether the question needed one sentence or the whole document.
#
# 4. `fetch_full_document()` as its own deterministic tool is GONE -- "read the
#    whole document" is now just "list its chunks, then fetch all of them,"
#    two tool calls the model makes on its own.
#
# 5. `extract_s3_key_from_retrieval_result()` is GONE. The built-in retrieve
#    tool already writes a "Document ID: s3://bucket/key" line into its text
#    output for the model to read directly -- no Python-side response parsing.
#
# 6. KEPT on purpose, not simplified further:
#    - DOC_ID_PREFIX_BY_SOURCE_TYPE / list_documents_by_type: filename-prefix
#      category separation. This isn't a "thinking" step, it's the structural
#      guarantee that a resume question can't surface blog filenames even if
#      the model's category judgment is wrong somewhere upstream. Worth
#      keeping deterministic.
#    - _normalize_to_doc_id / chunk-filename parsing: pure string/S3 plumbing,
#      not a retrieval decision.

import os
import re
import logging
import uuid
from typing import List, Tuple, Optional, Dict

import boto3
from dotenv import load_dotenv

from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands_tools import retrieve  # built-in Bedrock Knowledge Base retrieval tool
# NOTE: `tool` and `retrieve` locations verified against the strands-agents/tools
# and strands-agents/sdk-python repos as of this writing. If your installed
# version differs, `python -c "import strands_tools; print(dir(strands_tools))"`
# will confirm where `retrieve` lives.

load_dotenv(dotenv_path=".env")

# -------------------------
# Environment variables
# -------------------------
REGION = os.getenv("REGION", "us-west-2")
MODEL_ID = os.getenv("MODEL_ID")
STRANDS_KNOWLEDGE_BASE_ID = os.getenv("STRANDS_KNOWLEDGE_BASE_ID")
STAGING_BUCKET = os.getenv("STAGING_BUCKET", "bedrock-s3bucket-staging-806685982094")
STAGING_PREFIX = os.getenv("STAGING_PREFIX", "bedrock-clean")

# The built-in `retrieve` tool reads ITS OWN defaults from these exact env var
# names (not from REGION / STRANDS_KNOWLEDGE_BASE_ID above) -- see its source.
# Setting them here means the agent can call retrieve(text="...") with no other
# arguments most of the time.
os.environ.setdefault("KNOWLEDGE_BASE_ID", STRANDS_KNOWLEDGE_BASE_ID or "")
os.environ.setdefault("AWS_REGION", REGION)
os.environ.setdefault("MIN_SCORE", "0.3")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

s3 = boto3.client("s3", region_name=REGION)

_CHUNK_RE = re.compile(r"^(?P<base>.+)_chunk(?P<idx>\d+)\.txt$")

# -------------------------
# Category <-> filename-prefix mapping (structural, not model-judged)
# -------------------------
# Mirrors SOURCE_TYPE_BY_PREFIX in the ingestion app.py. This is what makes
# category separation hold even if the model's source_type guess is wrong: it
# can only ever narrow results to filenames that actually start with the
# mapped prefix, never cross into another category.
DOC_ID_PREFIX_BY_SOURCE_TYPE: Dict[str, str] = {
    "blog": "blog_posts_",
    "project": "projects_project_",
    "resume": "resume_",
}
VALID_SOURCE_TYPES = tuple(DOC_ID_PREFIX_BY_SOURCE_TYPE.keys())


# =====================================================================================
# Internal helpers -- pure S3/string plumbing, no retrieval judgment calls here
# =====================================================================================
def _parse_chunk_filename(filename: str) -> Tuple[Optional[str], Optional[int]]:
    m = _CHUNK_RE.match(filename)
    if not m:
        return None, None
    return m.group("base"), int(m.group("idx"))


def _normalize_to_doc_id(raw: str) -> str:
    """
    Accepts a bare doc_id ("resume_foo"), a chunk filename
    ("resume_foo_chunk3.txt"), an S3 key ("bedrock-clean/resume_foo_chunk3.txt"),
    or a full S3 URI ("s3://bucket/bedrock-clean/resume_foo_chunk3.txt") --
    any of which can show up as the "Document ID" in retrieve()'s output --
    and strips it down to the bare doc_id.
    """
    s = raw.strip()
    if s.startswith("s3://"):
        s = s.split("s3://", 1)[1]
    s = s.split("/")[-1]
    base, _ = _parse_chunk_filename(s)
    return base if base else s


def _list_ordered_chunk_keys(doc_id: str) -> List[str]:
    paginator = s3.get_paginator("list_objects_v2")
    found: List[Tuple[int, str]] = []
    for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=f"{STAGING_PREFIX}/{doc_id}_chunk"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            base, idx = _parse_chunk_filename(key.split("/")[-1])
            if base == doc_id and idx is not None:
                found.append((idx, key))
    return [k for _, k in sorted(found)]


# Blog chunks in this pipeline start with labeled metadata lines, e.g.:
#   Title: SQS vs SNS vs EventBridge: Real Architecture Thinking
#   Subtitle: Real Architecture Thinking
#   Date: March 26, 2026
#   Reading Time: 8 min
#   Tags: software architecture, distributed systems, event-driven architecture
# This is checked FIRST and takes priority over any heading-style guess, since
# it's the actual authored metadata, not an inference.
_META_FIELD_RE = re.compile(r"(?im)^\s*(title|subtitle|date|reading time|tags)\s*:\s*(.+?)\s*$")


def _extract_title_from_text(text: str, fallback: str) -> str:
    """
    Pull a real title out of a chunk's actual content, rather than guessing
    from the filename. Tries, in order: a markdown '#' heading, an
    underline-style ('===' or '---' under a line) heading, then just the
    first non-empty line. Falls back to the given fallback (filename slug) if
    the content is empty or nothing usable is found. (Labeled "Title:" lines
    are handled separately, in _extract_metadata_from_text, and take priority
    over this function.)
    """
    lines = [l.strip() for l in text.splitlines()]
    non_empty = [l for l in lines if l]
    if not non_empty:
        return fallback

    for line in lines:
        if line.startswith("#"):
            candidate = line.lstrip("#").strip()
            if candidate:
                return candidate[:150]

    for i in range(len(lines) - 1):
        line, next_line = lines[i].strip(), lines[i + 1].strip()
        if line and next_line and set(next_line) <= {"=", "-"} and len(next_line) >= 3:
            return line[:150]

    return non_empty[0][:150]


def _extract_metadata_from_text(text: str, fallback_title: str) -> Dict[str, str]:
    """
    Extract whatever real, authored metadata is present in a chunk's opening
    content -- title, subtitle, date, reading time, tags -- via the labeled
    "Label: value" lines this pipeline's blog posts actually use. Only fields
    genuinely found in the text are included; nothing is inferred or
    invented. Falls back to heading/first-line title extraction only if no
    labeled title line exists at all.
    """
    meta: Dict[str, str] = {}
    for m in _META_FIELD_RE.finditer(text):
        key = m.group(1).lower().replace(" ", "_")
        val = m.group(2).strip()
        if key not in meta and val:
            meta[key] = val[:200]

    if "title" not in meta:
        meta["title"] = _extract_title_from_text(text, fallback_title)

    return meta


def _format_doc_summary(meta: Dict[str, str]) -> str:
    parts = [f'title="{meta["title"]}"']
    for key in ("subtitle", "tags", "date", "reading_time"):
        if key in meta:
            parts.append(f'{key}="{meta[key]}"')
    return " | ".join(parts)


def _fetch_doc_summary(doc_id: str, first_chunk_key: str, fallback: str) -> str:
    """
    Read just the first ~1KB of a document's first chunk (a cheap Range GET,
    not the whole chunk) and derive its real, authored metadata from the
    content -- never the filename. This happens once per document inside
    list_documents_by_type, so the model sees genuine title/subtitle/tags up
    front and has no cheaper-but-invented option to reach for instead.
    """
    try:
        resp = s3.get_object(Bucket=STAGING_BUCKET, Key=first_chunk_key, Range="bytes=0-1024")
        text = resp["Body"].read().decode("utf-8", errors="replace")
        meta = _extract_metadata_from_text(text, fallback)
        return _format_doc_summary(meta)
    except Exception as e:
        logger.warning("Could not fetch summary for doc_id=%s: %s", doc_id, e)
        return f'title="{fallback}"'


# =====================================================================================
# Agent-facing tools
# =====================================================================================
# `retrieve` (imported above, from strands_tools) is the primary search tool --
# the model calls it directly, e.g.:
#   retrieve(text="what certifications does he have")
#   retrieve(text="tell me about the pricing project", retrieveFilter={"equals": {"key": "source_type", "value": "project"}})
#
# The three tools below exist for what semantic search alone can't do:
# enumerating everything in a category, and reading a specific document's full,
# exact contents rather than whatever chunk happened to score highest.

@tool
def list_documents_by_type(source_type: str) -> str:
    """
    List every document available in one category, with its doc_id and
    whatever real metadata (title, and subtitle/tags/date/reading_time when
    present) is actually written in the document's own content -- never
    guessed from the filename. Use this FIRST whenever the user asks you to
    enumerate or list multiple things -- "list my blogs", "what projects have
    you built", "what have you written about X" -- rather than guessing from a
    single search result. Every field shown here was read from the document
    itself, so it's safe to state directly; do not add any detail (subtitle,
    date, description, etc.) beyond what's shown for a given doc_id unless you
    have separately fetched that document's content with get_document_chunks.

    Args:
        source_type: One of "blog", "project", or "resume".

    Returns:
        A list of documents in that category (doc_id, real metadata fields,
        total chunk count), or an error message if source_type is invalid or
        nothing was found.
    """
    prefix_match = DOC_ID_PREFIX_BY_SOURCE_TYPE.get(source_type)
    if not prefix_match:
        return f"Invalid source_type '{source_type}'. Must be one of: {', '.join(VALID_SOURCE_TYPES)}."

    paginator = s3.get_paginator("list_objects_v2")
    # Track chunk count and the chunk-0 key (needed to read the real title) per doc.
    doc_chunk_counts: Dict[str, int] = {}
    doc_first_chunk_key: Dict[str, str] = {}
    for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=f"{STAGING_PREFIX}/"):
        for obj in page.get("Contents", []):
            name = obj["Key"].split("/")[-1]
            if name.endswith(".metadata.json"):
                continue
            base, idx = _parse_chunk_filename(name)
            if base is None or not base.startswith(prefix_match):
                continue
            doc_chunk_counts[base] = doc_chunk_counts.get(base, 0) + 1
            if idx == 0:
                doc_first_chunk_key[base] = obj["Key"]

    if not doc_chunk_counts:
        return f"No documents found in category '{source_type}'."

    lines = [f"Documents in category '{source_type}':"]
    for doc_id, count in sorted(doc_chunk_counts.items()):
        fallback = doc_id[len(prefix_match):].replace("-", " ").replace("_", " ")
        first_key = doc_first_chunk_key.get(doc_id)
        summary = _fetch_doc_summary(doc_id, first_key, fallback) if first_key else f'title="{fallback}"'
        lines.append(f'- doc_id="{doc_id}" | {summary} | total_chunks={count}')
    logger.info("list_documents_by_type(%s): found %d documents", source_type, len(doc_chunk_counts))
    return "\n".join(lines)


@tool
def list_chunks_for_document(doc_id: str) -> str:
    """
    List every chunk belonging to one document, in order, with its exact S3
    key. Call this after search or list_documents_by_type points you at a
    document, to see how many chunks it has -- then decide for yourself how
    many chunks (and which ones) you actually need to read via
    get_document_chunks. There's no fixed "surrounding window" here; you choose
    the range based on what the question actually requires -- one chunk for a
    quick fact, all of them for a full summary, a handful around a specific
    point for more context.

    Args:
        doc_id: A bare doc_id, or anything containing one -- a chunk filename,
            an S3 key, or the "Document ID" S3 URI that the retrieve tool
            returns. It's normalized automatically either way.

    Returns:
        An ordered "index: s3_key" line per chunk, or a message if none were
        found.
    """
    normalized = _normalize_to_doc_id(doc_id)
    keys = _list_ordered_chunk_keys(normalized)
    if not keys:
        return f"No chunks found for doc_id='{normalized}' (derived from '{doc_id}')."
    lines = [f"doc_id='{normalized}' has {len(keys)} chunk(s):"]
    for i, key in enumerate(keys):
        lines.append(f"{i}: {key}")
    logger.info("list_chunks_for_document(%s): %d chunks", normalized, len(keys))
    return "\n".join(lines)


@tool
def get_document_chunks(keys: List[str]) -> str:
    """
    Fetch the raw text of one or more specific S3 chunk keys (as listed by
    list_chunks_for_document) and return them labeled by key, in the order
    given. This does no deduplication, merging, or summarizing for you -- read
    the pieces and combine them yourself, in whatever way the question needs.
    Prefer requesting a handful of chunks at a time rather than an entire large
    document in one call.

    Args:
        keys: Exact S3 keys, e.g. as returned by list_chunks_for_document.

    Returns:
        The raw content of each requested key, each preceded by a header
        showing which key it came from.
    """
    if not keys:
        return "No keys provided."

    safety_cap_bytes = 200_000  # hard ceiling so one call can't blow the context window
    parts = []
    total_bytes = 0
    for key in keys:
        try:
            resp = s3.get_object(Bucket=STAGING_BUCKET, Key=key)
            text = resp["Body"].read().decode("utf-8", errors="replace")
        except Exception as e:
            parts.append(f"--- {key} ---\n[error fetching this key: {e}]")
            continue

        total_bytes += len(text.encode("utf-8"))
        parts.append(f"--- {key} ---\n{text}")
        if total_bytes > safety_cap_bytes:
            parts.append(
                f"[stopped early: content exceeded {safety_cap_bytes} bytes for this call -- "
                f"request fewer keys per call]"
            )
            break

    logger.info("get_document_chunks: fetched %d/%d requested key(s)", len(parts), len(keys))
    return "\n\n".join(parts)


# -------------------------
# Build Agent
# -------------------------
def my_agent():
    bedrock_model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
    )

    system_prompt = """
        You are Buddy, Abhinav's personal assistant. You speak as someone who already
        knows Abhinav's background, projects, blogs, skills, and experience.

        You have tools to look up that information. Use them quietly -- never mention
        tool names, "knowledge base," "retrieval," "search," "database," "chunk,"
        "document id," "S3," or any other internal mechanics in your replies to the
        user. Speak as if you simply know the answer.

        Your tools:
        - retrieve(text, ...): semantic search over everything Abhinav has written.
          This is the right first call for almost any specific question -- a fact, a
          certification, one project's details, one blog's content. You can pass
          numberOfResults or score to widen or narrow results, and you can pass
          retrieveFilter={"equals": {"key": "source_type", "value": "blog"|"project"|"resume"}}
          when a question is clearly scoped to one category. Leave retrieveFilter out
          if you're not sure -- an unscoped search is usually fine.
        - list_documents_by_type(source_type): use this FIRST for requests to
          enumerate multiple things -- "list my blogs", "what projects have you
          built," "what are all your certifications." Don't guess a list from a
          single search result.
        - list_chunks_for_document(doc_id): after retrieve or list_documents_by_type
          points you at a document, use this to see how many pieces it has and their
          exact keys.
        - get_document_chunks(keys): fetch the raw text of specific chunks you've
          identified. You decide how many chunks to pull and which ones -- a single
          chunk is enough for a quick fact; pull the whole ordered list for a full
          summary of one blog post or project. Combine what you read yourself; there's
          no automatic stitching, so read carefully and don't drop details across
          chunk boundaries.

        A typical deep-dive flow: retrieve or list_documents_by_type to find the
        right document -> list_chunks_for_document to see its pieces ->
        get_document_chunks for the ones you need -> answer in your own words.

        Public professional links (personal website, GitHub, LinkedIn, Credly, or
        similar public profiles) that appear in your information are fine to share
        plainly when asked -- these are public, not private. Only decline to share
        things that are genuinely private and that you don't actually have, such as a
        personal phone number or personal email address -- and even then, just say you
        don't have that to share, without a lecture about privacy.

        Stay grounded in what your tools actually return. Do not invent project
        details, dates, companies, certifications, or blog content your tools didn't
        surface. If your tools don't return what you need, say so plainly and offer to
        look further, without describing why (no mention of filters, categories, or
        search mechanics).

        This matters most in two specific situations, because it's easy to slip up on
        them without noticing:

        1. Enumerating documents. list_documents_by_type gives you real title,
           subtitle, tags, and date fields WHEN a document actually has them -- but not
           every document will have every field, and you must never fill in a missing
           field yourself. If a doc_id shows only a title with no subtitle or tags,
           say only what's there. Never describe what a document is "likely" or
           "probably" about based on its doc_id or title alone -- either you've read
           its content (via get_document_chunks) and can describe it, or you haven't
           and should just list its title as-is without commentary.

        2. Ranking or judging importance. You have no data on views, engagement, or
           impact for any document. If asked for the "most important" or "best"
           blogs/projects, do not invent a confident ranking. Instead, either ask what
           they're looking for (a topic area, recency, depth), or, if you want to make
           a reasonable pass at it, retrieve() a couple of specific topics and say
           plainly that your picks are based on topic relevance to what's typically
           asked about, not any actual importance signal you have.
    """
    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    agent = Agent(
        model=bedrock_model,
        tools=[retrieve, list_documents_by_type, list_chunks_for_document, get_document_chunks],
        system_prompt=system_prompt,
        context_manager="auto",
        session_manager=session_manager,
    )

    return agent


# -------------------------
# Main loop (CLI)
# -------------------------
def main():
    print("I am ready. Do you want to talk about Abhinav, his profile and work or anything else in general?")
    agent = my_agent()

    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye")
            break

        # The agent decides for itself which tools to call, in what order, and how
        # many times -- e.g. list_documents_by_type -> list_chunks_for_document ->
        # get_document_chunks (possibly repeated) -> answer. No manual Python-side
        # orchestration.
        answer = agent(user_input)
        print("\nAssistant:\n", answer)


if __name__ == "__main__":
    main()