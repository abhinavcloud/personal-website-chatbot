import os
import re
import threading
from datetime import datetime, timezone

from dotenv import load_dotenv

from mcp.client.streamable_http import streamablehttp_client

from strands import Agent, tool
from strands.tools.mcp import MCPClient
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
# CHANGED: AgentCoreMemorySessionManager import is deferred to build_agents()
# below rather than done here at module load - it requires the
# 'bedrock-agentcore[strands-agents]' extra, which local dev without
# AGENTCORE_MEMORY_ID set shouldn't need to have installed.
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry
from strands import AgentSkills
from strands.agent.conversation_manager import SummarizingConversationManager

# CHANGED: steering plugin reintroduced, but scoped narrowly this time - see
# the "Steering" section below for why and how.
from strands.vended_plugins.steering import (
    LLMSteeringHandler,
    Proceed,
    Guide,
)
from strands.vended_plugins.steering.handlers.llm.llm_handler import _LLMSteering

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext


# =============================================================================
# Environment / Application
# =============================================================================

load_dotenv(".env")

REGION = os.getenv("REGION")
STEERING_REGION = os.getenv("STEERING_REGION")
MODEL_ID = os.getenv("MODEL_ID")
STEERING_MODEL_ID = os.getenv("STEERING_MODEL_ID")
GATEWAY_URL = os.getenv("GATEWAY_URL")
# CHANGED: replaces FileSessionManager for main_agent - local disk isn't a
# reliable persistence layer once deployed to AgentCore Runtime (containers
# aren't guaranteed to keep local state between invocations of the same
# session). If unset, build_agents() falls back to FileSessionManager so
# local development still works without a real AWS Memory resource.
AGENTCORE_MEMORY_ID = os.getenv("AGENTCORE_MEMORY_ID")

if not GATEWAY_URL:
    raise RuntimeError(
        "GATEWAY_URL is not set - add it to .env "
        "(the AgentCore Gateway MCP endpoint URL)."
    )

app = BedrockAgentCoreApp()


# =============================================================================
# Skills
# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
skill = AgentSkills(skills=os.path.join(SCRIPT_DIR, "skills"))


# =============================================================================
# Tool-call tracking
#
# The trackers are intentionally separate:
#
#   main_agent -> profile_tool -> profile_agent -> gateway tool
#
# This prevents a nested profile-agent call from overwriting the main-agent
# tracker state.
# =============================================================================

_profile_called_tools: list[str] = []
_profile_called_tools_lock = threading.Lock()
_profile_called_tool_errors: list[str] = []

_main_called_tools: list[str] = []
_main_called_tools_lock = threading.Lock()


class ToolCallTracker(HookProvider):
    """Record the tools actually invoked by an agent invocation."""

    def __init__(self, storage, lock, error_storage=None):
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


class ToolLoggerHook(HookProvider):
    """
    Deployment-safe tool logger.

    The old implementation used strands_shell to write tool-call logs into
    /workspace/output. That filesystem/shell dependency has intentionally been
    removed. AgentCore/CloudWatch can collect stdout/stderr without requiring a
    shell sandbox.
    """

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        tool_name = event.tool_use.get("name")

        print(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "tool_call",
                "tool": tool_name,
                "exception": str(event.exception) if event.exception else None,
            },
            flush=True,
        )

        return event.result


# =============================================================================
# CHANGED: OffloadRetrievalGuard
#
# context_manager="auto" wires in the ContextOffloader plugin (max_result_
# tokens=1500), which registers a retrieve_offloaded_content tool whenever a
# gateway result (resume, blog body, project body) exceeds that threshold.
# Nothing told the model when to stop calling that tool, so it could retrieve
# the same reference repeatedly forever - this is what caused the profile
# agent to loop indefinitely on "work history" instead of returning to the
# main agent.
#
# This hook is the actual loop-breaker: it rejects a second
# retrieve_offloaded_content call for a reference already fetched in this
# invocation, regardless of whether the model's own instructions are
# followed. It applies uniformly to resume, blog, and project content - no
# per-tool exceptions.
# =============================================================================

class OffloadRetrievalGuard(HookProvider):
    """Blocks re-fetching the same offloaded reference more than once per
    agent invocation, preventing retrieve_offloaded_content loops."""

    def __init__(self):
        self._seen_refs: set[str] = set()

    def reset(self) -> None:
        self._seen_refs.clear()

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        if event.tool_use.get("name") == "retrieve_offloaded_content":
            reference = event.tool_use.get("input", {}).get("reference")

            if reference in self._seen_refs:
                event.result = {
                    "status": "error",
                    "content": [
                        {
                            "text": (
                                "You already retrieved this content. Do not "
                                "call retrieve_offloaded_content again for "
                                "this reference - answer now using what you "
                                "already have."
                            )
                        }
                    ],
                }
            elif reference:
                self._seen_refs.add(reference)

        return event.result


