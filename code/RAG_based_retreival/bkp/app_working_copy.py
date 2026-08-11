# app.py
import os
import re
import json
import logging
from typing import List, Tuple, Optional
from pathlib import Path
import uuid

import boto3
import botocore
from botocore.exceptions import ParamValidationError
from dotenv import load_dotenv

# Strands imports
from strands import Agent
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager

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
# Intent classification -> source_type filter
# -------------------------
# This is the single source of truth for routing a question to the right KB slice.
# It mirrors SOURCE_TYPE_BY_PREFIX in the ingestion app.py -- keep the values
# ("resume" / "project" / "blog") in sync between the two.
_BLOG_HINTS = ("blog", "blogs", "post", "posts", "article", "wrote about", "write up", "wrote a")
_PROJECT_HINTS = ("project", "projects", "built", "portfolio", "side project", "repo", "github")
_RESUME_HINTS = (
    "resume", "cv", "career", "experience", "skill", "skills", "certification",
    "certifications", "education", "abhinav", "background", "work history",
    "company", "companies", "role", "job",
)

def classify_source_type(text_query: str) -> Optional[str]:
    """
    Classifies a user question into a source_type ("blog" | "project" | "resume") based
    on keyword hints, so we can pass a server-side metadata filter to Bedrock retrieve
    instead of relying on vector similarity (which puts resume and project chunks close
    together in embedding space) plus prompt instructions to keep them apart.

    Returns None when the query doesn't clearly map to one type -- in that case we
    retrieve unfiltered across all source types (e.g. "tell me about Abhinav" could
    reasonably touch resume + projects + blogs).
    """
    q = text_query.lower()
    if any(h in q for h in _BLOG_HINTS):
        return "blog"
    if any(h in q for h in _PROJECT_HINTS):
        return "project"
    if any(h in q for h in _RESUME_HINTS):
        return "resume"
    return None

# -------------------------
# Stitch helper
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

def stitch_adjacent_chunks(
    bucket: str,
    staging_prefix: str,
    target_key: str,
    adjacent: int = 2,
    max_bytes: int = 3000,
    dedupe: bool = True
) -> Tuple[str, List[str]]:
    base, idx = parse_chunk_key(target_key)
    if base is None or idx is None:
        txt = fetch_object_text(bucket, target_key)
        return txt, [target_key]

    all_keys = list_keys_for_base(bucket, staging_prefix, base)
    key_idx = {k: parse_chunk_key(k)[1] for k in all_keys}
    ordered = sorted(all_keys, key=lambda k: key_idx[k])
    try:
        pos = ordered.index(target_key)
    except ValueError:
        txt = fetch_object_text(bucket, target_key)
        return txt, [target_key]

    start = max(0, pos - adjacent)
    end = min(len(ordered) - 1, pos + adjacent)
    selected = ordered[start:end+1]

    stitched_parts = []
    total_bytes = 0
    seen_lines = set()
    for k in selected:
        txt = fetch_object_text(bucket, k)
        if dedupe:
            lines = []
            for line in txt.splitlines():
                if line and line not in seen_lines:
                    lines.append(line)
                    seen_lines.add(line)
            part = "\n".join(lines)
        else:
            part = txt
        part_bytes = len(part.encode("utf-8"))
        if total_bytes + part_bytes > max_bytes:
            remaining = max_bytes - total_bytes
            if remaining <= 0:
                break
            b = part.encode("utf-8")[:remaining]
            try:
                trimmed = b.decode("utf-8")
            except UnicodeDecodeError:
                trimmed = b.decode("utf-8", errors="ignore")
            stitched_parts.append(trimmed)
            total_bytes += len(trimmed.encode("utf-8"))
            break
        stitched_parts.append(part)
        total_bytes += part_bytes

    stitched = "\n\n".join(p for p in stitched_parts if p)
    return stitched, selected

