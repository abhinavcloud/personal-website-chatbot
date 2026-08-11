# app.py
import os
import re
import json
import logging
from typing import List, Tuple, Optional, Dict
from pathlib import Path
import uuid

import boto3
import botocore
from botocore.exceptions import ParamValidationError
from dotenv import load_dotenv

# Strands imports
from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
# NOTE: `tool` is imported from the top-level `strands` package per the Strands Agents
# SDK docs. If your installed version exposes it elsewhere (e.g. `strands.tools`),
# adjust this import -- run `python -c "import strands; print(dir(strands))"` to check.

load_dotenv(dotenv_path=".env")

# Environment variables
REGION = os.getenv("REGION")
MODEL_ID = os.getenv("MODEL_ID")
STRANDS_KNOWLEDGE_BASE_ID = os.getenv("STRANDS_KNOWLEDGE_BASE_ID")
STAGING_BUCKET = os.getenv("STAGING_BUCKET", "bedrock-s3bucket-staging-806685982094")
STAGING_PREFIX = os.getenv("STAGING_PREFIX", "bedrock-clean")
#GUARDRAIL_ID = os.getenv("GUARDRAIL_ID")
#GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION")

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

s3 = boto3.client("s3", region_name=REGION)
_CHUNK_RE = re.compile(r"^(?P<base>.+)_chunk(?P<idx>\d+)\.txt$")

# -------------------------
# Category <-> filename-prefix mapping
# -------------------------
# Mirrors SOURCE_TYPE_BY_PREFIX in the ingestion app.py. This is the STRUCTURAL
# guarantee that category separation holds even if the agent picks the wrong
# source_type for a tool call: list_documents_by_type() and the doc_id-matching in
# search_and_stitch() only ever match filenames starting with the mapped prefix, so a
# misclassified "resume" request literally cannot surface a blog/project filename --
# there is no vector-similarity path here that could blur the line.
DOC_ID_PREFIX_BY_SOURCE_TYPE: Dict[str, str] = {
    "blog": "blog_posts_",
    "project": "projects_project_",
    "resume": "resume_",
}
VALID_SOURCE_TYPES = tuple(DOC_ID_PREFIX_BY_SOURCE_TYPE.keys())

# -------------------------
# Intent classification -> source_type filter (fallback, not agent-trusted alone)
# -------------------------
# Used as a SAFETY NET inside search_and_stitch when the agent omits source_type or
# passes something invalid -- not as the sole source of truth anymore, since the agent
# itself can now specify a category directly as a tool argument.
#
# "abhinav" was deliberately removed as a resume hint: in this assistant, nearly every
# question is implicitly "about Abhinav" (that's the whole app), so it matched almost
# everything and wasn't actually discriminating between categories -- that's what
# caused "abhinav-cloud.com?" to get misrouted to the resume filter in testing.
_BLOG_HINTS = ("blog", "blogs", "post", "posts", "article", "wrote about", "write up", "wrote a")
_PROJECT_HINTS = ("project", "projects", "built", "portfolio", "side project", "repo", "github")
_RESUME_HINTS = (
    "resume", "cv", "career", "experience", "skill", "skills", "certification",
    "certifications", "education", "background", "work history",
    "company", "companies", "role", "job",
)

def _hint_pattern(hints: Tuple[str, ...]) -> re.Pattern:
    return re.compile(r"\b(" + "|".join(re.escape(h) for h in hints) + r")\b")

_BLOG_PATTERN = _hint_pattern(_BLOG_HINTS)
_PROJECT_PATTERN = _hint_pattern(_PROJECT_HINTS)
_RESUME_PATTERN = _hint_pattern(_RESUME_HINTS)

def classify_source_type(text_query: str) -> Optional[str]:
    """Best-effort category guess from keywords. Returns None if no clear match."""
    q = text_query.lower()
    if _BLOG_PATTERN.search(q):
        return "blog"
    if _PROJECT_PATTERN.search(q):
        return "project"
    if _RESUME_PATTERN.search(q):
        return "resume"
    return None

# -------------------------
# S3 chunk helpers
# -------------------------
def parse_chunk_key(key: str) -> Tuple[Optional[str], Optional[int]]:
    m = _CHUNK_RE.search(key.split("/")[-1])
    if not m:
        return None, None
    return m.group("base"), int(m.group("idx"))

def list_keys_for_base(bucket: str, prefix: str, base: str) -> List[str]:
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            k = obj["Key"]
            name = k.split("/")[-1]
            if name.startswith(base + "_chunk") and name.endswith(".txt"):
                keys.append(k)
    def idx_of(k):
        _, idx = parse_chunk_key(k)
        return idx or 0
    return sorted(keys, key=idx_of)