# =============================================================================
# Gateway tool naming
#
# AgentCore Gateway exposes tools using:
#
#     <targetName>___<toolName>
#
# We only need the suffix when validating that a profile lookup actually
# invoked a real gateway tool.
# =============================================================================

RESUME_TOOL_NAMES = {"read_resume"}

BLOG_TOOL_NAMES = {
    "list_blogs",
    "get_first_blog",
    "get_last_blog",
    "get_latest_blogs",
    "get_oldest_blogs",
    "read_blog",
}

PROJECTS_TOOL_NAMES = {
    "list_projects",
    "get_first_project",
    "get_last_project",
    "get_latest_projects",
    "get_oldest_projects",
    "read_projects",
}

ALL_GATEWAY_TOOL_NAMES = (
    RESUME_TOOL_NAMES
    | BLOG_TOOL_NAMES
    | PROJECTS_TOOL_NAMES
)


def _bare_tool_name(name: str) -> str:
    """Convert target___tool to tool; leave an unprefixed name unchanged."""
    return name.rsplit("___", 1)[-1] if "___" in name else name


def _tool_was_called(called: set[str], bare_name: str) -> bool:
    return any(_bare_tool_name(tool_name) == bare_name for tool_name in called)


# =============================================================================
# Steering
#
# CHANGED: reintroduced, but scoped narrowly this time. Steering previously
# ran on every profile_agent answer, including contact-info questions - its
# Guide-retry loop forced a SECOND generation of an already-correct answer,
# and that redundant regeneration of short, high-entropy PII (a phone
# number) was the repeated trigger for Nova's built-in content filter
# blocking legitimate output. Contact info is now handled deterministically
# in profile_tool() instead (see _finalize_profile_text), with no model call
# involved at all.
#
# Steering now evaluates ONLY blog/project summaries - grounding (no
# invented facts) and a helpful closing line. This is steering's genuine
# value: catching hallucination in open-ended prose, where a regenerated
# paragraph carries none of the "short unique identifier" risk that a
# regenerated phone number does. Two independent guards keep it out of
# contact-info territory entirely:
#   1. steer_after_model hard-skips (no LLM call at all) if the turn's own
#      user query matches contact intent.
#   2. It ALSO hard-skips if the drafted response itself contains something
#      that looks like a disclosed contact value - so even a misrouted or
#      unexpected case can never trigger a PII regeneration.
# Both checks reference _CONTACT_INTENT_RE / _contains_contact_value,
# defined later in this file - safe, since Python resolves module-level
# names at call time, and this method is only ever called after the whole
# module has finished loading.
# =============================================================================

class LLMSteeringHandlerWithModelSteering(LLMSteeringHandler):

    async def steer_before_tool(self, *, agent, tool_use, **kwargs):
        # Tool calls are never blocked by the steering handler.
        return Proceed(
            reason="Tool calls are never blocked; steering evaluates final responses."
        )

    async def steer_after_model(
        self,
        *,
        agent,
        message,
        stop_reason,
        **kwargs,
    ):
        # Only evaluate an actual final model response.
        if stop_reason != "end_turn":
            return Proceed(reason="Not a final response yet.")

        response_text = "".join(
            block.get("text", "")
            for block in message.get("content", [])
            if "text" in block
        )

        # Hard skip #1: never evaluate a contact-info turn. profile_agent is
        # stateless (see build_agents), so the first user message in
        # agent.messages is this turn's actual query.
        first_user_text = ""
        for m in agent.messages:
            if m.get("role") == "user":
                first_user_text = "".join(
                    b.get("text", "") for b in m.get("content", []) if "text" in b
                )
                break

        if _CONTACT_INTENT_RE.search(first_user_text):
            return Proceed(reason="Contact-info turn - never re-evaluated by steering.")

        # Hard skip #2: never evaluate a response that already looks like it
        # discloses a contact value, regardless of how the query classified.
        if _contains_contact_value(response_text):
            return Proceed(reason="Response contains a contact value - skipped.")

        prompt = f"""
Evaluate this AGENT'S FINAL RESPONSE against the guidance in your system prompt.

## Response to evaluate
{response_text}

Decide "proceed" if it fully complies with the guidance, or "guide" with
specific, actionable feedback on what to fix if it does not.
"""

        steering_agent = Agent(
            system_prompt=self.system_prompt,
            model=self.model or agent.model,
            callback_handler=None,
        )

        llm_result: _LLMSteering = steering_agent(
            prompt,
            structured_output_model=_LLMSteering,
        ).structured_output

        match llm_result.decision:
            case "proceed":
                return Proceed(reason=llm_result.reason)
            case "guide":
                return Guide(reason=llm_result.reason)
            case _:
                return Proceed(
                    reason="Unhandled decision, defaulting to proceed."
                )


steering_model = BedrockModel(
    model_id=STEERING_MODEL_ID,
    region_name=STEERING_REGION,
    temperature=0.0,
    max_tokens=500,
    streaming=False,
)

