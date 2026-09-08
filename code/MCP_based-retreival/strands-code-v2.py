# V2 Code (regenerated)
#
# Changes from the previous version, and why:
#
# 1. MCP_SYSTEM_PROMPT now explicitly tells the model that list/get tools
#    return metadata only, and that summarizing/quoting/describing content
#    requires a follow-up read_blog / read_projects call using the "path"
#    field. Previously nothing told the model this two-step flow existed,
#    so it treated "I have no summarize tool" as "I can't fetch full text
#    either" and refused.
#
# 2. Steering system prompt's grounding rule (#3) now explicitly
#    distinguishes "paraphrasing/condensing content that IS in the tool
#    result" (allowed, expected) from "introducing facts not in the tool
#    result" (not allowed). The old wording didn't make this distinction,
#    so the judge model (or the base model anticipating it) could easily
#    treat any summary as "inventing" and push toward refusal.
#
# 3. Contact info is no longer something the LLM is trusted to retype from
#    memory of the tool result. mcp_tool now builds a deterministic,
#    pre-formatted contact block directly from the captured resume data
#    (see ContactCaptureHook + format_contact) whenever read_resume was
#    called during that turn, and MAIN_SYSTEM_PROMPT instructs main_agent
#    to relay that block verbatim rather than retyping the phone
#    number/email itself. This removes the main source of the
#    inconsistent/placeholder ("+1 (555) 123-4567") numbers you saw -
#    those were the LLM regenerating digits from its own text prediction
#    instead of the exact tool output.
#
# 4. last_contact_result is now reset at the start of every mcp_tool call
#    (not just cleared after use) and guarded with a lock, since it's
#    shared, mutable, cross-turn state. This avoids a stale contact block
#    from a previous turn leaking into an unrelated later answer.
#
# 5. steering_model now runs at temperature=0.0 with an explicit max_tokens.
#    A judge that isn't deterministic will inconsistently pass/flag the
#    same category of response across calls - exactly the "sometimes it
#    answers, sometimes it refuses the identical question" behavior in
#    your transcript.
#
# 6. Steering rule 4 (disclaimer) is now paired with a proactive
#    requirement: if the tool result actually contains Abhinav's contact
#    fields, the final response MUST surface them (not just avoid
#    mis-disclosing them). Previously the rule only fired reactively, so a
#    flat refusal and a correct disclosure both "passed" steering equally.

import sys
import os
import re
import threading
import boto3
import json
from datetime import datetime, timezone
import uuid
import concurrent.futures


from mcp import StdioServerParameters, stdio_client  # STDIO CLIENT

from strands import Agent
from strands.tools.mcp import MCPClient  # MCP Client
from strands import Agent, tool  # Agent and Tools
from strands.models import BedrockModel  # Models
import strands_shell  # SHELL Tools
from strands.session.file_session_manager import FileSessionManager  # Session Manager
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry  # Hooks


# Optional Debug Logging
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("strands").setLevel(logging.DEBUG)

from strands import AgentSkills  # Skills Plugin

from strands.vended_plugins.steering import LLMSteeringHandler, Proceed, Guide  # Steering Plugins
from strands.vended_plugins.steering.handlers.llm.llm_handler import _LLMSteering  # Steering Plugins


from dotenv import load_dotenv
load_dotenv(".env")  # Loading Env Variables

REGION = os.getenv("REGION")
STEERING_REGION = os.getenv("STEERING_REGION")
MODEL_ID = os.getenv("MODEL_ID")
STEERING_MODEL_ID = os.getenv("STEERING_MODEL_ID")
SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server_local_v2.py")


# -----------------------------------------------------------------------------------------------------------
# Hooks
# ------------------------------------------------------------------------------------------------------------

timestamp = datetime.now(timezone.utc).isoformat()


class ToolLoggerHook(HookProvider):

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": event.tool_use.get("name"),
            "args": event.tool_use.get("arguments"),
            "result": event.result,
            "exception": str(event.exception) if event.exception else None
        }

        safe_log_path = "/workspace/output/tool_calls.jsonl"

        # Read existing log file (if any)
        try:
            existing = shell_mgr.read_file(safe_log_path).decode()
        except Exception:
            existing = ""

        # Append new entry
        updated = existing + json.dumps(log_entry) + "\n"

        # Write back to sandbox (persisted in S3)
        shell_mgr.write_file(safe_log_path, updated.encode())

        print(f"\n[HOOK] Tool Executed:{event.tool_use.get('name')}")

        return event.result