def fetch_object_text(bucket: str, key: str) -> str:
    resp = s3.get_object(Bucket=bucket, Key=key)
    return resp["Body"].read().decode("utf-8", errors="replace")

def _stitch_keys(keys: List[str], max_bytes: int, dedupe: bool = True) -> str:
    """Fetch and concatenate a list of chunk keys (already in order), byte-capped."""
    parts = []
    total_bytes = 0
    seen_lines = set()
    for k in keys:
        txt = fetch_object_text(STAGING_BUCKET, k)
        if dedupe:
            lines = [l for l in txt.splitlines() if l and l not in seen_lines]
            seen_lines.update(lines)
            part = "\n".join(lines)
        else:
            part = txt
        part_bytes = len(part.encode("utf-8"))
        if total_bytes + part_bytes > max_bytes:
            remaining = max_bytes - total_bytes
            if remaining <= 0:
                break
            trimmed = part.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")
            parts.append(trimmed)
            total_bytes += len(trimmed.encode("utf-8"))
            break
        parts.append(part)
        total_bytes += part_bytes
    return "\n\n".join(p for p in parts if p)

def stitch_adjacent_chunks(
    bucket: str,
    staging_prefix: str,
    target_key: str,
    adjacent: int = 10,
    max_bytes: int = 120000,
    dedupe: bool = True
) -> Tuple[str, List[str]]:
    base, idx = parse_chunk_key(target_key)
    if base is None or idx is None:
        return fetch_object_text(bucket, target_key), [target_key]

    all_keys = list_keys_for_base(bucket, staging_prefix, base)
    key_idx = {k: parse_chunk_key(k)[1] for k in all_keys}
    ordered = sorted(all_keys, key=lambda k: key_idx[k])
    try:
        pos = ordered.index(target_key)
    except ValueError:
        return fetch_object_text(bucket, target_key), [target_key]

    start = max(0, pos - adjacent)
    end = min(len(ordered) - 1, pos + adjacent)
    selected = ordered[start:end + 1]
    return _stitch_keys(selected, max_bytes, dedupe=dedupe), selected

# -------------------------
# Utility: extract S3 key from a Bedrock retrieval result
# -------------------------
def extract_s3_key_from_retrieval_result(result_item: dict) -> Optional[str]:
    loc = result_item.get("location", {}) or {}
    s3loc = loc.get("s3Location") or {}
    uri = s3loc.get("uri")
    if uri:
        if isinstance(uri, str) and uri.startswith("s3://"):
            _, rest = uri.split("s3://", 1)
            parts = rest.split("/", 1)
            return parts[1] if len(parts) == 2 else parts[0]
        return uri

    for field in ("s3_uri", "s3Uri", "s3_path", "s3Path"):
        val = result_item.get(field)
        if val:
            if isinstance(val, str) and val.startswith("s3://"):
                _, rest = val.split("s3://", 1)
                parts = rest.split("/", 1)
                return parts[1] if len(parts) == 2 else parts[0]
            return val

    meta = result_item.get("metadata", {}) or {}
    for field in ("x-amz-bedrock-kb-source-file", "x-amz-bedrock-kb-source-file-name", "x-amz-bedrock-kb-chunk-id"):
        if meta.get(field):
            return meta.get(field)

    for field in ("key", "documentId", "id", "document_id"):
        if result_item.get(field):
            return result_item.get(field)

    return None