steering = LLMSteeringHandlerWithModelSteering(
    model=steering_model,
    system_prompt="""
Guidance:

You are a steering evaluator for the Profile Agent. You evaluate ONLY the
agent's final textual response, after tool execution, for questions about
Abhinav's BLOG POSTS or PROJECTS. Contact-info answers never reach you -
they are skipped before evaluation. You never block, cancel, or modify
tool calls - always "proceed" for pre-tool-call evaluation.

1. Grounding
- The response must use only information present in tool results from this
  conversation. Rewording, condensing, or summarizing retrieved content is
  expected behavior, not invention.
- Facts, technologies, dates, or claims not present in any tool result are
  ungrounded - flag that as a defect.
- If a tool result genuinely lacks the needed information, saying so is
  correct behavior, not a defect.

2. Helpful close
- A blog or project summary should end with a short line inviting the user
  to ask for more detail or about another blog/project. Flag a summary
  that just stops with no closing line.

3. Decision
- Return "proceed" if the response satisfies the guidance above.
- Otherwise return "guide" with specific, actionable feedback.
""",
)


# =============================================================================
# Session Manager
# =============================================================================
#
# FileSessionManager is retained because it is not a shell tool. It is used by
# Strands for agent conversation/session state. The shell-agent implementation,
# shell manager, shell subprocesses, and shell workspace are completely removed.
#
# If you later decide to use a different persistent session backend, this is
# the only area that needs to change.
# =============================================================================

# CHANGED: a fresh OffloadRetrievalGuard is created per build so each session
# invocation starts with a clean "seen references" set.
_profile_offload_guard: OffloadRetrievalGuard | None = None


def build_agents(session_id: str):
    global profile_agent, main_agent, _profile_offload_guard

    _profile_offload_guard = OffloadRetrievalGuard()

    # CHANGED: profile_agent is built WITHOUT a persistent session_manager -
    # every invoke() gives it a completely blank conversation. Previously it
    # shared the same session_id-linked FileSessionManager as main_agent, so
    # it accumulated the ENTIRE conversation across every turn, including any
    # content-filter block event or hedge. Once that happened once, later
    # turns read it back as if it were the agent's own established position
    # and generalized it to unrelated requests (blog links, career history)
    # that were never blocked at all. profile_agent doesn't need cross-turn
    # memory - it's a stateless tool-router that answers one self-contained
    # question at a time - so removing its persistence removes the surface
    # for this contamination entirely.
    #
    # Continuity for follow-ups like "summarize the blog you just gave me"
    # is handled by main_agent instead, which keeps its own persistent
    # session and is instructed (see MAIN_SYSTEM_PROMPT) to resolve such
    # references into a complete, self-contained profile_tool query itself.
    profile_agent = make_profile_agent()

    # CHANGED: main_agent's session manager now depends on AGENTCORE_MEMORY_ID.
    # FileSessionManager wrote to local disk, which AgentCore Runtime does not
    # guarantee persists between invocations of the same session once
    # deployed - it's a local-dev convenience only. AgentCoreMemorySessionManager
    # is the harness-native replacement: it persists to a managed Memory
    # resource (DynamoDB-backed) instead of local disk, so it survives across
    # container recycles. actor_id is set to session_id here since this app
    # has no auth/user identity yet - each browser session is its own actor;
    # revisit if you later add real user accounts.
    if AGENTCORE_MEMORY_ID:
        from bedrock_agentcore.memory.integrations.strands.config import (
            AgentCoreMemoryConfig,
        )
        from bedrock_agentcore.memory.integrations.strands.session_manager import (
            AgentCoreMemorySessionManager,
        )

        memory_config = AgentCoreMemoryConfig(
            memory_id=AGENTCORE_MEMORY_ID,
            session_id=session_id,
            actor_id=session_id,
        )
        main_session_manager = AgentCoreMemorySessionManager(
            memory_config,
            region_name=REGION,
        )
    else:
        # Local-dev fallback - no real AWS Memory resource needed to run
        # this locally. Never reached once AGENTCORE_MEMORY_ID is set in
        # the deployed environment's .env / task definition.
        main_session_manager = FileSessionManager(
            session_id=session_id,
            storage_dir="./agent_sessions/main",
        )

    main_agent = make_main_agent(main_session_manager)


# =============================================================================
# Profile Agent
# =============================================================================
#
# This replaces the old "mcp_agent".
#
# The agent's responsibility is not "MCP" as a capability. MCP is merely the
# transport used to reach the AgentCore Gateway. The actual business capability
# is answering questions about Abhinav's profile, including resume, blogs,
# projects, and contact information.
# =============================================================================

profile_model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.3,
    max_tokens=2000,
    context_window_limit=100000,
)