# Shared, mutable, cross-call state - guarded by a lock and reset at the
# start of every mcp_tool invocation (see mcp_tool below) so a previous
# turn's contact data can never leak into a later, unrelated answer.
last_contact_result = {"data": None}
_contact_lock = threading.Lock()

# Display-only channel: holds the fully-formatted contact block for the
# OUTER CALLER (main(), or whatever harness is running this agent) to show
# directly to the user. mcp_tool populates this but never puts the same
# raw text in what it returns to main_agent, specifically so the phone
# number / emails / links never sit in an LLM's context waiting to be
# "reproduced" - see the note in mcp_tool for why that mattered in practice.
pending_contact_block = {"data": None}
_display_lock = threading.Lock()


def _extract_contact_json(text: str):
    """
    Best-effort extraction of a `"contact": { ... }` object out of a raw
    tool-result string. Large tool results (read_resume's included, since
    it returns the whole resume body) can get offloaded/truncated by the
    agent framework into a preview blob before hooks ever see the real
    dict - observed shape: a wrapper whose text contains the original
    JSON, but truncated partway through the long "content" field. That
    truncation breaks json.loads() on the WHOLE blob even though the small
    "contact" sub-object near the top is complete. So instead of parsing
    the whole payload, this scans for the "contact" key and brace-matches
    just that sub-object.
    """
    marker = text.find('"contact"')
    if marker == -1:
        return None
    brace_start = text.find("{", marker)
    if brace_start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[brace_start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None
    return None


class ContactCaptureHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        if event.tool_use.get("name") != "read_resume":
            return event.result

        result = event.result
        contact = None

        if isinstance(result, dict) and "contact" in result:
            # Normal, non-offloaded shape: the raw dict our tool returned.
            contact = result["contact"]
        else:
            # Offloaded/wrapped shape: dig out whatever text blocks are in
            # here and try to recover the contact object from the text
            # rather than assuming a specific dict layout.
            text_blobs = []
            if isinstance(result, dict):
                content = result.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            text_blobs.append(block["text"])
                elif isinstance(result.get("text"), str):
                    text_blobs.append(result["text"])
            elif isinstance(result, str):
                text_blobs.append(result)

            for blob in text_blobs:
                contact = _extract_contact_json(blob)
                if contact:
                    break

        if contact:
            with _contact_lock:
                last_contact_result["data"] = contact
        return event.result


# Records which tool names actually fired during a given agent invocation.
# This exists because prompt instructions alone ("you must call read_resume")
# turned out not to be reliable - the model would sometimes answer a contact
# or summarization question straight from its own text prediction without
# calling anything at all. Callers use this list to verify a required tool
# genuinely ran before trusting the response; if it didn't, they retry with
# a forceful directive rather than passing the ungrounded text through.
#
# Two independent instances are used - one scoped to mcp_agent's own tool
# calls (read_resume/read_blog/...), one scoped to main_agent's tool calls
# (mcp_tool/shell_tool). Keeping separate storage per agent avoids one
# level's clear-before/read-after cycle wiping out the other's in-flight
# data when calls are nested (main_agent -> mcp_tool -> mcp_agent).
_called_tools: list[str] = []
_called_tools_lock = threading.Lock()
_called_tool_errors: list[str] = []  # names of tools whose call actually raised, this cycle

_main_called_tools: list[str] = []
_main_called_tools_lock = threading.Lock()


class ToolCallTracker(HookProvider):
    def __init__(self, storage: list, lock: threading.Lock, error_storage: list = None):
        self._storage = storage
        self._lock = lock
        self._error_storage = error_storage

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        name = event.tool_use.get("name")
        if name:
            with self._lock:
                self._storage.append(name)
                if self._error_storage is not None and event.exception:
                    self._error_storage.append(name)
        return event.result


# -------------------------------------------------------------------------------------------------------------
# Skills Plugin
# -------------------------------------------------------------------------------------------------------------

skill = AgentSkills(skills="./MCP_based-retreival/skills/")

# ------------------------------------------------------------------------------------------------------------
# Steering Plugin
# ------------------------------------------------------------------------------------------------------------

class LLMSteeringHandlerWithModelSteering(LLMSteeringHandler):

    async def steer_before_tool(self, *, agent, tool_use, **kwargs):
        # Tool calls always proceed - no LLM judge needed here.
        # All evaluation happens after the model's final response.
        return Proceed(reason="Tool calls are never blocked; steering only evaluates final responses.")

    async def steer_after_model(self, *, agent, message, stop_reason, **kwargs):
        # Only bother evaluating actual final answers, not intermediate
        # tool_use turns - nothing textual to judge there yet.
        if stop_reason != "end_turn":
            return Proceed(reason="Not a final response yet.")

        # Pull the plain text out of the message content blocks
        response_text = "".join(
            block.get("text", "") for block in message.get("content", []) if "text" in block
        )

        prompt = f"""
        Evaluate this AGENT'S FINAL RESPONSE against the guidance in your system prompt.

        ## Response to evaluate
        {response_text}

        Decide "proceed" if it fully complies with the guidance, or "guide" with
        specific, actionable feedback on what to fix (e.g. missing disclaimer,
        wrong tone, an unsummarized refusal that should have used retrieved
        content instead) if it does not.
        """

        steering_agent = Agent(
            system_prompt=self.system_prompt,
            model=self.model or agent.model,
            callback_handler=None,
        )
        llm_result: _LLMSteering = steering_agent(
            prompt, structured_output_model=_LLMSteering
        ).structured_output

        match llm_result.decision:
            case "proceed":
                return Proceed(reason=llm_result.reason)
            case "guide":
                return Guide(reason=llm_result.reason)
            case _:
                return Proceed(reason="Unhandled decision, defaulting to proceed.")


steering_model = BedrockModel(
    model_id=STEERING_MODEL_ID,
    region_name=STEERING_REGION,
    temperature=0.0,   # deterministic judging - a flaky judge produces the
                       # same-question-different-verdict behavior you saw
    max_tokens=500,    # the decision payload (decision + reason) is small
    streaming=False
)

steering = LLMSteeringHandlerWithModelSteering(
    model=steering_model,
    system_prompt="""

    Guidance:

    You are a steering evaluator. You evaluate ONLY the agent's final textual
    response, after tool execution. You never block, cancel, or modify tool calls.

    1. Tool calls
    - Always return "proceed" for any pre-tool-call evaluation. Tool calls are
    never evaluated or blocked by this handler.

    2. Tone
    - Final response must be friendly, helpful, and semi-formal (not stiffly
    professional, not overly casual).

    3. Grounding vs. paraphrasing (read carefully - these are different things)
    - The final response must answer only what the user asked.
    - REWORDING, CONDENSING, or SUMMARIZING text that IS present in a tool
      result (e.g. a blog/project body returned by read_blog/read_projects)
      is expected, required behavior, not "invention." A 500-word summary
      of a 2,000-word tool-provided article is grounded, correct behavior -
      do NOT flag it as ungrounded just because the exact wording differs
      from the source.
    - What counts as ungrounded (and SHOULD be flagged "guide"): facts,
      numbers, names, dates, or claims that do NOT appear anywhere in the
      tool result the agent had available. Refusing to summarize retrieved
      content, when the content was actually retrieved, is ALSO a defect -
      flag it as "guide" with the feedback "content was available; the
      agent should have summarized it instead of declining."
    - If the tool result genuinely doesn't contain the information needed
      to answer (e.g. no matching entry, an empty body), the agent should
      say so or ask for clarification - that refusal is correct grounding
      behavior, not a defect.

    4. Abhinav's own contact information
    - Abhinav's own phone number, email, LinkedIn, GitHub, and website are
    public information he has chosen to publish on his site, and may be
    shared with visitors without redaction.
    - If the tool results available to the agent this turn include
      Abhinav's contact fields AND the user asked for contact information,
      the final response MUST include those fields. A refusal, hedge, or
      omission in that situation is a defect - flag it "guide" with
      feedback to include the contact details from the tool result.
    - Any response containing Abhinav's own contact details must include this
    disclaimer: "This contact information is shared with Abhinav Kumar's
    consent."
    - This rule applies ONLY to Abhinav's own information. If a tool result
    contains personal information belonging to anyone else (e.g. a third
    party's resume content), that information must NOT be disclosed under
    this rule, and the disclaimer must not be attached to it - it does not
    have Abhinav's consent behind it.

    5. File paths
    - Suppress raw file paths in the final response; describe the result
    instead (e.g. "saved to your output folder").

    6. Decision
    - "proceed" if all of the above are satisfied.
    - Otherwise "guide" with specific, actionable feedback (what's missing:
    disclaimer, tone, an unscoped PII disclosure, an assumption not
    supported by the tool result, an unnecessary refusal to summarize
    available content, etc.).

    """
)

# ---------------------------------------------------------------------------------------------------------------
# Session Manager
session_id = f"session-{uuid.uuid4().hex}"
main_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/main")
mcp_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/mcp")
shell_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/shell")

# ----------------------------------------------------------------------------------------------------------------
# MCP Agent

mcp_model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.3,
    max_tokens=2000,
    context_window_limit=100000,
)