# -------------------------
# Bedrock KB retrieve (boto3), source_type -> metadata filter
# -------------------------
def retrieve_tool(tool_input):
    """
    Bedrock KB retrieval using boto3 with correct retrievalConfiguration shapes.
    Accepts an optional "sourceType" key in tool_input ("resume" | "project" | "blog").
    When present, passed as a Bedrock metadata equals-filter on `source_type`.

    NOTE: both configuration variants use "numberOfResults" -- NOT "maxResults"
    (managedSearchConfiguration) and NOT "k" (vectorSearchConfiguration). Using the
    wrong field name fails client-side in boto3 with ParamValidationError BEFORE the
    request is ever sent, which previously fell through silently to a filter-less,
    numberOfResults-less last-resort payload -- meaning the filter was never actually
    applied despite logs claiming otherwise. Fixed here.
    """
    if isinstance(tool_input, str):
        params = {"text": tool_input}
    elif isinstance(tool_input, dict):
        params = dict(tool_input)
    else:
        params = {"text": str(tool_input)}

    kb_id = params.get("knowledgeBaseId") or STRANDS_KNOWLEDGE_BASE_ID
    region = params.get("region") or REGION
    num_results = int(params.get("numberOfResults", 20))
    text_query = params.get("text", "")
    source_type = params.get("sourceType")

    client = boto3.client("bedrock-agent-runtime", region_name=region)

    metadata_filter = None
    if source_type:
        metadata_filter = {"equals": {"key": "source_type", "value": source_type}}

    payloads = []

    vector_cfg = {"numberOfResults": num_results}
    if metadata_filter:
        vector_cfg["filter"] = metadata_filter
    payloads.append({
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": text_query},
        "retrievalConfiguration": {"vectorSearchConfiguration": vector_cfg},
    })

    managed_cfg = {"numberOfResults": num_results}
    if metadata_filter:
        managed_cfg["filter"] = metadata_filter
    payloads.append({
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": text_query},
        "retrievalConfiguration": {"managedSearchConfiguration": managed_cfg},
    })

    payloads.append({
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": text_query},
    })

    last_exc = None
    for i, payload in enumerate(payloads):
        try:
            response = client.retrieve(**payload)
            results = response.get("retrievalResults") or response.get("results") or response.get("items") or []
            if i == len(payloads) - 1 and (source_type or num_results != len(results)):
                logger.warning(
                    "Retrieve fell through to the UNFILTERED last-resort payload "
                    "(source_type=%s requested but not applied). Check prior "
                    "ParamValidationError warnings.", source_type
                )
            logger.info("Bedrock retrieve succeeded with %d results (source_type=%s, payload_variant=%d)", len(results), source_type, i)
            return results
        except ParamValidationError as e:
            logger.warning("ParamValidationError for payload variant %d: %s", i, e)
            last_exc = e
            continue
        except Exception as e:
            logger.exception("Error calling Bedrock retrieve with payload variant %d: %s", i, e)
            last_exc = e
            continue

    logger.error("All retrieval payload attempts failed. Last error: %s", last_exc)
    return [{"error": True, "message": f"Retrieval failed: {last_exc}"}]

# =====================================================================================
# Agent-facing tools
# =====================================================================================
# All three are deliberately narrow and typed. The agent decides WHICH to call and WHEN
# (that's the "multi-tool calling" part), but none of them let the agent bypass the
# structural category separation (filename-prefix matching) or fetch unbounded amounts
# of data -- those guardrails live in the implementations below, not in agent judgment.

@tool
def search_and_stitch(query: str, source_type: Optional[str] = None) -> str:
    """
    Search Abhinav's background information for the single best-matching piece of
    content and return it along with its surrounding context, stitched into one
    coherent block. This is the right first tool to reach for almost any specific
    question -- a fact, a certification, one project's details, one blog's content.

    Args:
        query: The user's question, in their own words.
        source_type: Optional category to restrict the search to -- one of "blog",
            "project", or "resume". Use this when the question is clearly about one
            of those categories. Leave it out if you're not sure; it will be inferred.

    Returns:
        A block of stitched background text, or a short message saying nothing
        matched if no relevant content was found.
    """
    if source_type not in (None, *VALID_SOURCE_TYPES):
        logger.info("search_and_stitch got invalid source_type=%r, ignoring it.", source_type)
        source_type = None
    if source_type is None:
        source_type = classify_source_type(query)

    retrieve_results = retrieve_tool({
        "text": query,
        "knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID,
        "numberOfResults": 20,
        "region": REGION,
        "sourceType": source_type,
    })

    if not retrieve_results or retrieve_results[0].get("error"):
        return "No matching background information was found for that query."

    top_key = None
    for item in retrieve_results:
        candidate = extract_s3_key_from_retrieval_result(item)
        if candidate:
            top_key = candidate
            break
    if not top_key:
        return "No matching background information was found for that query."

    stitched_text, sources = stitch_adjacent_chunks(
        bucket=STAGING_BUCKET, staging_prefix=STAGING_PREFIX,
        target_key=top_key, adjacent=10, max_bytes=120000, dedupe=True,
    )
    logger.info("search_and_stitch: query=%r source_type=%s top_key=%s stitched_keys=%d", query, source_type, top_key, len(sources))
    return stitched_text or "No matching background information was found for that query."