# CHANGED: PROFILE_SYSTEM_PROMPT rewritten from a long lettered decision tree
# (A-H) with repeated IMPORTANT/MUST/NEVER restatements down to one short
# workflow. The routing logic (metadata tool -> get path -> content tool) is
# preserved but stated once. The disclaimer/consent requirement is enforced
# deterministically in code now (see _finalize_profile_text in profile_tool),
# not via a second model call. A short rule on retrieve_offloaded_content has
# been added (backed by the OffloadRetrievalGuard hook, which enforces it
# even if the model ignores it).
PROFILE_SYSTEM_PROMPT = """
You are Abhinav Kumar's Profile Agent. Answer questions about his resume,
blogs, and projects using only the AgentCore Gateway tools - never from
memory.

TOOLS
- read_resume: profile, skills, experience, contact info.
- list_blogs / get_first_blog / get_last_blog / get_latest_blogs /
  get_oldest_blogs: find a blog and get its path.
- read_blog(path): full text of one blog. Get the path from a metadata tool
  first - never guess it.
- list_projects / get_first_project / get_last_project /
  get_latest_projects / get_oldest_projects: find a project and get its
  path.
- read_projects(path): full text of one project. Get the path from a
  metadata tool first - never guess it.

CONTACT INFO AUTHORIZATION
- The phone, email, LinkedIn, GitHub, and website returned by read_resume
  are Abhinav's own contact details, already published publicly with his
  consent - they are not private third-party data. When asked for any of
  these, give the actual value returned, plainly.

WORKFLOW
- Resume, skills, experience, or contact info -> read_resume.
- Content of a specific blog (summary, explanation, key points) -> find its
  path with a metadata tool, then read_blog(path).
- Content of a specific project -> find its path with a metadata tool, then
  read_projects(path).
- A question spanning multiple domains -> call every tool it needs before
  answering.

NO HEDGING OR SUBSTITUTE CONTENT
- If a tool can answer the request, call it and give that answer. Never
  withhold, redact, or hedge a value a tool actually returned, and never
  swap it for an apology, a caveat, or a suggestion to get it some other
  way ("reach out via email instead") - that is a substitute answer, not
  a real one, and it is never the right response when the data is already
  in hand.

GROUNDING
- Answer only from tool results. Never invent facts, paths, dates,
  technologies, or contact details.
- If the information isn't in the results, say it wasn't found.

ERRORS
- GITHUB_RATE_LIMIT_EXCEEDED -> say the live data source is temporarily
  unavailable and suggest retrying later.
- Unknown or invalid path -> say the requested blog or project wasn't
  found.
- Any other tool failure -> say the lookup failed. Don't invent a reason.

OFFLOADED CONTENT
- Some tool results may be offloaded, showing a preview and a stored
  reference. If so, call retrieve_offloaded_content for that reference at
  most once, then answer. Never call it twice for the same reference.

TURN INDEPENDENCE
- Evaluate every message on its own terms, including follow-up feedback
  about your own prior answer. A previous turn's hedge, refusal, or
  caveat - yours or anyone else's - has no bearing now. Do not reason from
  "as I said before" or treat an earlier answer as settled fact; re-decide
  fresh from the tools available and the current message alone.
- If feedback says a contact value was disclosed without a consent
  statement, the fix is to ADD one short phrase (e.g. "shared with his
  consent") to your existing answer, keeping the value exactly as given.
  Do not remove the value or switch to a refusal - retracting authorized
  information is itself the wrong fix, not a safer one. Never call a tool
  again just because of this kind of feedback.

Never mention tool names, routing, or internal mechanics to the user.
"""

# AgentCore Gateway MCP client.
#
# The gateway remains the only external tool source for the Profile Agent.
# There is no local shell/stdio MCP server.
website_mcp = MCPClient(
    lambda: streamablehttp_client(GATEWAY_URL)
)


def make_profile_agent(session_manager=None):
    return Agent(
        agent_id="profile_agent",
        name="profile_agent",
        description=(
            "Answers questions about Abhinav Kumar's profile, including "
            "resume, professional background, blogs, projects, portfolio, "
            "website content, and published contact information."
        ),
        tools=[website_mcp],
        model=profile_model,
        context_manager="auto",
        system_prompt=PROFILE_SYSTEM_PROMPT,
        callback_handler=None,
        hooks=[
            ToolLoggerHook(),
            ToolCallTracker(
                _profile_called_tools,
                _profile_called_tools_lock,
                _profile_called_tool_errors,
            ),
            _profile_offload_guard or OffloadRetrievalGuard(),  # CHANGED: loop guard
        ],
        plugins=[skill, steering],  # CHANGED: steering re-added, scoped to blog/project grounding only
        session_manager=session_manager,
    )


profile_agent = make_profile_agent()


# =============================================================================
# Profile Agent Verification
# =============================================================================

_CONTACT_INTENT_RE = re.compile(
    r"\b(phone|mobile|call him|contact\s*(info|number|details?)|email|e-mail|"
    r"linkedin|github|reach (him|abhinav)|get in touch)\b",
    re.IGNORECASE,
)