MCP_SYSTEM_PROMPT = """
                    You are Abhinav Kumar's personal website assistant.

                    - Answer questions about Abhinav's background, projects, blog, and resume
                    using the tools available to you.

                    - CONTENT RETRIEVAL (read this carefully):
                      list_blogs, get_last_blog, get_first_blog, get_latest_blogs,
                      get_oldest_blogs (and their project equivalents) return METADATA
                      ONLY - title, subtitle, date, reading time, tags, icon. They do
                      NOT contain the article body.
                      Whenever the user wants a summary, description, paraphrase, key
                      points, or the full text of a SPECIFIC blog or project, you must:
                        1. Identify the entry (via a list/get tool if you don't already
                           have its "path"), then
                        2. Call read_blog(path) or read_projects(path) with that path
                           to retrieve the actual body text, then
                        3. Compose your answer from that retrieved body.
                      Rephrasing or condensing text that a tool actually returned is
                      expected and required - it is NOT "inventing" content. Only
                      introducing facts that appear nowhere in the tool output counts
                      as fabrication. Never tell the user that summarizing is "outside
                      the tool's capabilities" - retrieving the body IS the tool's
                      capability; composing the summary from it is your job.

                    - CONTACT INFORMATION:
                      For ANY question about Abhinav's phone number, email, LinkedIn,
                      GitHub, or personal website, you must call read_resume in this
                      turn and answer using its "contact" fields exactly as returned -
                      never from memory, never a guessed or reconstructed number.
                      Abhinav's own contact information is public and may be shared
                      with visitors who ask for it, without redaction, exactly as
                      returned by the tool.

                    - Do not extend the contact-sharing rule to anyone else's personal
                    information. If a tool result contains PII belonging to someone
                    other than Abhinav, do not disclose it.

                    - MULTI-PART REQUESTS AND ERRORS:
                      A single question can need more than one tool call - e.g. "his
                      latest AND oldest blog" needs both a get_last_blog/
                      get_latest_blogs call AND a get_first_blog/get_oldest_blogs
                      call. Make every call the question actually needs, not just
                      the first one. If a tool call genuinely fails, say plainly that
                      it failed - never invent a specific-sounding reason ("a
                      concurrency issue", "a technical error") for something that
                      didn't happen. If you're not sure whether a tool is needed for
                      part of a question, call it rather than guessing from memory
                      or from something listed earlier in this conversation.
                    """