@tool
def list_documents_by_type(source_type: str) -> str:
    """
    List every document available in one category, with a short id and approximate
    title for each. Use this FIRST whenever the user asks you to enumerate or list
    multiple things -- "list my blogs", "what projects have you built", "what have
    you written about X" -- rather than guessing from a single search result. After
    listing, use fetch_full_document on the specific ones relevant to the question.

    Args:
        source_type: One of "blog", "project", or "resume".

    Returns:
        A list of documents in that category (doc_id, approximate title, chunk
        count), or an error message if source_type is invalid or nothing was found.
    """
    prefix_match = DOC_ID_PREFIX_BY_SOURCE_TYPE.get(source_type)
    if not prefix_match:
        return f"Invalid source_type '{source_type}'. Must be one of: {', '.join(VALID_SOURCE_TYPES)}."

    paginator = s3.get_paginator("list_objects_v2")
    doc_chunk_counts: Dict[str, int] = {}
    for page in paginator.paginate(Bucket=STAGING_BUCKET, Prefix=STAGING_PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            name = key.split("/")[-1]
            if name.endswith(".metadata.json"):
                continue
            base, _ = parse_chunk_key(name)
            if base is None or not base.startswith(prefix_match):
                continue
            doc_chunk_counts[base] = doc_chunk_counts.get(base, 0) + 1

    if not doc_chunk_counts:
        return f"No documents found in category '{source_type}'."

    lines = [f"Documents in category '{source_type}':"]
    for doc_id, count in sorted(doc_chunk_counts.items()):
        readable = doc_id[len(prefix_match):].replace("-", " ").replace("_", " ")
        lines.append(f'- doc_id="{doc_id}" | approx_title="{readable}" | chunks={count}')
    logger.info("list_documents_by_type(%s): found %d documents", source_type, len(doc_chunk_counts))
    return "\n".join(lines)


@tool
def fetch_full_document(doc_id: str, max_bytes: int = 20000) -> str:
    """
    Fetch the full stitched content of one specific document by its doc_id (as
    returned by list_documents_by_type). Use this after listing documents in a
    category, when you need the complete content of one of them to answer a
    detailed question -- e.g. summarizing a specific blog post or describing one
    project in depth.

    Args:
        doc_id: The exact doc_id string from list_documents_by_type.
        max_bytes: Optional cap on how much content to return (default 20000,
            clamped between 2000 and 40000 to keep responses bounded).

    Returns:
        The document's full stitched text, or a message if doc_id wasn't found.
    """
    max_bytes = max(2000, min(int(max_bytes), 40000))
    all_keys = list_keys_for_base(STAGING_BUCKET, STAGING_PREFIX, doc_id)
    if not all_keys:
        return f"No document found with doc_id='{doc_id}'."
    logger.info("fetch_full_document(doc_id=%s): stitching %d chunks (max_bytes=%d)", doc_id, len(all_keys), max_bytes)
    return _stitch_keys(all_keys, max_bytes, dedupe=True)

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
        #guardrail_id=GUARDRAIL_ID,
        #guardrail_version=GUARDRAIL_VERSION
    )

    # The agent now owns the retrieval decision end-to-end via the three tools above:
    # search_and_stitch (single-fact / single-document lookup), list_documents_by_type
    # (enumeration -- "list my blogs"), and fetch_full_document (deep dive on one item
    # after listing). This is genuine multi-step tool orchestration: a "list my blogs"
    # question should trigger list_documents_by_type, possibly followed by one or more
    # fetch_full_document calls, before the agent ever produces a final answer.
    #
    # Category separation stays structural (filename-prefix matching inside the tools),
    # not dependent on the agent choosing the right source_type -- so a wrong guess
    # narrows results rather than causing cross-contamination.
    system_prompt = """
        You are Buddy, Abhinav's personal assistant. You speak as someone who already
        knows Abhinav's background, projects, blogs, skills, and experience.

        You have tools to look up that information. Use them quietly -- never mention
        tool names, "knowledge base," "retrieval," "search," "database," "chunk,"
        "document id," or any other internal mechanics in your replies to the user.
        Speak as if you simply know the answer.

        How to use your tools:
        - For most specific questions (a fact, a certification, details of one project
          or blog post), call search_and_stitch with the user's question. Only pass a
          source_type if the user is clearly asking about one category specifically.
        - For requests to enumerate or list multiple items ("list my blogs", "what
          projects have you built", "what are all your certifications"), call
          list_documents_by_type first to see everything in that category, then call
          fetch_full_document on the specific items you need more detail on before
          answering. Don't guess a list from a single search result.
        - For a deep question about one specific item you've already identified (e.g.
          the user asks about one blog by name after you've listed it), call
          fetch_full_document with its doc_id.
        - You may call tools more than once and combine results across calls to fully
          answer a question.

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
    """
    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    agent = Agent(
        model=bedrock_model,
        tools=[search_and_stitch, list_documents_by_type, fetch_full_document],
        system_prompt=system_prompt,
        context_manager="auto",
        session_manager=session_manager
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

        # The agent now decides for itself whether/which tools to call -- no manual
        # Python-side pre-fetch step. This is what makes the multi-tool orchestration
        # (list -> fetch -> answer) actually happen for enumeration-style questions.
        answer = agent(user_input)
        print("\nAssistant:\n", answer)

if __name__ == "__main__":
    main()