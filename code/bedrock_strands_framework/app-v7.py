import os
import json
import uuid
import logging
from collections import defaultdict
from datetime import date, datetime as dt
from typing import List, Dict, Any, Optional, Literal

import boto3
from dotenv import load_dotenv

from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands.tools.mcp import MCPClient
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.vended_plugins.context_offloader import ContextOffloader, FileStorage
from mcp.client.streamable_http import streamablehttp_client

load_dotenv(".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# GitMCP gives the agent a second, independent way to pull content directly
# from the source repo (github.com/{owner}/{repo}) via https://gitmcp.io.
# Its tools are documentation/code-SEARCH oriented (fetch_{repo}_documentation,
# search_{repo}_documentation, search_{repo}_code, fetch_generic_url_content) --
# not a generic "read this exact file path" API, and coverage of files nested
# deep in a non-standard layout (this repo's content lives under
# site/blog/posts/ and site/projects/project/, not a conventional /docs
# folder) isn't guaranteed the way it would be for a typical README-centric
# OSS repo. Treat search_knowledge_base as the primary, reliable source (see
# its own docstring) and these as a secondary capability for checking
# against the live repo. The resume is a PDF in this repo -- these tools
# fetch/search text-based documentation and are not expected to extract
# usable text from a binary PDF, so don't rely on them for resume facts.
GIT_REPO_OWNER = os.getenv("GIT_REPO_OWNER", "abhinavcloud")
GIT_REPO_NAME = os.getenv("GIT_REPO_NAME", "PersonalWebsite")
GITMCP_URL = f"https://gitmcp.io/{GIT_REPO_OWNER}/{GIT_REPO_NAME}"

REGION = os.getenv("REGION")
MODEL_ID = os.getenv("MODEL_ID")
STRANDS_KNOWLEDGE_BASE_ID = os.getenv("STRANDS_KNOWLEDGE_BASE_ID")

"""
=====================================================================
DESIGN NOTE -- why this file looks the way it does
=====================================================================
The previous version of this file decided everything in plain Python
BEFORE the agent ever ran: a keyword-hint classifier picked source_type,
another keyword-hint classifier decided if the question was about
"recency", the KB was queried unconditionally on every single turn (even
"hi" or "thanks"), and the model's only job was to reformat a pre-built
wall of text. That's manual RAG, not an agent -- the model never got to
decide anything, and Strands' actual value (a model-driven agent that
picks which tool to call, with what arguments, how many times, and in
what order) was completely unused.

This version exposes exactly ONE tool, `search_knowledge_base`, decorated
with @tool. Everything that was previously a hardcoded Python decision --
which source_type to search, whether the question is asking for the most
recent item, how many documents to pull back, whether to search again with
different parameters if the first attempt wasn't good enough -- is now a
parameter the MODEL sets when it calls the tool, guided by the tool's
docstring (which Strands uses to build the tool's spec/schema the model
sees). The model decides whether to call the tool at all, can call it
multiple times in a single turn with different parameters if it's not
satisfied, and only the model produces the final answer -- Strands' event
loop drives that whole call/observe/decide cycle natively.

What stays as plain Python (correctly, as implementation detail, not
policy): the actual boto3 Retrieve API call, its 100-results-per-call cap
and nextToken pagination, and the client-side date parsing needed to sort
by real published_date. Those are HOW the tool does its job, not WHAT the
model should decide -- the model shouldn't need to know Bedrock's
pagination mechanics to use the tool correctly.
=====================================================================
"""


def _parse_date_safe(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return dt.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _bedrock_retrieve(
    query: str,
    source_type: Optional[str],
    fetch_all: bool,
    num_results: int,
    max_pages: int = 20,
) -> List[Dict[str, Any]]:
    """
    Low-level Bedrock KB retrieval, with pagination. Not exposed to the
    agent directly -- called by the search_knowledge_base tool below.
    Bedrock's Retrieve API caps numberOfResults at 100 per call; fetch_all
    pages through nextToken until Bedrock reports no more results, instead
    of silently working from whatever fit in one 100-result page.
    """
    client = boto3.client("bedrock-agent-runtime", region_name=REGION)

    metadata_filter = None
    if source_type and source_type != "any":
        metadata_filter = {"equals": {"key": "source_type", "value": source_type}}

    per_page = 100 if fetch_all else num_results

    def build_payloads(n: int) -> List[Dict[str, Any]]:
        vector_cfg = {"numberOfResults": n}
        if metadata_filter:
            vector_cfg["filter"] = metadata_filter
        managed_cfg = {"numberOfResults": n}
        if metadata_filter:
            managed_cfg["filter"] = metadata_filter
        return [
            {"knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID, "retrievalQuery": {"text": query},
             "retrievalConfiguration": {"vectorSearchConfiguration": vector_cfg}},
            {"knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID, "retrievalQuery": {"text": query},
             "retrievalConfiguration": {"managedSearchConfiguration": managed_cfg}},
            {"knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID, "retrievalQuery": {"text": query}},
        ]

    payloads = build_payloads(per_page)
    working_variant: Optional[int] = None
    all_results: List[Dict[str, Any]] = []
    next_token: Optional[str] = None
    last_exc = None

    for i, payload in enumerate(payloads):
        try:
            response = client.retrieve(**payload)
            results = response.get("retrievalResults") or response.get("results") or response.get("items") or []
            logger.info("Retrieve succeeded with %d results (source_type=%s, variant=%d, fetch_all=%s)",
                        len(results), source_type, i, fetch_all)
            all_results.extend(results)
            next_token = response.get("nextToken")
            working_variant = i
            break
        except Exception as e:
            last_exc = e
            logger.warning("Retrieve payload variant %d failed: %s", i, e)
            continue

    if working_variant is None:
        logger.error("All retrieve payloads failed: %s", last_exc)
        return []

    if fetch_all:
        pages_fetched = 1
        while next_token and pages_fetched < max_pages:
            page_payload = dict(payloads[working_variant])
            page_payload["nextToken"] = next_token
            try:
                response = client.retrieve(**page_payload)
            except Exception as e:
                logger.warning("Pagination failed on page %d, stopping with partial results: %s", pages_fetched + 1, e)
                break
            page_results = response.get("retrievalResults") or response.get("results") or response.get("items") or []
            all_results.extend(page_results)
            next_token = response.get("nextToken")
            pages_fetched += 1
        logger.info("fetch_all complete: %d total results across %d page(s) (source_type=%s)",
                    len(all_results), pages_fetched, source_type)

    return all_results


def _chunk_index_int(meta: Dict[str, Any]) -> int:
    """chunk_index is stored as a STRING in metadata ("chunk_index": str(i)).
    Comparing/sorting/min/max on strings is lexicographic, not numeric --
    "10" < "2" as strings -- which silently scrambles chunk ordering when
    stitching full text back together, and produces nonsensical citation
    ranges like "chunks 10-9". Always compare as int."""
    try:
        return int(meta.get("chunk_index", 0))
    except (TypeError, ValueError):
        return 0


def _stitch_docs(
    results: List[Dict[str, Any]],
    max_docs: int,
    sort_by_recency: bool,
) -> List[Dict[str, Any]]:
    """Groups chunks by doc_id, joins them in chunk order, dedupes to one entry per document."""
    docs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in results:
        meta = item.get("metadata", {}) or {}
        doc_id = meta.get("doc_id")
        if not doc_id:
            continue
        docs[doc_id].append(item)

    stitched: List[Dict[str, Any]] = []
    for doc_id, chunks in docs.items():
        chunks.sort(key=lambda x: _chunk_index_int(x.get("metadata", {}) or {}))
        texts, indices = [], []
        source_type = title = published_date = None
        for c in chunks:
            raw = c.get("content") or c.get("text") or ""
            if isinstance(raw, dict):
                raw = raw.get("text") or (json.dumps(raw["json"]) if "json" in raw else str(raw))
            texts.append(raw)
            m = c.get("metadata", {}) or {}
            indices.append(_chunk_index_int(m))
            source_type = source_type or m.get("source_type")
            title = title or m.get("title")
            published_date = published_date or m.get("published_date")
        stitched.append({
            "doc_id": doc_id, "source_type": source_type, "title": title,
            "published_date": published_date, "text": "\n\n".join(texts), "chunk_indices": indices,
        })

    if sort_by_recency:
        stitched.sort(key=lambda d: _parse_date_safe(d.get("published_date")) or date.min, reverse=True)
    else:
        stitched.sort(key=lambda d: d["doc_id"])

    return stitched[:max_docs]


def _format_docs_for_model(stitched_docs: List[Dict[str, Any]], sort_by_recency: bool) -> str:
    """
    Always returns full document text, deliberately. Strands' own Context
    Offloader plugin already handles oversized tool results correctly --
    it stores the full result externally, shows the agent a preview plus
    a reference, and auto-registers retrieve_offloaded_content for the
    agent to fetch the complete data when the preview isn't enough.
    Artificially shrinking results here to dodge that would just be
    redundant orchestration fighting a mechanism that already works (see
    the system prompt for the instruction that actually fixes the real
    gap: the model needs to be told to USE retrieve_offloaded_content
    before answering from an incomplete preview, not have this tool
    pre-emptively hide data from it).
    """
    if not stitched_docs:
        return "No matching documents were found in the knowledge base for this search. Try a broader query, a different source_type, or ask the user for clarification."

    order_note = " (sorted most-recent-first by actual published date -- trust this order)" if sort_by_recency else ""
    blocks = [f"Found {len(stitched_docs)} matching document(s){order_note}:\n"]
    for d in stitched_docs:
        indices = d["chunk_indices"]
        citation = f"{d['doc_id']}, chunks {min(indices)}-{max(indices)}" if indices else d["doc_id"]
        blocks.append(
            f'=== "{d.get("title") or d["doc_id"]}" ===\n'
            f'Source type: {d.get("source_type") or "unknown"}\n'
            f'Published: {d.get("published_date") or "date unknown"}\n'
            f'Reference: {citation}\n\n'
            f'{d["text"]}\n'
        )
    return "\n".join(blocks)


@tool
def search_knowledge_base(
    query: str,
    source_type: Literal["blog", "project", "resume", "any"] = "any",
    sort_by_recency: bool = False,
    max_documents: int = 5,
) -> str:
    """
    Search Abhinav's personal knowledge base (his resume/career history,
    blog posts, and projects) and return matching documents with their
    real title, published date, source type, and full text.

    Call this whenever you need specific facts about Abhinav that you
    don't already have in the conversation -- his companies, roles, dates,
    skills, blog posts, or projects. Don't call it for greetings, thanks,
    or general chit-chat that doesn't need any specific fact about him.
    You may call this tool more than once in the same turn with different
    parameters if your first search didn't give you what you needed (for
    example: broaden source_type from "blog" to "any", or retry with
    sort_by_recency=True if the user actually wanted the newest item and
    your first search returned semantically-relevant-but-not-newest
    results).

    Args:
        query: The search text. Use the user's own question, or a more
            specific rephrasing of it if that would search better.
        source_type: Restrict the search to one category, or "any" to
            search everything. Use "resume" for questions about companies,
            job roles, career history, employment dates, skills, or
            education. Use "blog" for questions about blog posts or
            articles he's written. Use "project" for questions about
            things he's built. Use "any" when the question could span
            categories or you're unsure.
        sort_by_recency: Set this to True whenever the user is asking for
            the most recent, latest, newest, or "last" item(s) -- for
            example "what's his most recent blog" or "what's he working on
            lately". When True, this searches the FULL set of matching
            documents and returns them sorted by their real published
            date, newest first. Leave this False for ordinary topical
            questions, where documents most semantically relevant to the
            query should come first instead -- semantic relevance and
            recency are different things, and setting this incorrectly
            will give a wrong answer to either kind of question.
        max_documents: How many distinct documents to return. Raise this
            (e.g. to 10-20) if the user wants a broader list, like "list
            all his blogs about Kubernetes" or a full career history.

    Returns:
        A formatted block listing each matching document's title,
        published date, source type, a reference ID for citation, and its
        full text -- or a message saying nothing matched if the search
        found nothing.
    """
    fetch_all = sort_by_recency or source_type == "resume"
    results = _bedrock_retrieve(
        query=query,
        source_type=None if source_type == "any" else source_type,
        fetch_all=fetch_all,
        num_results=100 if fetch_all else 20,
    )
    if not results:
        return "The knowledge base search failed or returned nothing -- there may be a connectivity issue, or truly nothing matches this query."

    stitched = _stitch_docs(results, max_docs=max_documents, sort_by_recency=sort_by_recency)
    return _format_docs_for_model(stitched, sort_by_recency=sort_by_recency)


def my_agent(tools: List[Any]) -> Agent:
    bedrock_model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
    )

    system_prompt = f"""
You are Buddy, Abhinav's personal assistant. You speak as someone who already
knows Abhinav's background, projects, blogs, skills, and experience -- but you
look things up with your tools rather than guessing, whenever a question needs
a specific fact you don't already have in this conversation.

You have two different ways to look things up:

1. search_knowledge_base -- your PRIMARY and most reliable tool. It searches an
   already-indexed, cleaned copy of Abhinav's resume, blog posts, and projects,
   with accurate titles and publish dates attached. Use this first for almost
   everything.

2. GitMCP tools for the repo {GIT_REPO_OWNER}/{GIT_REPO_NAME} (fetch/search
   documentation, search code) -- a SECONDARY capability that reads directly
   from the live GitHub repository behind Abhinav's site. Useful if you want to
   double-check something against the actual source, or if search_knowledge_base
   didn't have what you needed. Two important limits: these tools are
   documentation/code-search oriented, not guaranteed to find every file in a
   non-standard folder layout -- if a search comes back empty, that doesn't
   necessarily mean the content doesn't exist, just that this tool didn't find
   it, so fall back to search_knowledge_base or say you're not sure. Also,
   Abhinav's resume in this repo is a PDF file -- these tools read text-based
   documentation and are not expected to extract usable text from it, so never
   rely on them for resume/career facts; use search_knowledge_base for those.

How to decide what to do:
- Decide for yourself whether a question needs a tool at all. Skip tools for
  greetings, thanks, or general chat.
- Start with search_knowledge_base for anything about Abhinav's background, blog
  posts, or projects, choosing its source_type and sort_by_recency parameters
  based on what's actually being asked.
- If a result is incomplete, or the user explicitly asks you to check the
  source/repo directly, use the GitMCP tools as a follow-up -- not as your
  default first move.
- If your first search doesn't give you what you need, search again with
  different parameters or a different tool rather than answering from a weak
  or incomplete result.

Answering rules, once you have results:
- Never mention knowledge bases, retrieval, chunks, tools, repos, or metadata to
  the user -- speak as though you simply already know this about Abhinav.
- Use the exact title given for a blog post or project. Never invent or guess a
  title -- if none is given, use its reference ID as-is instead of making one up.
- If a tool result comes back offloaded -- you'll see a note like "[Offloaded:
  N blocks, ~X tokens]" or "[Full content offloaded to storage - reference:
  <id>]" -- you MUST call retrieve_offloaded_content in the SAME turn before
  writing your final answer. Never answer from the preview alone, and never
  ask the user for permission first -- just call it, the way you would call
  any other tool. Use it like this:
    - reference: the exact reference ID shown in the offloaded note (required).
    - For a targeted question (e.g. "what companies did he work at"), pass
      pattern with a relevant keyword (e.g. pattern="Company" or a term you
      expect to appear near what you need) to search within the content
      instead of pulling all of it back.
    - For "list everything" style questions where you need the whole thing,
      omit pattern and line_range to retrieve the full content.
  This matters especially for list-style answers ("top 5 blogs", "all his
  roles", "companies he worked at"): a preview showing only the first item is
  not the same as having all of them, and answering from an incomplete
  preview -- or worse, inventing a placeholder like "company not specified"
  instead of actually looking -- is never acceptable.
- Trust the published date given for each document. For "most recent" / "latest"
  questions, trust the order the tool already sorted results in when
  sort_by_recency was used -- don't re-order by content or guess recency yourself.
- When citing a specific resume, blog, or project detail, include a brief citation
  in parentheses using the reference ID, e.g. (source: rag_guardrails, chunks 1-3).
- For resume/career questions, list EVERY distinct role you find, including
  short-duration or junior roles, or roles that sit awkwardly between more senior
  ones. Do not quietly drop an entry because it seems minor or interrupts a
  cleaner-looking career narrative -- list everything you found first, then
  organize it the way the user asked (chronologically, grouped by company, etc.).
- If something genuinely isn't in your results, say so plainly rather than
  inventing details, dates, companies, or content.
"""

    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    # context_manager="auto" implicitly composes a ContextOffloader backed by
    # InMemoryStorage, which the Strands docs explicitly warn against when a
    # session_manager is also in use: in-memory offload entries don't persist
    # across process restarts, evict after 20 idle agent-loop cycles, and
    # retrieving an evicted reference raises an error -- exactly the kind of
    # thing that would make retrieve_offloaded_content behave inconsistently
    # in a session-persisted, multi-turn chatbot. Using FileStorage here
    # instead makes offloaded content durable on disk, matching how
    # FileSessionManager already persists everything else. Thresholds are
    # raised a bit above "auto"'s defaults (1500/750 tokens) since our
    # documents are legitimately a few KB each and don't need to be offloaded
    # quite that aggressively.
    context_offloader = ContextOffloader(
        storage=FileStorage("./context_offload_artifacts"),
        max_result_tokens=3000,
        preview_tokens=1200,
    )
    conversation_manager = SlidingWindowConversationManager(window_size=30)

    agent = Agent(
        model=bedrock_model,
        tools=tools,
        system_prompt=system_prompt,
        plugins=[context_offloader],
        conversation_manager=conversation_manager,
        session_manager=session_manager,
    )
    return agent


def _run_chat_loop(agent: Agent):
    print("I am ready. Ask about Abhinav, his profile, work, blogs, projects, or topics.")
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye")
            break
        # No pre-decided source_type, no pre-decided recency intent, no
        # unconditional KB call, no manually-built prompt -- the agent
        # decides all of that itself via its tools.
        answer = agent(user_input)
        print("\nAssistant:\n", answer)


def main():
    """
    GitMCP's tools are only usable while its MCPClient connection is open --
    unlike the plain in-process search_knowledge_base tool, they're a live
    remote connection, not a static function. That connection needs to stay
    open for the WHOLE chat session (every turn may call it), not just for
    one call the way the sample snippet's `with:` block does -- so the
    entire interactive loop runs inside the `with repo_mcp_client:` block
    below, not just the agent construction.

    If GitMCP is unreachable (network issue, service down, wrong repo),
    that shouldn't take down the whole chatbot -- fall back to
    search_knowledge_base alone and keep going.
    """
    repo_mcp_client = MCPClient(lambda: streamablehttp_client(GITMCP_URL))

    try:
        with repo_mcp_client:
            git_tools = repo_mcp_client.list_tools_sync()
            tool_names = [getattr(t, "tool_name", None) or getattr(t, "name", None) or repr(t) for t in git_tools]
            logger.info("Connected to GitMCP (%s) with %d tool(s): %s", GITMCP_URL, len(git_tools), tool_names)
            agent = my_agent(tools=[search_knowledge_base] + list(git_tools))
            _run_chat_loop(agent)
    except Exception as e:
        logger.warning("Could not connect to GitMCP (%s): %s -- continuing with search_knowledge_base only.", GITMCP_URL, e)
        agent = my_agent(tools=[search_knowledge_base])
        _run_chat_loop(agent)


if __name__ == "__main__":
    main()