# MCP Client
website_mcp = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(
            command=sys.executable,   # guarantees same venv/interpreter
            args=[SERVER_PATH],       # absolute path, not relative
        )
    )
)

mcp_agent = Agent(
    agent_id="mcp_agent",
    name="mcp_agent",
    description="Answers questions about Abhinav — his blog, projects, resume, and contact info (phone, email, LinkedIn, GitHub, website). Authorized to share his contact/PII directly.",
    tools=[website_mcp],
    model=mcp_model,
    context_manager="auto",
    system_prompt=MCP_SYSTEM_PROMPT,
    callback_handler=None,
    hooks=[ToolLoggerHook(), ContactCaptureHook(), ToolCallTracker(_called_tools, _called_tools_lock, _called_tool_errors)],
    plugins=[skill, steering],
    session_manager=mcp_session_manager
)


def format_contact(c: dict) -> str:
    emails = ", ".join(c["email"]) if isinstance(c.get("email"), list) else c.get("email", "")
    return (
        f"Name: {c.get('name', '')}\n"
        f"Title: {c.get('title', '')}\n"
        f"Location: {c.get('location', '')}\n"
        f"Phone: {c.get('phone', '')}\n"
        f"Email: {emails}\n"
        f"LinkedIn: {c.get('linkedin', '')}\n"
        f"GitHub: {c.get('github', '')}\n"
        f"Website: {c.get('website', '')}\n"
        f"(This contact information is shared with Abhinav Kumar's consent.)"
    )


# --- Verification, not topic-guessing -----------------------------------
# Earlier versions tried to predict which specific tool a query needed via
# per-topic regexes (summarize/describe/latest/oldest/...). That doesn't
# scale: "longest post", "posts about Kubernetes", "project on serverless
# queues" - the space of possible questions is open-ended and can't be
# enumerated. Two things generalize across ANY topic instead:
#
#   1. Did the agent call ANY tool at all? mcp_agent's whole purpose is
#      tool-backed lookups; answering a substantive question with zero
#      tool calls is suspicious regardless of what the question was about.
#   2. Does the response claim a failure/error that didn't actually
#      happen? This is checked against REAL exception status from the
#      tool-call hooks, not keyword-guessed from the query - so it catches
#      "I encountered a concurrency issue" being fabricated for ANY topic,
#      not just the ones someone thought to write a regex for.
#
# Contact info stays as its own narrow check: unlike open-ended blog/
# project topics, "phone/email/linkedin/github/website" is a small, fixed,
# enumerable schema, and PII correctness is worth a dedicated safety net.