def retrieve_then_stitch_and_ask(
    agent,
    user_question: str,
    number_of_results: int = 20,
    adjacent: int = 10,
    max_bytes: int = 120000
    ) -> str:
    """
    Single deterministic retrieval path (this is the ONLY retrieval that runs -- the
    agent is no longer given its own copy of the retrieve tool, so there's no second,
    unfiltered, unstitched retrieval competing with this one).

    1. Classify the question's source_type and pass it as a server-side metadata
       filter to Bedrock retrieve, so resume/project/blog chunks can't cross-contaminate
       regardless of embedding similarity.
    2. Take the top result (now safe to trust -- everything Bedrock returned already
       matches the filter, there's no LLM-side post-filtering to do).
    3. Stitch its neighboring chunks (same doc_id) into one coherent context block.
    4. Hand that stitched context to the agent as part of the prompt.
    """
    source_type = classify_source_type(user_question)
    retrieve_params = {
        "text": user_question,
        "knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID,
        "numberOfResults": number_of_results,
        "region": REGION,
        "sourceType": source_type,  # None means "no filter, search everything"
    }

    logger.info(
        "Calling retrieve with params: %s",
        {k: retrieve_params[k] for k in ("knowledgeBaseId", "numberOfResults", "region", "sourceType")},
    )
    try:
        retrieve_results = retrieve_tool(retrieve_params)
    except Exception as e:
        logger.exception("Exception calling retrieve_tool: %s", e)
        return f"Retrieval failed with exception: {e}"

    if not retrieve_results or retrieve_results[0].get("error"):
        logger.info("retrieve returned no usable results (source_type=%s); calling agent without KB context.", source_type)
        return agent(user_question)

    try:
        logger.debug("retrieve_results[0]: %s", json.dumps(retrieve_results[0], indent=2)[:2000])
    except Exception:
        logger.debug("retrieve_results[0] (non-serializable)")

    top_key = None
    for item in retrieve_results:
        candidate = extract_s3_key_from_retrieval_result(item)
        if candidate:
            top_key = candidate
            break

    if not top_key:
        logger.info("No S3 key extracted from retrieval results; calling agent without KB context.")
        return agent(user_question)

    logger.info("Top S3 key extracted (source_type=%s): %s", source_type, top_key)

    stitched_text, sources = stitch_adjacent_chunks(
        bucket=STAGING_BUCKET,
        staging_prefix=STAGING_PREFIX,
        target_key=top_key,
        adjacent=adjacent,
        max_bytes=max_bytes,
        dedupe=True
    )

    logger.info("Stitched %d source keys; preview length=%d", len(sources), len(stitched_text))
    logger.debug("Stitched keys: %s", json.dumps(sources, indent=2)[:2000])
    logger.debug("Stitched preview: %s", stitched_text[:2000])

    # Deliberately neutral header -- no "KB"/"source_type"/"retrieval" wording here,
    # since the model reads this text directly and could otherwise echo that framing
    # back to the user (e.g. "based on the knowledge base..."). source_type is only
    # logged above for debugging, never included in what the model sees.
    prompt_with_context = f"Background information you already know:\n{stitched_text}\n\nUser question: {user_question}"
    return agent(prompt_with_context)

# -------------------------
# Utility: extract S3 key
# -------------------------
def extract_s3_key_from_retrieval_result(result_item: dict) -> Optional[str]:
    """
    Handles Bedrock agent runtime shape: location -> s3Location -> uri,
    plus older alternate fields and metadata fallbacks.
    Returns S3 key relative to bucket (e.g., 'bedrock-clean/resume_chunk1.txt')
    or other id string the stitcher can use.
    """
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


