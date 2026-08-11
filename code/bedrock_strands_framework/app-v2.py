import os
import json
import uuid
import logging
from collections import defaultdict
from datetime import date, datetime as dt
from typing import List, Dict, Any, Optional

import boto3
from dotenv import load_dotenv

from strands import Agent
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager

load_dotenv(".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

REGION = os.getenv("REGION")
MODEL_ID = os.getenv("MODEL_ID")
STRANDS_KNOWLEDGE_BASE_ID = os.getenv("STRANDS_KNOWLEDGE_BASE_ID")

_BLOG_HINTS = ("blog", "blogs", "post", "posts", "article", "wrote about", "write up", "wrote a")
_PROJECT_HINTS = ("project", "projects", "built", "portfolio", "side project", "repo", "github")
_RESUME_HINTS = (
    "resume", "cv", "career", "experience", "skill", "skills", "certification",
    "certifications", "education", "abhinav", "background", "work history",
    "company", "companies", "role", "job",
)
# Intent hints for "sort by real recency" -- distinct from source-type
# classification. When these match, sorting/filtering must be done
# deterministically on the stored published_date metadata, not left to
# vector-similarity ranking (which ranks by semantic closeness to the query
# text, not by date) or to the LLM (which has no reliable notion of "today"
# or real publish dates unless it's handed them explicitly).
_RECENCY_HINTS = (
    "most recent", "recent", "recently", "latest", "newest", "new",
    "last blog", "last post", "just wrote", "just published",
    "most current", "up to date", "up-to-date",
)


def classify_source_type(text_query: str) -> Optional[str]:
    q = text_query.lower()
    if any(h in q for h in _BLOG_HINTS):
        return "blog"
    if any(h in q for h in _PROJECT_HINTS):
        return "project"
    if any(h in q for h in _RESUME_HINTS):
        return "resume"
    return None


def detect_recency_intent(text_query: str) -> bool:
    q = text_query.lower()
    return any(h in q for h in _RECENCY_HINTS)


def _parse_date_safe(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return dt.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def retrieve_tool(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    kb_id = params.get("knowledgeBaseId", STRANDS_KNOWLEDGE_BASE_ID)
    region = params.get("region", REGION)
    num_results = int(params.get("numberOfResults", 50))
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
            logger.info("Retrieve succeeded with %d results (source_type=%s, variant=%d)", len(results), source_type, i)
            return results
        except Exception as e:
            last_exc = e
            logger.warning("Retrieve payload variant %d failed: %s", i, e)
            continue

    logger.error("All retrieve payloads failed: %s", last_exc)
    return [{"error": True, "message": str(last_exc)}]


def stitch_docs_from_results(
    results: List[Dict[str, Any]],
    max_docs: int = 10,
    sort_by_recency: bool = False,
) -> List[Dict[str, Any]]:
    docs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in results:
        meta = item.get("metadata", {}) or {}
        doc_id = meta.get("doc_id")
        if not doc_id:
            continue
        docs[doc_id].append(item)

    stitched: List[Dict[str, Any]] = []
    for doc_id, chunks in docs.items():
        chunks.sort(key=lambda x: x.get("metadata", {}).get("chunk_index", 0))
        texts = []
        indices = []
        source_type = None
        title = None
        published_date = None
        for c in chunks:
            raw = c.get("content") or c.get("text") or ""
            if isinstance(raw, dict):
                if "text" in raw:
                    raw = raw["text"]
                elif "json" in raw:
                    raw = json.dumps(raw["json"])
                else:
                    raw = str(raw)

            texts.append(raw)
            m = c.get("metadata", {}) or {}
            indices.append(m.get("chunk_index", 0))
            if source_type is None:
                source_type = m.get("source_type")
            # title/published_date are the same across every chunk of a
            # document (written once per document at ingestion time), so
            # grab them from whichever chunk has them first.
            if title is None and m.get("title"):
                title = m.get("title")
            if published_date is None and m.get("published_date"):
                published_date = m.get("published_date")
        stitched.append({
            "doc_id": doc_id,
            "source_type": source_type,
            "title": title,
            "published_date": published_date,
            "text": "\n\n".join(texts),
            "chunk_indices": indices,
        })

    if sort_by_recency:
        # Real recency, from stored metadata -- not vector-similarity rank
        # and not left for the LLM to infer. Docs with no parseable date
        # sort last rather than being dropped.
        stitched.sort(key=lambda d: _parse_date_safe(d.get("published_date")) or date.min, reverse=True)
    else:
        stitched.sort(key=lambda d: d["doc_id"])

    return stitched[:max_docs]


def build_grounded_prompt(
    user_question: str,
    stitched_docs: List[Dict[str, Any]],
    sorted_by_recency: bool = False,
) -> str:
    blocks = []
    for d in stitched_docs:
        doc_id = d["doc_id"]
        source_type = d["source_type"]
        title = d.get("title") or doc_id
        published_date = d.get("published_date") or "date unknown"
        indices = d["chunk_indices"]
        citation = f"{doc_id}, chunks {min(indices)}–{max(indices)}" if indices else doc_id
        header = f"[{source_type or 'unknown'} | \"{title}\" | published: {published_date} | {citation}]"
        blocks.append(f"{header}\n{d['text']}")
    background = "\n\n".join(blocks)

    recency_note = (
        "\nThe items above are already sorted most-recent-first by their actual published date. "
        "Preserve this order in your answer -- do not re-order them based on content, topic, or "
        "perceived importance.\n"
        if sorted_by_recency else ""
    )

    prompt = f"""
Background information you already know:

{background}
{recency_note}
User question: {user_question}

You are Buddy, Abhinav's personal assistant. You speak as someone who already knows
Abhinav's background, projects, blogs, skills, and experience.

Instructions:
- Answer strictly based on the background information above.
- Do not mention knowledge bases, retrieval, chunks, or metadata.
- If something is not present in the background, say so plainly.
- Use the exact "title" given in each item's header when referring to a blog post or
  project. Never invent or guess a title -- if no title is given, use the doc_id as-is
  rather than making one up.
- When you refer to specific resume, blog, or project details, include brief citations
  in parentheses using the doc_id and chunk ranges, e.g. (source: rag_guardrails, chunks 1–3).
- For resume questions, first scan every chunk and list EVERY distinct role/position
  found, including short-duration roles, junior titles, or roles sandwiched between
  more senior ones (e.g. a QA or analyst role between two architect roles at the same
  or different companies). Do not omit any entry because it seems minor or because it
  interrupts a cleaner-looking career progression. Only after listing every entry should
  you organize or summarize them (chronologically, grouped by company, etc.) as the user
  requested.
- For "most recent" / "latest" questions about blogs or projects, trust the published
  date given in each item's header -- do not guess recency from content or title alone.
- For blog questions, list or summarise the relevant blogs (up to 4–5) one by one or together.
- For project questions, list or summarise the projects and their key details.
- For topic questions, synthesise the crux across multiple relevant blogs or projects.
"""
    return prompt.strip()


def retrieve_then_stitch_and_ask(agent: Agent, user_question: str) -> str:
    source_type = classify_source_type(user_question)
    recency_intent = detect_recency_intent(user_question)

    # Resumes are small -- always fetch every chunk rather than relying on
    # similarity ranking to surface less "prominent" roles. Recency
    # questions also need the full population fetched so date-sorting has
    # something real to work with, not just whatever ranked highest for
    # the literal query text.
    num_results = 100 if (source_type == "resume" or recency_intent) else 50

    params = {
        "text": user_question,
        "knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID,
        "numberOfResults": num_results,
        "region": REGION,
        "sourceType": source_type,
    }
    logger.info("Calling retrieve_tool with source_type=%s recency_intent=%s", source_type, recency_intent)
    results = retrieve_tool(params)

    if not results or results[0].get("error"):
        logger.info("No usable KB results; answering without KB context.")
        return agent(user_question)

    stitched_docs = stitch_docs_from_results(results, max_docs=5, sort_by_recency=recency_intent)
    if not stitched_docs:
        logger.info("No stitched docs; answering without KB context.")
        return agent(user_question)

    prompt = build_grounded_prompt(user_question, stitched_docs, sorted_by_recency=recency_intent)
    return agent(prompt)


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
knows Abhinav's background, projects, blogs, skills, and experience.

You will sometimes receive a prompt that includes background information plus a user
question. Treat the background as things you already know. Answer naturally and
directly from it, without mentioning knowledge bases, retrieval, chunks, or metadata.

Stay grounded in the information you were actually given. Do not invent project
details, dates, companies, blog titles, or blog content that isn't present.
"""

    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

    agent = Agent(
        model=bedrock_model,
        tools=[],
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
        answer = retrieve_then_stitch_and_ask(agent, user_input)
        print("\nAssistant:\n", answer)


if __name__ == "__main__":
    main()