_CONTACT_INTENT_RE = re.compile(
    r"\b(phone|mobile|call him|contact\s*(info|number|details?)|email|e-mail|"
    r"linkedin|github|reach (him|abhinav)|get in touch)\b",
    re.IGNORECASE,
)

# Phrasing that claims something went wrong - checked against actual
# exception status (see _tools_called_during's had_error), not trusted at
# face value. Kept general (no topic words) so it applies no matter what
# the underlying question was about.
_FABRICATED_FAILURE_RE = re.compile(
    r"\b(concurrency issue|encountered an? (error|issue|problem)|"
    r"ran into an? (error|issue|problem)|unable to retrieve|"
    r"couldn'?t retrieve|could not retrieve|failed to retrieve|"
    r"technical issue|something went wrong|error occurred)\b",
    re.IGNORECASE,
)

# Values that showed up as fabricated placeholders in testing - if these
# (or anything shaped like them) appear in a contact answer that wasn't
# actually backed by a verified read_resume call, treat the whole answer
# as untrustworthy rather than merely suspicious.
_PLACEHOLDER_RE = re.compile(
    r"(example\.com|555[-.\s]?123|123[-.\s]?4567|jane\.?doe|john\.?doe)",
    re.IGNORECASE,
)


def _tools_called_during(fn, storage: list, lock: threading.Lock, error_storage: list = None):
    """
    Run fn(), returning (result, set-of-tool-names-invoked, had_error).
    had_error is True only if a tool call genuinely raised an exception
    this cycle (per the hook's event.exception) - always False if
    error_storage isn't provided (main_agent's tracker doesn't need it;
    mcp_tool never raises from Python, it always returns a dict).
    """
    with lock:
        storage.clear()
        if error_storage is not None:
            error_storage.clear()
    result = fn()
    with lock:
        called = set(storage)
        storage.clear()
        had_error = bool(error_storage) if error_storage is not None else False
        if error_storage is not None:
            error_storage.clear()
    return result, called, had_error


