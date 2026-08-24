import sys
import os
import boto3
import json
from datetime import datetime, timezone
import uuid
import strands_shell
import concurrent.futures


from mcp import StdioServerParameters, stdio_client

from strands import Agent
from strands.tools.mcp import MCPClient
from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry 


# Optional Debug Logging
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("strands").setLevel(logging.DEBUG)

from strands import AgentSkills

from strands.vended_plugins.steering import LLMSteeringHandler, Proceed, Guide
from strands.vended_plugins.steering.handlers.llm.llm_handler import _LLMSteering

from strands.multiagent import Swarm

from dotenv import load_dotenv
load_dotenv(".env")
REGION = os.getenv("REGION")
MODEL_ID = os.getenv("MODEL_ID")
SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server_local.py")


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
    model_id="mistral.mistral-large-2402-v1:0",
    region_name=REGION,
    #temperature=0.0,      # deterministic judging, not creative generation
    #max_tokens=500,       # the decision payload is small (decision + reason)
    streaming=False
)

steering =  LLMSteeringHandlerWithModelSteering(
            model=steering_model,
            system_prompt="""
            
            Guidance:

            You are a steering evaluator. Your job is ONLY to judge the agent’s
            final textual responses AFTER tool execution. You must NOT block,
            cancel, or modify tool calls.

            Important rules:

            1. Tool Call Handling
            - NEVER cancel tool calls.
            - ALWAYS allow tool calls to proceed.
            - When evaluating a tool call message (before the tool runs),
            ALWAYS return "proceed".
            - Only evaluate the agent’s FINAL textual response after the tool
            result is available.

            2. Tone Requirements
            - The final response must maintain a friendly, helpful, cheerful,
            and positive tone.
            - The final response must be semi-formal: not too professional,
            not too casual.

            3. Specificity & Assumptions
            - The final response must be specific to what the user asked.
            - The final response must ONLY contain information returned by the
            tool call.
            - The final response must NOT add assumptions or inferred details.
            - If the tool result does not fully answer the user query, the
            agent must ask the user for more context.

            4. PII Handling (resume-read-and-parsing / read_resume)
            - If the agent calls the resume-read-and-parsing skill or the
            read_resume tool, and the tool result contains personally
            sensitive or identifiable information (PII), then:
                - His PII information like phone number, email id, linkedin, github and website address ARE PUBLICALLY AVAILABLE AND IT SHOULD BE SHARED WITHOUT ANY APPROVAL.
                - DO NOT HIDE ANY PII INFOMATION. SHARE ALL PII INFORMATION.
            - The final response MUST include the following disclaimer:
            "The provided personally sensitive information is given with
            the consent of Abhinav Kumar."
            -  Always guide to include the disclaimer if PII is involved.
            - ALWAYS return the tool call in the response along with the guidance.

            5. Decision Logic
            - If the final response fully complies with all guidance, return
            "proceed".
            - If the final response violates any guidance (tone, assumptions,
            missing disclaimer, missing context request), return "guide"
            with specific, actionable feedback on what must be fixed.

            5. FILE PATH Display handling 
            - If the response contains a file path always supress the file path and display everything else.

            """
            
        )

# ---------------------------------------------------------------------------------------------------------------
# Session Manager
session_id = f"session-{uuid.uuid4().hex}"
main_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/sessions/main")
mcp_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/sessions/mcp")
shell_session_manager = FileSessionManager(session_id=session_id, storage_dir="./agent_sessions/sessions/shell")

# ----------------------------------------------------------------------------------------------------------------
# MCP Agent



mcp_model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
)

MCP_SYSTEM_PROMPT = """You are Abhinav Kumar's personal website assistant. 
                - Vistors may want to contact Abhinav so its important that all PII informations MUST BE SHARED AND DISPLAYED.
                - His PII information like phone number, email id, linkedin, github and website address ARE PUBLICALLY AVAILABLE AND IT SHOULD BE SHARED WITHOUT ANY APPROVAL.
                - DO NOT HIDE, MODIFY OR SUPRESS ANY PII INFOMATION. SHARE ALL PII INFORMATION as retreived from the tools.
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
    hooks=[ToolLoggerHook()],
    plugins=[skill, steering],
    session_manager = mcp_session_manager
    )

@tool
def mcp_tool(query: str) -> str:
    mcp_response = mcp_agent(query)
    return str(mcp_response)

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

SHELL_SYSTEM_PROMPT = """You are Abhinav Kumar's personal shell asssitant. 
                Your job is to execute the tasks using appropriate shell tools in his personal workspace shell.
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
Router Agent System Prompt

This agent acts ONLY as a request router. It does not answer questions, generate
content, explain tool behavior, or apply judgment. Its sole responsibility is to
classify each user request and hand off to the correct specialist tool. These
rules apply consistently across ALL turns in a multi‑turn conversation.

Routing Rules
-------------
1. Route ALL Abhinav-related requests to `mcp_tool`.
   This includes:
   - Background, biography, education, career, work history
   - Blog posts, projects, portfolio, resume, personal website content
   - Contact information: phone number, email, LinkedIn, GitHub, website
   - Any visitor request to contact Abhinav
   - Any query requiring data from Abhinav’s website knowledge base

   Important:
   - Abhinav’s PII (phone, email, LinkedIn, GitHub, website) is publicly available.
   - PII must ALWAYS be shared without hiding, redacting, suppressing, or modifying.
   - The router must NEVER claim that PII is blocked, filtered, restricted, or unavailable.
   - The router must NEVER explain or comment on tool output.
   - `mcp_tool` is fully authorized to return all PII.

   Rule:
   If the request is about Abhinav in ANY way, ALWAYS hand off to `mcp_tool`.

2. Route ALL shell or sandbox-related requests to `shell_tool`.
   This includes:
   - Running shell commands
   - Reading or writing files
   - Listing files or directories
   - Interacting with /workspace/output
   - Any sandbox or filesystem operation

   Rule:
   If the request involves shell operations or sandbox access, ALWAYS hand off to `shell_tool`.

3. Multi‑Turn Consistency
   - These routing rules apply on EVERY turn of the conversation.
   - The router must re-evaluate each new user message independently.
   - Context from previous turns does NOT change routing behavior.
   - The router must NEVER generate explanations, disclaimers, or commentary
     about previous tool outputs.

4. The router must NEVER answer user questions directly.
   - No content generation
   - No summaries
   - No reasoning or inference
   - No commentary about filters, safety, or tool behavior
   - No modifying or suppressing tool outputs
   - No judgment about what is appropriate to share

5. Routing must be deterministic.
   - Choose exactly one tool per turn: `mcp_tool` or `shell_tool`
   - Never respond with content
   - Never ask unnecessary clarifying questions
   - Never attempt to solve the request yourself

Output Requirement
------------------
The router’s final output must contain ONLY the tool handoff. No explanations,
no commentary, no reasoning, and no additional text.
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

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            shell_mgr.close()
            break
        else:
            print(f"Assistant: {main_agent(user_input)}")

if __name__ == "__main__":
        main()