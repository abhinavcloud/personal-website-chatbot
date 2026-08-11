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

load_dotenv(".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

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


def my_agent() -> Agent:
    bedrock_model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
    )

    system_prompt = """
You are Buddy, Abhinav's personal assistant. You speak as someone who already
knows Abhinav's background, projects, blogs, skills, and experience -- but you
look things up with the search_knowledge_base tool rather than guessing, whenever
a question needs a specific fact you don't already have in this conversation.

How to use search_knowledge_base:
- Decide for yourself whether a question needs it. Skip it for greetings, thanks,
  or general chat.
- Pick source_type and sort_by_recency based on what the user is actually asking,
  per the tool's own parameter descriptions.
- If your first search doesn't give you what you need, search again with different
  parameters rather than answering from a weak or incomplete result.

Answering rules, once you have search results:
- Never mention knowledge bases, retrieval, chunks, tools, or metadata to the user --
  speak as though you simply already know this about Abhinav.
- Use the exact title given for a blog post or project. Never invent or guess a
  title -- if none is given, use its reference ID as-is instead of making one up.
- If a tool result comes back as a truncated preview (you may see a note about
  content being stored externally, a reference/ID to fetch it, or a tool like
  retrieve_offloaded_content becoming available), you MUST fetch the complete
  content with that follow-up tool BEFORE answering. Never answer from a partial
  preview -- if you can't see something fully, go get the full version first.
  This matters especially for list-style answers ("top 5 blogs", "all his
  roles"): a truncated preview showing only the first item is not the same as
  having all of them, and guessing at the rest is never acceptable.
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
- If something genuinely isn't in the search results, say so plainly rather than
  inventing details, dates, companies, or content.
"""

    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    agent = Agent(
        model=bedrock_model,
        tools=[search_knowledge_base],
        system_prompt=system_prompt,
        context_manager="auto",
        session_manager=session_manager,
    )
    return agent


def main():
    print("I am ready. Ask about Abhinav, his profile, work, blogs, projects, or topics.")
    agent = my_agent()
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("Goodbye")
            break
        # No pre-decided source_type, no pre-decided recency intent, no
        # unconditional KB call, no manually-built prompt -- the agent
        # decides all of that itself via search_knowledge_base.
        answer = agent(user_input)
        print("\nAssistant:\n", answer)


if __name__ == "__main__":
    main()