@tool
def mcp_tool(query: str) -> dict:
    """
    Route a query to Abhinav's website knowledge base (blog, projects,
    resume, contact info).

    If this call successfully retrieves Abhinav's contact info, the
    returned dict has "contact_available": true, but the "text" field
    deliberately does NOT contain the phone number, email, or links - the
    formatted block is written to a separate display-only channel
    (pending_contact_block) for the outer caller to show directly. This is
    intentional: keeping raw PII out of the orchestrating agent's context
    avoids both transcription/hallucination risk and a hosting-layer
    content filter that was observed to silently truncate a response when
    the model was asked to "reproduce" a block containing a phone number
    and multiple emails.
    """
    with _contact_lock:
        last_contact_result["data"] = None

    wants_contact = bool(_CONTACT_INTENT_RE.search(query))

    mcp_response, called, had_error = _tools_called_during(
        lambda: mcp_agent(query), _called_tools, _called_tools_lock, _called_tool_errors
    )
    response_text = str(mcp_response)

    def _fabricated_failure(text, had_error):
        return bool(_FABRICATED_FAILURE_RE.search(text)) and not had_error

    contact_missing = wants_contact and "read_resume" not in called
    zero_tools_called = len(called) == 0
    fabricated_failure = _fabricated_failure(response_text, had_error)

    if contact_missing or zero_tools_called or fabricated_failure:
        # One retry with an explicit, hard-to-ignore directive - belt-and-
        # suspenders alongside the system prompt, which evidently wasn't
        # always enough on its own.
        reasons = []
        if contact_missing:
            reasons.append("you must call read_resume before answering any contact question")
        if zero_tools_called:
            reasons.append(
                "you answered without calling any tool - you must use one of "
                "your available tools to answer this, never from memory or "
                "from what you already listed earlier in this conversation"
            )
        if fabricated_failure:
            reasons.append(
                "you claimed an error/issue/problem occurred, but no tool call "
                "actually failed - there was no real error, retry the lookup "
                "for real and do not repeat that claim"
            )

        forced_query = f"{query}\n\n(System: {'. Also, '.join(reasons)}.)"
        mcp_response, called, had_error = _tools_called_during(
            lambda: mcp_agent(forced_query), _called_tools, _called_tools_lock, _called_tool_errors
        )
        response_text = str(mcp_response)
        contact_missing = wants_contact and "read_resume" not in called
        zero_tools_called = len(called) == 0
        fabricated_failure = _fabricated_failure(response_text, had_error)

    contact_block = None
    with _contact_lock:
        if last_contact_result["data"]:
            contact_block = format_contact(last_contact_result["data"])
            last_contact_result["data"] = None  # consumed

    # Trust is gated on the verified `called` set (tool names actually
    # invoked), never on whether contact_block happened to parse. A tool
    # can genuinely run and return correct data while our best-effort
    # extraction of a deterministic contact_block still fails (e.g. an
    # offloaded/truncated preview our parser doesn't recognize) - that's a
    # cosmetic miss, not a grounding failure, and must not turn a correct,
    # disclaimer-compliant answer into a refusal.
    if wants_contact:
        if "read_resume" not in called:
            # Never ran, even after the forced retry - genuinely ungrounded.
            return {
                "text": (
                    "I wasn't able to retrieve Abhinav's contact details from "
                    "the website's data source just now, so I won't guess at a "
                    "phone number or email. Please try again in a moment."
                ),
                "contact_available": False,
            }
        if _PLACEHOLDER_RE.search(response_text):
            # The tool ran, but the final text still looks like a
            # fabricated placeholder rather than real retrieved data.
            return {
                "text": (
                    "I wasn't able to retrieve verified contact details from "
                    "the website's data source just now, so I won't guess at a "
                    "phone number or email. Please try again in a moment."
                ),
                "contact_available": False,
            }
        if contact_block is not None:
            # Success. Deliberately do NOT put the phone number / emails /
            # links in what main_agent sees: earlier testing showed that
            # putting raw PII in the model's context and then instructing
            # it to "reproduce it verbatim" is exactly the pattern that
            # trips a hosting-layer content filter, which silently
            # truncated the whole response (outputTokens: 0). Instead,
            # main_agent only gets a flag; the actual formatted block is
            # stashed here for the outer caller (see main()) to display
            # directly, bypassing the LLM for the sensitive characters
            # entirely.
            with _display_lock:
                pending_contact_block["data"] = contact_block
            return {
                "text": (
                    "Abhinav's contact details were retrieved successfully. "
                    "The verified details will be shown separately - do not "
                    "restate, retype, or guess any of the values yourself."
                ),
                "contact_available": True,
            }

    # General guard for everything else, regardless of topic: if the model
    # still called nothing, or is still claiming a failure that never
    # actually happened, don't relay that text - it isn't grounded and/or
    # isn't honest. This intentionally discards a partially-correct answer
    # in the rare case a compound request (e.g. "latest and oldest blog")
    # only half-succeeded even after retry, in favor of never passing
    # through an invented excuse.
    if zero_tools_called or fabricated_failure:
        return {
            "text": (
                "I wasn't able to retrieve that from the website's data "
                "source just now - there was no actual error, the lookup "
                "just didn't complete. Please try again."
            ),
            "contact_available": False,
        }

    return {
        "text": response_text,
        "contact_available": False,
    }


# -------------------------------------------------------------------------------------------------------------
# Shell Agent

SHELL_PATH = os.path.join(
    "C:\\Users\\abhin",
    "Documents",
    "GitHubRepos",
    "personal-website-chatbot",
    "code"
)

OUTPUT_PATH = os.path.join(SHELL_PATH, "agent_output")
os.makedirs(OUTPUT_PATH, exist_ok=True)


class ShellManager:
    def __init__(self):
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.shell = self.executor.submit(
            lambda: strands_shell.Shell(
                binds=[
                    strands_shell.Bind(
                        source=OUTPUT_PATH,
                        destination="/workspace/output",
                        mode="direct",
                    )
                ]
            )
        ).result()

    def run(self, command: str):
        return self.executor.submit(lambda: self.shell.run(command)).result()

    def write_file(self, path: str, content: bytes):
        return self.executor.submit(lambda: self.shell.write_file(path, content)).result()

    def read_file(self, path: str):
        return self.executor.submit(lambda: self.shell.read_file(path)).result()

    def list_files(self, path: str):
        return self.executor.submit(lambda: self.shell.list_files(path)).result()

    def close(self):
        def drop_shell():
            self.shell = None
        self.executor.submit(drop_shell).result()
        self.executor.shutdown(wait=True)


shell_mgr = ShellManager()


# Shell Tools