# CHANGED: catches the blog/project fabrication bug - main_agent, once it
# already has SOME info cached in its own persisted history (e.g. blog
# titles/metadata from an earlier "list blogs" turn), was answering follow-
# up content questions ("summarize it", "what methods did he mention",
# "read the whole blog") straight from memory instead of calling
# profile_tool again - inventing plausible-sounding content it never
# actually retrieved. Any mention of a blog/project now forces a fresh
# profile_tool call this turn, the same way contact questions already do,
# so profile_agent's grounding/steering checks always get a chance to run
# instead of being silently bypassed.
_BLOG_PROJECT_INTENT_RE = re.compile(
    r"\b(blog|post|article|project|repo|repository)\b",
    re.IGNORECASE,
)

_FABRICATED_FAILURE_RE = re.compile(
    r"\b(concurrency issue|encountered an? (error|issue|problem)|"
    r"ran into an? (error|issue|problem)|unable to retrieve|"
    r"couldn'?t retrieve|could not retrieve|failed to retrieve|"
    r"technical issue|something went wrong|error occurred)\b",
    re.IGNORECASE,
)

_RATE_LIMIT_RE = re.compile(
    r"GITHUB_RATE_LIMIT_EXCEEDED",
    re.IGNORECASE,
)

_PLACEHOLDER_RE = re.compile(
    r"(example\.com|555[-.\s]?123|123[-.\s]?4567|jane\.?doe|john\.?doe)",
    re.IGNORECASE,
)