def retrieve_tool(tool_input):
    """
    Bedrock KB retrieval using boto3 with correct retrievalConfiguration shapes.
    Accepts an optional "sourceType" key in tool_input ("resume" | "project" | "blog").
    When present, it's passed as a Bedrock metadata equals-filter on the `source_type`
    attribute written by the ingestion pipeline's metadata sidecars -- this is what
    actually prevents resume chunks from being returned for project/blog questions
    (server-side, deterministic), rather than relying on the model to notice and
    discard mismatched chunks after the fact.

    Tries managedSearchConfiguration first, then vectorSearchConfiguration, then a
    filter-less payload as a last resort (e.g. if the KB has no metadata attributes
    indexed yet, so filtered payloads would 400).
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
    source_type = params.get("sourceType")  # None => no filter

    client = boto3.client("bedrock-agent-runtime", region_name=region)

    metadata_filter = None
    if source_type:
        metadata_filter = {"equals": {"key": "source_type", "value": source_type}}

    # NOTE: the retrieval API shape differs by knowledge base type. Standard vector
    # knowledge bases should use vectorSearchConfiguration; managed knowledge bases may
    # support managedSearchConfiguration. We prefer the vector shape first so the
    # common case succeeds without emitting the validation error you saw in the logs,
    # while still preserving the source_type metadata filter and the existing fallback
    # behavior if a managed KB is used.
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

    # Last resort: no filter, no explicit numberOfResults. If execution reaches this
    # payload, log it loudly -- it means the filter was silently dropped, which is
    # exactly the bug that caused resume/project/blog cross-contamination before.
    payloads.append({
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": text_query},
    })

    last_exc = None
    for i, payload in enumerate(payloads):
        try:
            logger.debug("Calling Bedrock retrieve with payload keys: %s", list(payload.keys()))
            response = client.retrieve(**payload)
            results = response.get("retrievalResults") or response.get("results") or response.get("items") or []
            if i == len(payloads) - 1 and (source_type or num_results != len(results)):
                logger.warning(
                    "Retrieve fell through to the UNFILTERED last-resort payload "
                    "(source_type=%s requested but not applied, numberOfResults not sent). "
                    "Both filtered payload variants failed -- check the ParamValidationError "
                    "warnings above.", source_type
                )
            logger.info("Bedrock retrieve succeeded with %d results (source_type filter=%s, payload_variant=%d)", len(results), source_type, i)
            logger.debug("Top retrieval result preview: %s", json.dumps(results[0], indent=2)[:2000] if results else "none")
            return results
        except ParamValidationError as e:
            logger.warning("ParamValidationError for payload variant %d: %s", i, e)
            last_exc = e
            continue
        except Exception as e:
            message = str(e)
            if "managedSearchConfiguration" in message or "vectorSearchConfiguration" in message:
                logger.info("Bedrock rejected payload variant %d: %s", i, e)
            else:
                logger.exception("Error calling Bedrock retrieve with payload variant %d: %s", i, e)
            last_exc = e
            continue

    logger.error("All retrieval payload attempts failed. Last error: %s", last_exc)
    return [{"error": True, "message": f"Retrieval failed: {last_exc}"}]

retrieve_tool.__name__ = "retrieve"

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

    # NOTE: this system prompt intentionally does NOT instruct the model to call a
    # retrieve tool or post-filter raw results. Retrieval, filtering (by source_type,
    # server-side), and stitching all happen in retrieve_then_stitch_and_ask() BEFORE
    # the agent is ever called -- the agent only ever sees pre-filtered, pre-stitched
    # context in its prompt. Giving the agent its own copy of the retrieve tool here
    # would let it launch a second, unfiltered, unstitched retrieval on top of that,
    # which is what was previously causing nondeterministic resume/project bleed --
    # so `tools=[]` is deliberate, not an oversight.
    #
    # This prompt also deliberately never mentions "KB", "source_type", "context block",
    # "retrieval", or filtering -- the user should be able to say "list my blogs" or
    # "tell me about my projects" and just get the answer, the same way a person would
    # answer from what they already know. The retrieval machinery is plumbing, not
    # something the assistant should narrate, ask the user to specify, or expose.
    system_prompt = """
        You are Buddy, Abhinav's personal assistant. You speak as someone who already
        knows Abhinav's background, projects, blogs, skills, and experience -- not as a
        system that looks things up on request.

        Above the user's question you will sometimes see a block of background
        information. Treat it as things you already know. Answer naturally and directly
        from it, in your own words -- do not mention where it came from, how it was
        found, what category it belongs to, or that you were "given context." Never use
        words like "knowledge base," "retrieval," "source," "database," or "chunk" in
        your replies.

        Examples of how to handle common requests:
        - "List my blogs" / "what have I written about" -> just list the blog titles/topics.
        - "Tell me about my projects" / "what have I built" -> describe the projects directly.
        - "What are my skills" / "tell me about my experience" -> answer directly, as
          Abhinav's assistant who knows this already.
        Never ask the user to tell you which category, file, or section to look in --
        figure out what they're asking about from the question itself.

        If the background information provided doesn't actually cover what the user
        asked, say plainly that you don't have that detail yet, and ask if they'd like
        you to look more broadly -- without describing the mechanics of why (no mention
        of filters, categories, or search scope).

        Stay grounded in the information you were actually given. Do not invent project
        details, dates, companies, or blog content that isn't present in it.
    """
    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    agent = Agent(
        model=bedrock_model,
        tools=[],  # retrieval happens explicitly in retrieve_then_stitch_and_ask,
                   # not as an agent-invoked tool -- see note above.
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

        answer = retrieve_then_stitch_and_ask(agent, user_input, number_of_results=20, adjacent=10, max_bytes=120000)
        print("\nAssistant:\n", answer)

if __name__ == "__main__":
    main()