@tool
def sandbox_write(path: str, content: str) -> str:
    """
    Write content to a file in the sandbox output workspace, overwriting
    any existing file at that path.

    Explicitly removes any existing file before writing: the underlying
    write_file() was observed to leave old trailing bytes in place when a
    new write was SHORTER than a previous one at the same path (a prior
    placeholder/partial write followed by the real, shorter content
    produced visibly corrupted output - old bytes bleeding through past
    the end of the new content). rm -f first guarantees a clean write
    regardless of write_file's truncation behavior.
    """
    safe_path = f"/workspace/output/{os.path.basename(path)}"
    shell_mgr.run(f"rm -f {safe_path}")
    shell_mgr.write_file(safe_path, content.encode())
    return f"Wrote {len(content)} bytes to {safe_path}"


@tool
def sandbox_shell(command: str) -> dict:
    cmd = command
    if cmd.strip() == "ls":
        cmd = "ls /workspace/output"

    result = shell_mgr.run(cmd)
    stdout = result.stdout.decode() if result.stdout else ""
    stderr = result.stderr.decode() if result.stderr else ""
    status = int(result.status)

    return {"stdout": stdout, "stderr": stderr, "status": status}


@tool
def sandbox_read(path: str) -> str:
    safe_path = f"/workspace/output/{os.path.basename(path)}"
    data = shell_mgr.read_file(safe_path)
    return data.decode()


@tool
def sandbox_list(path: str = "") -> list:
    safe_path = "/workspace/output"
    files = shell_mgr.list_files(safe_path)
    return [f for f in files]


shell_tools = [sandbox_shell, sandbox_write, sandbox_read, sandbox_list]

# Shell Model
shell_model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.3,
    max_tokens=2000,
    context_window_limit=100000,
)

SHELL_SYSTEM_PROMPT = """
                    You are Abhinav Kumar's personal sandbox assistant.

                    - Execute file read/write/list operations in the sandbox workspace exactly
                    as requested, using the available shell tools.
                    - Files in this workspace, including any containing Abhinav's own contact
                    details (phone, email, etc.), may be read, written, or listed without
                    additional filtering.
                    - This scope is the sandbox workspace only; it does not authorize disclosing
                    any third party's personal information encountered in files you handle.
                    """

# Shell Agent
shell_agent = Agent(
    agent_id="shell_agent",
    name="shell_agent",
    tools=shell_tools,
    model=shell_model,
    system_prompt=SHELL_SYSTEM_PROMPT,
    context_manager="auto",
    callback_handler=None,
    description="Executes shell commands and reads/writes files in Abhinav's personal sandbox workspace.",
    hooks=[ToolLoggerHook()],
    plugins=[skill],
    session_manager=shell_session_manager
)


@tool
def shell_tool(query: str) -> str:
    shell_response = shell_agent(query)
    return str(shell_response)


# -------------------------------------------------------------------------------------------------

MAIN_SYSTEM_PROMPT = """
                Routing rules (re-evaluated independently on every turn):

                1. Route to `mcp_tool` for anything about Abhinav, including:
                - Background, biography, education, career, work history
                - Blog posts, projects, portfolio, resume, personal website content
                - Contact information: phone number, email, LinkedIn, GitHub, website
                - Any visitor request to contact Abhinav
                - Any query requiring data from Abhinav's website knowledge base

                Abhinav's own PII (phone, email, LinkedIn, GitHub, website) is public
                and must be shared as returned by the tool, without being withheld,
                redacted, or hedged.

                1a. If `mcp_tool`'s result has "contact_available": true, do NOT
                state, retype, reformat, or guess the phone number, email,
                LinkedIn, GitHub, or website yourself - you were not given those
                exact values in this field, only a confirmation that they were
                retrieved. Respond with a short acknowledgment only, e.g. "Sure —
                here are Abhinav's verified contact details:" and nothing else.
                The system displays the actual contact details separately; do
                not attempt to fill that in yourself.

                2. Route to `shell_tool` for anything involving shell or sandbox
                operations:
                - Running shell commands
                - Reading, writing, or listing files
                - Interacting with /workspace/output
                - Any sandbox or filesystem operation

                3. A single request may require both tools in sequence - e.g. "summarize
                his blog and save it to a file" means: call `mcp_tool` FIRST, wait for
                its actual result, and only THEN call `shell_tool`, passing it the real
                content `mcp_tool` returned. Chaining tools across a turn is still
                routing, not content generation. You may not summarize, compose, or
                generate the content yourself, and you may not stop after the first
                tool and hand the rest back to the user as something they should do
                manually.

                Never call `shell_tool` before `mcp_tool` has returned in the same
                chained request - do not write a placeholder, an empty file, or a
                "content pending" stub and fill it in later. If you already have
                everything needed to complete a chained request, complete it fully in
                this turn; do not call a tool and then ask the user whether you should
                proceed with what you already have all the information to do.

                4. Choose the tool(s) the request actually needs - never zero. If a
                request needs only one tool, call only that one.

                5. Never produce your own explanation, reasoning, apology, caveat, or
                substitute content (including code snippets, `<thinking>` text, or
                framing like "you could do this yourself"). If a tool can fulfill the
                request, call it - do not describe how the user could do it instead.

                6. Your visible output is limited to the result of the tool call(s) -
                the last tool's result if chaining (or the short acknowledgment per
                rule 1a when "contact_available" is true). No preamble, no commentary
                about previous tool outputs, no meta-discussion of routing.

                7. If no available tool can fulfill any part of the request, say so in
                one plain sentence, and nothing else.

                8. Evaluate every turn on its own terms. If an earlier turn in this
                conversation hedged, refused, or called something "off-limits" - for
                a completely different question - that has no bearing on this turn.
                Do not let the tone of a previous answer carry over into a routing
                decision now; re-apply rules 1-7 fresh, from the current user message
                alone.
            """