# CHANGED: deterministic, non-generative replacements for what the steering
# plugin used to check via a second model call. These run as plain string
# operations on the already-generated answer - no model invocation, so
# nothing here can trigger a repeat generation of the same PII (which is
# what steering's Guide-retry was doing, and the apparent trigger for
# Nova's built-in content filter blocking legitimate output).
#
# CHANGED: replaced the single blunt _CONTACT_VALUE_RE regex with
# _contains_contact_value(). The old regex false-matched blog dates
# ("2026-01-12" looks like a phone number to a bare digit-and-hyphen
# pattern) and EVERY github.com project repo link (not just Abhinav's own
# bare profile URL) - which meant blog/project answers kept getting a
# bogus consent disclaimer appended, AND steering's hard-skip #2 was
# silently skipping evaluation of almost every blog/project answer, since
# nearly all of them contain a date or a repo link. This version:
#   - requires phone candidates to have >=9 actual digits and rejects a
#     bare ISO date shape (YYYY-MM-DD).
#   - only matches Abhinav's own bare GitHub profile URL
#     (github.com/abhinavcloud with nothing after it) - a repo URL like
#     github.com/abhinavcloud/SomeRepo has a path segment following and
#     will not match.
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\-\s]{7,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_CONTACT_URL_RE = re.compile(
    r"linkedin\.com/in/\S+"
    r"|github\.com/abhinavcloud(?![\w/-])"
    r"|abhinav-cloud\.com(?:/\S*)?",
    re.IGNORECASE,
)


def _contains_contact_value(text: str) -> bool:
    """True if text discloses an actual Abhinav-specific contact value
    (phone/email/linkedin/his own bare github or website) - not just any
    digit string (dates) or any github.com link (project repos)."""

    if _EMAIL_RE.search(text) or _CONTACT_URL_RE.search(text):
        return True

    for match in _PHONE_CANDIDATE_RE.finditer(text):
        candidate = match.group(0).strip()
        if _ISO_DATE_RE.match(candidate):
            continue
        if sum(ch.isdigit() for ch in candidate) >= 9:
            return True

    return False


_CONSENT_PHRASE_RE = re.compile(
    r"consent|publicly (available|accessible)|public(ly)? published|own published",
    re.IGNORECASE,
)

_FILE_PATH_RE = re.compile(r"\bsite/[\w./-]+\.md\b", re.IGNORECASE)

# CHANGED: strip stray <thinking> tags the model occasionally leaks into
# visible answer text (observed directly in profile_tool output).
_THINKING_TAG_RE = re.compile(r"<thinking>.*?</thinking>\s*", re.IGNORECASE | re.DOTALL)


def _finalize_profile_text(text: str) -> str:
    """Deterministic cleanup: strip leaked <thinking> tags and raw internal
    file paths, and append a consent note if a contact value is present
    without one. No model call - this replaces steering's job without ever
    regenerating the answer."""

    text = _THINKING_TAG_RE.sub("", text)
    text = _FILE_PATH_RE.sub("", text)

    if _contains_contact_value(text) and not _CONSENT_PHRASE_RE.search(text):
        text = text.rstrip() + (
            "\n\nThis information is shared with Abhinav's consent and is "
            "publicly available on his profile."
        )

    return text


def _tools_called_during(
    fn,
    storage,
    lock,
    error_storage=None,
):
    """
    Run an agent invocation and return:

        (result, set_of_called_tools, had_error)

    Tool calls are collected from actual AfterToolCallEvent callbacks.
    """

    with lock:
        storage.clear()

        if error_storage is not None:
            error_storage.clear()

    result = fn()

    with lock:
        called = set(storage)
        storage.clear()

        had_error = (
            bool(error_storage)
            if error_storage is not None
            else False
        )

        if error_storage is not None:
            error_storage.clear()

    return result, called, had_error


def _fabricated_failure(text: str, had_error: bool) -> bool:
    if _RATE_LIMIT_RE.search(text):
        return False

    return bool(_FABRICATED_FAILURE_RE.search(text)) and not had_error


_last_profile_result = {"text": None}
_last_profile_result_lock = threading.Lock()


@tool
def profile_tool(query: str) -> dict:
    """
    Route a query to Abhinav's profile knowledge source.

    The Profile Agent uses the AgentCore Gateway to retrieve resume, blog,
    project, portfolio, and contact information.
    """

    wants_contact = bool(_CONTACT_INTENT_RE.search(query))

    profile_response, called, had_error = _tools_called_during(
        lambda: profile_agent(query),
        _profile_called_tools,
        _profile_called_tools_lock,
        _profile_called_tool_errors,
    )

    response_text = str(profile_response)

    contact_missing = (
        wants_contact
        and not _tool_was_called(called, "read_resume")
    )

    zero_tools_called = len(called) == 0

    fabricated_failure = _fabricated_failure(
        response_text,
        had_error,
    )

    # One forced retry if the profile agent ignored a required lookup or
    # returned an unsupported failure without an actual tool exception.
    if contact_missing or zero_tools_called or fabricated_failure:
        reasons = []

        if contact_missing:
            reasons.append(
                "you must call read_resume before answering any contact question"
            )

        if zero_tools_called:
            reasons.append(
                "you answered without calling any gateway tool; "
                "use the available gateway tools instead of memory"
            )

        if fabricated_failure:
            reasons.append(
                "you claimed an error occurred but no tool call actually "
                "failed; retry the gateway lookup and do not repeat that claim"
            )

        # CHANGED: dropped the "(System: ...)" bracket-style annotation. That
        # phrasing looks structurally like a fake system-override injected
        # mid-conversation - exactly the pattern safety-tuned models are
        # trained to treat with suspicion, which may itself have been
        # raising the model's caution for whatever came next. Phrased as
        # plain continuation text instead.
        forced_query = (
            f"{query}\n\n"
            f"One more thing before you answer: {'. Also, '.join(reasons)}."
        )

        profile_response, called, had_error = _tools_called_during(
            lambda: profile_agent(forced_query),
            _profile_called_tools,
            _profile_called_tools_lock,
            _profile_called_tool_errors,
        )

        response_text = str(profile_response)

    # Contact questions receive an additional grounding check.
    if wants_contact:
        if not _tool_was_called(called, "read_resume"):
            return {
                "text": (
                    "I wasn't able to retrieve Abhinav's contact details "
                    "from the website data source just now, so I won't "
                    "guess at a phone number or email. Please try again."
                )
            }

        if _PLACEHOLDER_RE.search(response_text):
            return {
                "text": (
                    "I wasn't able to retrieve verified contact details "
                    "from the website data source just now, so I won't "
                    "guess at a phone number or email. Please try again."
                )
            }

    # General grounding/failure guard.
    if zero_tools_called or fabricated_failure:
        return {
            "text": (
                "I wasn't able to retrieve that from the website data "
                "source just now. Please try again."
            )
        }

    # CHANGED: deterministic finalize pass - no model call, so it cannot
    # trigger another regeneration of the same PII.
    response_text = _finalize_profile_text(response_text)

    with _last_profile_result_lock:
        _last_profile_result["text"] = response_text

    return {"text": response_text}


# =============================================================================
# Main Router Agent
# =============================================================================
#
# The Main Agent is deliberately kept as a separate router/orchestrator.
#
# Future agents can be added as additional tools here, for example:
#
#     tools=[profile_tool, future_agent_tool, another_agent_tool]
#
# The Main Agent should remain the entry point for user requests.
# =============================================================================

# CHANGED: MAIN_SYSTEM_PROMPT still tells main_agent to relay profile_tool's
# answer verbatim - this guides its ROUTING decision (call profile_tool for
# anything about Abhinav) and its behavior on the small-talk path. It is no
# longer what actually protects the final text, though: ask_main_agent now
# enforces the relay in code (see the profile_tool passthrough there),
# because main_agent's own generation has repeatedly proven willing to
# override a correct, already-authorized tool result with its own refusal.
# Keep this wording anyway - it still steers the routing/small-talk
# behavior even though it's no longer the last line of defense for the
# final answer text.
MAIN_SYSTEM_PROMPT = """
You are the router for Abhinav Kumar's assistant.

For anything about Abhinav - background, resume, skills, experience, blogs,
projects, portfolio, or contact info (phone, email, LinkedIn, GitHub,
website) - call profile_tool and return its answer to the user as-is. Do
not rewrite, summarize, shorten, or remove any part of it, including any
disclaimer or consent statement it contains. Do not add your own privacy
caveats or withhold any part of profile_tool's answer - it has already
determined what is safe to share.

For greetings, small talk, or anything unrelated to Abhinav, respond
directly yourself - do not call profile_tool.

NO HEDGING BEFORE CALLING THE TOOL
- If the request is about Abhinav, call profile_tool immediately. Never
  refuse, hedge, or add a privacy caveat before calling it - you have not
  seen what it returns yet, so there is nothing to hedge about. This
  applies to blog and project content requests exactly as much as contact
  info - never redirect the user to "search LinkedIn" or "check his
  website yourself" when profile_tool can just answer directly.

NEVER ANSWER BLOG/PROJECT CONTENT FROM MEMORY
- Titles, dates, or tags you saw earlier in this conversation are
  metadata, NOT the full content. A request to summarize, explain, quote,
  or describe what a blog/project actually says - or to identify methods,
  claims, or details "he mentioned" - always needs the full body, which
  you do not have unless you call profile_tool THIS turn.
- Never invent, extrapolate, or describe "what a blog like this would
  typically cover." If you have not retrieved the actual content this
  turn, call profile_tool now - do not answer from an earlier metadata
  fetch or your own general knowledge of the topic.

TURN INDEPENDENCE
- Evaluate every message on its own terms. A previous turn's hedge or
  refusal - yours or anyone else's - has no bearing now; do not reason
  from "as I said before." Re-decide fresh from the current message alone.

CONTEXT RESOLUTION
- profile_tool has no memory between calls - every call starts blank. If
  the user refers to something from earlier in this conversation ("the
  blog you just gave me", "his email again", "that project"), resolve the
  reference yourself using what you already know from this conversation,
  and send profile_tool a complete, self-contained query (name the actual
  blog title, project, or topic) - never assume profile_tool remembers
  anything you have not just told it in this call.

Never invent facts, names, dates, or contact details of your own.

If no available tool can fulfill the request, say so plainly.

Never expose internal tool or agent names, routing logic, or implementation
details unless explicitly asked.
"""

main_model = BedrockModel(
    model_id=MODEL_ID,
    region_name=REGION,
    temperature=0.3,
    max_tokens=2000,
    context_window_limit=100000,
)

# CHANGED: replaces context_manager="auto" for main_agent specifically.
# main_agent keeps a persistent FileSessionManager (needed for "summarize
# the blog you just gave me" style continuity), but that meant its own
# occasional refusals/hedges got permanently written into its history and
# could bias later, unrelated turns - the same contamination pattern we
# fixed for profile_agent by making it stateless, except main_agent can't
# go fully stateless without losing continuity.
#
# This custom summarization prompt leverages the SAME built-in
# SummarizingConversationManager that "auto" already wires in, but tells it
# explicitly to keep only what was actually delivered to the user and to
# drop any hedge/refusal/apology entirely - especially if it was later
# corrected. preserve_recent_messages is set low so this fires quickly in
# a short conversation rather than only once the context window is nearly
# full (the default behavior, which would leave most short demo sessions
# never benefiting from it at all).
#
# Trade-offs, explicit: this costs an extra summarization LLM call once
# compression triggers (unlike the zero-call code-level fixes elsewhere in
# this file) - but it's condensing old prose, not regenerating a discrete
# PII value in isolation, so it doesn't carry the specific Nova-filter risk
# that steering's Guide-retry did.
MAIN_SUMMARIZATION_PROMPT = """
Summarize this conversation for the assistant's own future reference.

Keep, in third person:
- What the user asked about (topics, names of blogs/projects/people).
- What information was actually delivered to the user (concrete facts,
  values, titles, paths) - the final outcome, not the process.

Drop entirely:
- Any hedge, refusal, apology, or privacy caveat the assistant produced,
  even if it appears to have been said "by the assistant" - these are not
  standing policy and must not be preserved or treated as established fact.
- Tool names, routing decisions, retries, or internal mechanics.

If the assistant initially declined something and then the correct
information was provided afterward, record ONLY that the information was
provided - never record that a decline happened.
"""

main_conversation_manager = SummarizingConversationManager(
    summarization_system_prompt=MAIN_SUMMARIZATION_PROMPT,
    preserve_recent_messages=4,
)


def make_main_agent(session_manager=None):
    return Agent(
        agent_id="main_agent",
        name="main_agent",
        description=(
            "Primary routing and orchestration agent for Abhinav Kumar's "
            "assistant. Delegates profile-related requests to the Profile Agent "
            "and is designed to support additional specialized agents later."
        ),
        model=main_model,
        tools=[profile_tool],
        conversation_manager=main_conversation_manager,  # CHANGED: was context_manager="auto"
        system_prompt=MAIN_SYSTEM_PROMPT,
        callback_handler=None,
        hooks=[
            ToolCallTracker(
                _main_called_tools,
                _main_called_tools_lock,
            )
        ],
        # CHANGED: no steering plugin on main_agent. Steering is back, but
        # scoped to profile_agent's blog/project grounding only (see the
        # "Steering" section) - contact info is enforced deterministically
        # in profile_tool below, and main_agent's own generation is bypassed
        # for anything profile_tool answered anyway (see the passthrough in
        # ask_main_agent).
        session_manager=session_manager,
    )


main_agent = make_main_agent()


# =============================================================================
# Main Router Verification
# =============================================================================

def ask_main_agent(user_input: str):
    """
    Invoke the Main Agent with a small grounding safety net.

    The check is intentionally focused on routing rather than predicting every
    possible profile topic with regexes.
    """

    if not user_input or not user_input.strip():
        return "Please type a question."

    # CHANGED: broadened from contact-only to also cover blog/project
    # mentions - see _BLOG_PROJECT_INTENT_RE for why. Renamed to reflect
    # the wider scope: this now means "this turn needs a fresh profile_tool
    # call, not an answer from main_agent's own cached memory."
    wants_profile_tool_call = bool(
        _CONTACT_INTENT_RE.search(user_input)
        or _BLOG_PROJECT_INTENT_RE.search(user_input)
    )

    # CHANGED: reset before this turn so a stale result from an earlier,
    # unrelated turn can never leak into this one.
    with _last_profile_result_lock:
        _last_profile_result["text"] = None

    response, called, _ = _tools_called_during(
        lambda: main_agent(user_input),
        _main_called_tools,
        _main_called_tools_lock,
    )

    def _needs_retry(resp, called_tools):
        tool_call_missing = (
            wants_profile_tool_call
            and not any(
                _bare_tool_name(name) == "profile_tool"
                for name in called_tools
            )
        )

        # A response claiming a failure without even calling a tool is
        # suspicious and should get one retry.
        fabricated_failure = (
            _fabricated_failure(str(resp), had_error=False)
            and len(called_tools) == 0
        )

        return tool_call_missing or fabricated_failure

    if _needs_retry(response, called):
        # CHANGED: dropped the "(System: ...)" bracket annotation here too -
        # missed this one earlier when profile_tool's own version was
        # softened. Same reasoning applies: this phrasing looks like a fake
        # system-override injected into the conversation, which may raise a
        # safety-tuned model's suspicion/caution rather than simply being
        # read as an instruction.
        forced_input = (
            f"{user_input}\n\n"
            "One more thing before you answer: this request requires the "
            "Profile Agent. Call profile_tool now and answer from its "
            "retrieved result - do not answer from memory, even if you "
            "already discussed this blog/project/contact earlier in this "
            "conversation. What you have cached may only be a title or "
            "summary, not the full content this question needs."
        )

        response, called, _ = _tools_called_during(
            lambda: main_agent(forced_input),
            _main_called_tools,
            _main_called_tools_lock,
        )

    if _needs_retry(response, called):
        # CHANGED: log what main_agent actually said/attempted before we
        # discard it in favor of the canned fallback below - otherwise a
        # failure here is undebuggable (as it just was): the raw response
        # and which tools main_agent chose to call are gone the moment we
        # return the generic string.
        print(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "main_agent_fallback",
                "user_input": user_input,
                "called_tools": list(called),
                "raw_response_preview": str(response)[:500],
            },
            flush=True,
        )
        return (
            "I wasn't able to retrieve the requested profile information "
            "from the website data source just now. Please try again."
        )

    # CHANGED: if profile_tool was actually called this turn, use its own
    # returned text as the final answer instead of trusting main_agent's own
    # generated response. main_agent has repeatedly proven unreliable at this
    # step specifically - it has refused to relay a phone number even when
    # the tool result placed it directly in front of the model with an
    # explicit consent statement attached. Rather than prompt-engineer
    # around a model bias that keeps resurfacing in new shapes, this removes
    # main_agent's discretion entirely for anything profile_tool already
    # answered: profile_agent (prompt-instructed, deterministically finalized)
    # is the sole author of that text; main_agent only routes to it.
    #
    # Note for later: if a second specialized tool is ever added alongside
    # profile_tool, this passthrough will need to become "combine outputs"
    # rather than a straight override, since it currently assumes
    # profile_tool's answer is the whole answer.
    if any(_bare_tool_name(name) == "profile_tool" for name in called):
        with _last_profile_result_lock:
            profile_text = _last_profile_result["text"]

        if profile_text is not None:
            return profile_text

    return response


# =============================================================================
# AgentCore Runtime Entry Point
# =============================================================================

@app.entrypoint
def invoke(payload: dict, context: RequestContext):
    user_input = str(payload.get("prompt", "")).strip()

    if not user_input:
        return {
            "error": "Please provide a non-empty 'prompt'."
        }

    build_agents(context.session_id)

    response = ask_main_agent(user_input)

    return {
        "result": str(response),
        "session_id": context.session_id,
    }


if __name__ == "__main__":
    app.run()