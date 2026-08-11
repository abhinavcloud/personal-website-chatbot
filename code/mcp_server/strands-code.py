import sys
import os
import boto3
import json
import uuid
from mcp import StdioServerParameters, stdio_client
from strands import Agent
from strands.tools.mcp import MCPClient
from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands.agent.conversation_manager import SlidingWindowConversationManager


from dotenv import load_dotenv
load_dotenv(".env")
REGION = os.getenv("REGION")
MODEL_ID = os.getenv("MODEL_ID")
SERVER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server_local.py")

website_mcp = MCPClient(
    lambda: stdio_client(
        StdioServerParameters(
            command=sys.executable,   # guarantees same venv/interpreter
            args=[SERVER_PATH],       # absolute path, not relative
        )
    )
)
SYSTEM_PROMPT = """
You are an AI assistant for Abhinav Kumar's personal website.
The Personal Website MCP server is the source of truth for his blogs and projects.

- When asked what blogs Abhinav has written:
    1. Call list_blogs().
    2. Use the returned results.
    3. Never invent blog titles.

- When asked about the contents of a blog:
    1. Identify the relevant blog.
    2. Call read_blog() with its exact path.
    3. Base your answer on the retrieved blog content.
    4. Do not infer content from filenames.

- When asked what projects Abhinav has worked on:
    1. Call list_projects().
    2. Use the returned results.
    3. Never invent project names.

- When asked about the contents of a project:
    1. Identify the relevant project.
    2. Call read_projects() with its exact path.
    3. Base your answer on the retrieved project content.
    4. Do not infer content from filenames.

- When asked about Abhinav resume, including his contact details, linkedin profile, personal website, github profile, skills, certifications, work experience or companies he has worked on:
    1. Identify the relevant resume document.
    2. Call read_resume() with its exact path.
    3. Base your answer on the retreived resume content.
    4. Do not give any response which is not mentioned in the resume content

"""

bedrock_model = BedrockModel(
        model_id=MODEL_ID,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
)


session_id = f"session-{uuid.uuid4().hex}"
session_manager = FileSessionManager(session_id=session_id, storage_dir="./mcp_server/sessions")

conversation_manager = SlidingWindowConversationManager(window_size=30)

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            break
        #response = agent(user_input)
        print(f"Assistant: {agent(user_input)}")

with website_mcp:
    tools = website_mcp.list_tools_sync()
    print("MCP tools loaded:")
    for t in tools:
        print(t)

    agent = Agent(
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        model=bedrock_model,
        conversation_manager=conversation_manager,
        session_manager=session_manager,
    )

    if __name__ == "__main__":
        main()