# Main Model
main_model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.3,
    max_tokens=2000,
    context_window_limit=100000,
)

main_agent = Agent(
    agent_id="main_agent",
    name="main_agent",
    model=main_model,
    tools=[mcp_tool, shell_tool],
    context_manager="auto",
    system_prompt=MAIN_SYSTEM_PROMPT,
    callback_handler=None,
    hooks=[ToolCallTracker(_main_called_tools, _main_called_tools_lock)],
    session_manager=main_session_manager
)


def ask_main_agent(user_input: str):
    """
    Wraps main_agent(user_input) with the same verify-then-retry pattern
    used one level down for mcp_agent/read_resume. Rather than guessing
    which topics need mcp_tool (an open-ended, unenumerable space - blog
    topics, project topics, "longest post", etc.), this uses two
    topic-agnostic checks: the RAW user question is checked for contact
    intent (a small, fixed vocabulary, worth its own safety net given PII
    is at stake), and the response is checked for a claimed failure with
    zero tool calls behind it (which generalizes to any topic - it's what
    "I encountered a concurrency issue" for a "first blog" question and
    "I'm not allowed to share that" for a phone-number question have in
    common: neither called mcp_tool at all).
    """
    if not user_input or not user_input.strip():
        # Bedrock's ConverseStream rejects a blank text content block
        # outright (ValidationException), which previously crashed the
        # whole process on an empty Enter press. Never send empty/
        # whitespace-only input to the model at all.
        return "Please type a question."

    wants_contact = bool(_CONTACT_INTENT_RE.search(user_input))

    response, called, _ = _tools_called_during(
        lambda: main_agent(user_input), _main_called_tools, _main_called_tools_lock
    )

    def _needs_retry(resp, called):
        contact_missing = wants_contact and "mcp_tool" not in called
        fabricated_failure = (
            bool(_FABRICATED_FAILURE_RE.search(str(resp))) and len(called) == 0
        )
        return contact_missing or fabricated_failure

    if _needs_retry(response, called):
        forced_input = (
            f"{user_input}\n\n"
            f"(System: you answered the previous attempt at this without calling "
            f"any tool. Per routing rule 1, any question about Abhinav must be "
            f"routed to mcp_tool. There was no actual error on a previous turn if "
            f"one was mentioned - call mcp_tool now instead of answering directly "
            f"or repeating an earlier excuse.)"
        )
        response, called, _ = _tools_called_during(
            lambda: main_agent(forced_input), _main_called_tools, _main_called_tools_lock
        )

    if _needs_retry(response, called):
        # mcp_tool still never ran - don't trust whatever main_agent said on
        # its own; that's exactly the "I'm not allowed to share that" /
        # invented-excuse failure mode seen in testing.
        return (
            "I wasn't able to look that up from Abhinav's website data just "
            "now. Please try asking again."
        )

    return response


# ---------------------------------------------------

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            shell_mgr.close()
            break
        if not user_input.strip():
            continue

        response = ask_main_agent(user_input)

        # main_agent's own text is now deliberately PII-free (see mcp_tool /
        # MAIN_SYSTEM_PROMPT rule 1a) - it only ever produces a short
        # acknowledgment like "Sure, here are Abhinav's contact details:".
        # The actual formatted block, if this turn retrieved one, lives
        # here and is printed directly - the LLM never generates it.
        with _display_lock:
            pending_block = pending_contact_block["data"]
            pending_contact_block["data"] = None

        print(f"Assistant: {response}")
        if pending_block:
            print(pending_block)


if __name__ == "__main__":
    main() 