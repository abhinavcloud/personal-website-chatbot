import os
import json
import boto3
import uuid
import logging
from dotenv import load_dotenv
from strands import Agent, tool
from strands_tools import retrieve
from strands.models import BedrockModel
from strands.session.file_session_manager import FileSessionManager
from strands.tools.mcp import MCPClient
from mcp.client.streamable_http import streamablehttp_client

load_dotenv(".env")

# Connect to the GitHub Repositoru MCP Client
git_mcp = MCPClient(
    lambda: streamablehttp_client("https://gitmcp.io/abhinavcloud/PersonalWebsite")
    )


SYSTEM_PROMPT = """
You are Buddy, Abhinav's personal assistant. You speak as someone who already knows
Abhinav's background, projects, blogs, skills, and experience.

Instructions:
- Answer strictly based on the information available in the knowledge base or the github repository. 
- If you cant find relevant content in the github repository then check the knowledge base for relevant information.
- If the information is not available, respond with "I don't know."
- In case the ask is about Abhinav's personal information, his certifications, career, skills, companies, experience or any other information check the chunks in KB related to resume.
- In case the ask is about Abhinav's projects, check the chunks in KB related to projects.
- In case the ask is about Abhinav's blogs, check the chunks in KB related to blogs.

"""

bedrock_model = BedrockModel(
        model_id=os.getenv("MODEL_ID"),
        region_name=os.getenv("REGION"),
        temperature=0.3,
        max_tokens=2000,
        context_window_limit=100000,
    )

session_id = f"session-{uuid.uuid4().hex}"
session_manager = FileSessionManager(session_id=session_id, storage_dir="./sessions")

agent = Agent(
    tools=[git_mcp, retrieve],
    system_prompt=SYSTEM_PROMPT,
    model=bedrock_model,
    context_manager="auto",
    session_manager=session_manager
)

def main():
    print("Welcome to Abhinav's personal assistant. Type 'exit' to quit.")
    while True:
        user_input = input("You: ")
        if user_input.lower() == "exit":
            break
        response = agent(user_input)
        print(type(response))

if __name__ == "__main__":
    main()