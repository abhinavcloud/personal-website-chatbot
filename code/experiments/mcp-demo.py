import logging
import os
import json
import uuid


import boto3
from dotenv import load_dotenv

from strands import Agent, tool
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager


from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient

load_dotenv(".env")
logging.getLogger("mcp").setLevel(logging.CRITICAL)

# Load environment variables
MODEL = os.getenv("MODEL_ID")
REGION = os.getenv("REGION")

# Connect to the AWS MCP server
git_mcp_filtered_tools = MCPClient(
    lambda: streamablehttp_client("https://gitmcp.io/abhinavcloud/PersonalWebsite"),
   # tool_filters={
   #     "allowed": [ "search_PersonalWebsite_code"]
   # },
)

# "fetch_PersonalWebsite_docs", "search_PersonalWebsite_docs",

SYSTEM_PROMPT = '''
- You are a repsitory assistant of Abhinav's personal website
- Your job is to find abhinav's recent blogs or projects
- When searching for blogs, look for markdown (.md) files located in site/blog/posts.
- When searching for projects, look for markdown (.md) files located in site/projects/project.
- Keep running the agent loop with different
- Do not use wildcard or glob patterns; search by folder path or keywords instead.
- If the user asks what are the recent blogs that abhinav as written or published the navigate to folder site/blog/posts to get the required information
- If the user asks what are the recent projects that abhinav has worked on then navigate to folder site/projects/project to get the required information
- Fetch all the requried blogs or projects and then answer based on that.
'''

bedrock_model = BedrockModel(
        model_id=MODEL,
        region_name=REGION,
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
    )

session_id = f"session-{uuid.uuid4().hex}"
session_manager = FileSessionManager(session_id=session_id, storage_dir="./experiments/mcp_sessions")


agent = Agent(
    tools=[git_mcp_filtered_tools],
    system_prompt = SYSTEM_PROMPT,
    model=bedrock_model,
    session_manager=session_manager
    )

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            break
        response = agent(user_input)
        print(f"Assistant: {response}")

if __name__ == "__main__":
    main()
