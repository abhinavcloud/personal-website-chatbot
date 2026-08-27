# V2 Code

import sys
import os
import boto3
import json
from datetime import datetime, timezone
import uuid
import concurrent.futures


from mcp import StdioServerParameters, stdio_client # STDIO CLIENT

from strands import Agent
from strands.tools.mcp import MCPClient # MCP Client
from strands import Agent, tool # Agent and Tools 
from strands.models import BedrockModel # Models
import strands_shell # SHELL Tools
from strands.session.file_session_manager import FileSessionManager # Session Manager 
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry # Hooks


# Optional Debug Logging
#import logging
#logging.basicConfig(level=logging.DEBUG)
#logging.getLogger("strands").setLevel(logging.DEBUG)

from strands import AgentSkills # SKills Plugin

from strands.vended_plugins.steering import LLMSteeringHandler, Proceed, Guide # Steering Plugins
from strands.vended_plugins.steering.handlers.llm.llm_handler import _LLMSteering # Steering Plugins


from dotenv import load_dotenv
load_dotenv(".env") # Loading Env Variables

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

        print(f"\n[HOOK] Tool Executed:{event.tool_use.get("name")}")
       

        #print(log_entry)

        return event.result


last_contact_result = {"data": None}

class ContactCaptureHook(HookProvider):
    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(AfterToolCallEvent, self.after_tool_call)

    def after_tool_call(self, event: AfterToolCallEvent):
        if event.tool_use.get("name") == "read_resume":
            result = event.result
            if isinstance(result, dict) and "contact" in result:
                last_contact_result["data"] = result["contact"]
        return event.result

# -------------------------------------------------------------------------------------------------------------
# Skills Plugin
# -------------------------------------------------------------------------------------------------------------

skill = AgentSkills(skills="./MCP_based-retreival/skills/")

# ------------------------------------------------------------------------------------------------------------

# ------------------------------------------------------------------------------------------------------------
# Steering Plugin
# ------------------------------------------------------------------------------------------------------------

class LLMSteeringHandlerWithModelSteering(LLMSteeringHandler):

    async def steer_before_tool(self, *, agent, tool_use, **kwargs):
        # Tool calls always proceed — no LLM judge needed here.
        # All evaluation happens after the model's final response.
        return Proceed(reason="Tool calls are never blocked; steering only evaluates final responses.")



    async def steer_after_model(self, *, agent, message, stop_reason, **kwargs):
        # Only bother evaluating actual final answers, not intermediate
        # tool_use turns — nothing textual to judge there yet.
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
        wrong tone) if it does not.
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
    #temperature=0.0,      # deterministic judging, not creative generation
    #max_tokens=500,       # the decision payload is small (decision + reason)
    streaming=False
)

steering =  LLMSteeringHandlerWithModelSteering(
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

            3. Grounding
            - The final response must answer only what the user asked.
            - The final response must contain only information returned by the tool
            call — no invented or inferred details.
            - If the tool result doesn't fully answer the query, the agent must ask
            the user for clarification rather than filling gaps itself.

            4. Abhinav's own contact information
            - Abhinav's own phone number, email, LinkedIn, GitHub, and website are
            public information he has chosen to publish on his site, and may be
            shared with visitors without redaction.
            - Any response containing Abhinav's own contact details must include this
            disclaimer: "This contact information is shared with Abhinav Kumar's
            consent."
            - This rule applies ONLY to Abhinav's own information. If a tool result
            contains personal information belonging to anyone else (e.g. a third
            party's resume content), that information must NOT be disclosed under
            this rule, and the disclaimer must not be attached to it — it does not
            have Abhinav's consent behind it.

            5. File paths
            - Suppress raw file paths in the final response; describe the result
            instead (e.g. "saved to your output folder").

            6. Decision
            - "proceed" if all of the above are satisfied.
            - Otherwise "guide" with specific, actionable feedback (what's missing:
            disclaimer, tone, an unscoped PII disclosure, an assumption not
            supported by the tool result, etc.).

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
                    - Abhinav's own contact information — phone number, email, LinkedIn, GitHub,
                    website — is public and may be shared with visitors who ask for it,
                    without redaction, exactly as returned by the tools.
                    - Do not extend this rule to anyone else's personal information. If a tool
                    result contains PII belonging to someone other than Abhinav, do not
                    disclose it.
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
    system_prompt = MCP_SYSTEM_PROMPT,
    callback_handler=None,
    hooks=[ToolLoggerHook(), ContactCaptureHook()],
    plugins=[skill, steering],
    session_manager = mcp_session_manager
    )

@tool
def mcp_tool(query: str) -> dict:
    mcp_response = mcp_agent(query)

    # Try to surface structured contact data if this turn touched read_resume
    # contact_data = extract_contact_fields_from_tool_results(mcp_agent)  # see #2

    return {
        "text": str(mcp_response),
        #"contact_data": contact_data,  # None if not applicable this turn
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


# Shell Manager for Shell based tools

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
    safe_path = f"/workspace/output/{os.path.basename(path)}"
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
        system_prompt = SHELL_SYSTEM_PROMPT,
        context_manager="auto",
        callback_handler=None,
        description="Executes shell commands and reads/writes files in Abhinav's personal sandbox workspace.",
        hooks=[ToolLoggerHook()],
        plugins=[skill],
        session_manager = shell_session_manager
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

                2. Route to `shell_tool` for anything involving shell or sandbox
                operations:
                - Running shell commands
                - Reading, writing, or listing files
                - Interacting with /workspace/output
                - Any sandbox or filesystem operation

                3. A single request may require both tools in sequence — e.g. "summarize
                his blog and save it to a file" means: call `mcp_tool` to get the
                content, then call `shell_tool` to save it. Chaining tools across a
                turn is still routing, not content generation. You may not summarize,
                compose, or generate the content yourself, and you may not stop after
                the first tool and hand the rest back to the user as something they
                should do manually.

                4. Choose the tool(s) the request actually needs — never zero. If a
                request needs only one tool, call only that one.

                5. Never produce your own explanation, reasoning, apology, caveat, or
                substitute content (including code snippets, `<thinking>` text, or
                framing like "you could do this yourself"). If a tool can fulfill the
                request, call it — do not describe how the user could do it instead.

                6. Your visible output is limited to the result of the tool call(s) —
                the last tool's result if chaining. No preamble, no commentary about
                previous tool outputs, no meta-discussion of routing.

                7. If no available tool can fulfill any part of the request, say so in
                one plain sentence, and nothing else.
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
    agent_id = "main_agent",
    name="main_agent",
    model=main_model,
    tools=[mcp_tool, shell_tool],
    context_manager="auto",
    system_prompt = MAIN_SYSTEM_PROMPT,
    callback_handler=None,
    session_manager = main_session_manager
    )


# ---------------------------------------------------

def format_contact(c: dict) -> str:
    emails = ", ".join(c["email"]) if isinstance(c["email"], list) else c["email"]
    return (
        f"Name: {c['name']}\n"
        f"Title: {c['title']}\n"
        f"Location: {c['location']}\n"
        f"Phone: {c['phone']}\n"
        f"Email: {emails}\n"
        f"LinkedIn: {c['linkedin']}\n"
        f"GitHub: {c['github']}\n"
        f"Website: {c['website']}\n"
        f"(This contact information is shared with Abhinav Kumar's consent.)"
    )

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            shell_mgr.close()
            break

        response = main_agent(user_input)

        if last_contact_result["data"]:
            print("Assistant:")
            print(format_contact(last_contact_result["data"]))
            last_contact_result["data"] = None
        else:
            print(f"Assistant: {response}")

if __name__ == "__main__":
        main()