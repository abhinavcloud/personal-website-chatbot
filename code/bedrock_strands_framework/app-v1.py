import os
import json
import uuid
import logging
from collections import defaultdict
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

def classify_source_type(text_query: str) -> Optional[str]:
    q = text_query.lower()
    if any(h in q for h in _BLOG_HINTS):
        return "blog"
    if any(h in q for h in _PROJECT_HINTS):
        return "project"
    if any(h in q for h in _RESUME_HINTS):
        return "resume"
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

def stitch_docs_from_results(results: List[Dict[str, Any]], max_docs: int = 10) -> List[Dict[str, Any]]:
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
        for c in chunks:
            raw = c.get("content") or c.get("text") or ""
            # If Bedrock returns {"text": "..."} or {"json": {...}}
            if isinstance(raw, dict):
                # Vector search → {"text": "..."}
                if "text" in raw:
                    raw = raw["text"]
                # Managed search sometimes → {"json": {...}}
                elif "json" in raw:
                    raw = json.dumps(raw["json"])
                else:
                    raw = str(raw)

            texts.append(raw)
            m = c.get("metadata", {}) or {}
            indices.append(m.get("chunk_index", 0))
            if source_type is None:
                source_type = m.get("source_type")
        stitched.append({
            "doc_id": doc_id,
            "source_type": source_type,
            "text": "\n\n".join(texts),
            "chunk_indices": indices,
        })

    stitched.sort(key=lambda d: d["doc_id"])
    return stitched[:max_docs]

def build_grounded_prompt(user_question: str, stitched_docs: List[Dict[str, Any]]) -> str:
    blocks = []
    for d in stitched_docs:
        doc_id = d["doc_id"]
        source_type = d["source_type"]
        indices = d["chunk_indices"]
        if indices:
            citation = f"{doc_id}, chunks {min(indices)}–{max(indices)}"
        else:
            citation = doc_id
        header = f"[{source_type or 'unknown'} | {citation}]"
        blocks.append(f"{header}\n{d['text']}")
    background = "\n\n".join(blocks)

    prompt = f"""
Background information you already know:

{background}

User question: {user_question}

You are Buddy, Abhinav's personal assistant. You speak as someone who already knows
Abhinav's background, projects, blogs, skills, and experience.
When the session is initiated, always start by introducing yourself as Buddy, Abhinav's personal assistant. 
You will sometimes receive a prompt that includes background information plus a user question. 
Treat the background as things you already know. Answer naturally and directly from it, without mentioning knowledge bases, retrieval, chunks, or metadata.
Stay grounded in the information you were actually given. Do not invent project details, dates, companies, or blog content that isn't present.

Instructions:
- Answer strictly based on the background information above.
- Do not mention knowledge bases, retrieval, chunks, or metadata.
- If something is not present in the background, say so plainly.
- When you refer to specific resume, blog, or project details, include brief citations
  in parentheses using the doc_id and chunk ranges, e.g. (source: rag_guardrails, chunks 1–3).
- For resume questions, first scan every chunk and list EVERY distinct role/position
  found, including short-duration roles, junior titles, or roles sandwiched between
  more senior ones (e.g. a QA or analyst role between two architect roles at the same
  or different companies). Do not omit any entry because it seems minor or because it
  interrupts a cleaner-looking career progression. Only after listing every entry should
  you organize or summarize them chronologically mentioneing the companies, roles, and durations. 
  If a role is mentioned in multiple chunks, combine the information into a single entry. 
  If a role is mentioned in one chunk but not another, note that it was only found in the chunk where it was
  requested.
- For blog questions, list or summarise the relevant blogs (up to 4–5) one by one or together.
- For project questions, list or summarise the projects and their key details.
- For topic questions, synthesise the crux across multiple relevant blogs or projects.
"""
    return prompt.strip()

def retrieve_then_stitch_and_ask(agent: Agent, user_question: str) -> str:
    source_type = classify_source_type(user_question)
    params = {
        "text": user_question,
        "knowledgeBaseId": STRANDS_KNOWLEDGE_BASE_ID,
        "numberOfResults": 50,
        "region": REGION,
        "sourceType": source_type,
    }
    logger.info("Calling retrieve_tool with source_type=%s", source_type)
    results = retrieve_tool(params)

    if not results or results[0].get("error"):
        logger.info("No usable KB results; answering without KB context.")
        return agent(user_question)

    stitched_docs = stitch_docs_from_results(results, max_docs=5)
    if not stitched_docs:
        logger.info("No stitched docs; answering without KB context.")
        return agent(user_question)

    prompt = build_grounded_prompt(user_question, stitched_docs)
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
details, dates, companies, or blog content that isn't present.
"""

    session_id = f"session-{uuid.uuid4().hex}"
    session_manager = FileSessionManager(session_id=session_id, storage_dir="sessions")

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
