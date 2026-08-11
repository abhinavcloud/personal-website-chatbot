"""
Demonstrates tool filtering — listing all tools from an MCP server
and then filtering down to only the ones you need.
"""

import logging
logging.getLogger("mcp").setLevel(logging.CRITICAL)

from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.tools.mcp import MCPClient



# Connect to the AWS MCP server
git_mcp = MCPClient(
    lambda: streamablehttp_client("https://gitmcp.io/abhinavcloud/PersonalWebsite")
)



agent = Agent(
    tools=[git_mcp],
    system_prompt="You are a Git assistant. Only use Git-related tools."
)

print("Agent created with full tool set.")
print(f"Available tools: {agent.tool_names}")   

git_mcp_filtered_tools = MCPClient(
    lambda: streamablehttp_client("https://gitmcp.io/abhinavcloud/PersonalWebsite"),
    tool_filters={
        "allowed": ["fetch_PersonalWebsite_docs", "search_PersonalWebsite_docs", "search_PersonalWebsite_code"]
    },
)

agent_filtered = Agent(
    tools=[git_mcp_filtered_tools],
    system_prompt="You are a Git assistant. Only use Git-related tools."
)

print("Agent created with filtered tool set.")
print(f"Available tools: {agent_filtered.tool_names}")

SYSTEM_PROMPT = '''
- You are a repsitory assistant of Abhinav's personal website
- Your job is to find abhinav's recent blogs or projects
- The blogs are in md format in the folder site/blog/posts
- The projects are in md format in the folder site/projects/project
- If the user asks what are the recent blogs that abhinav as written or published the navigate to folder site/blog/posts to get the required information
- If the user asks what are the recent projects that abhinav has worked on then navigate to folder site/projects/project to get the required information
- Fetch all the requried blogs or projects and then answer based on that.
'''

agent_fetch_info_from_website = Agent(
    tools=[git_mcp_filtered_tools],
    system_prompt="You are a repository assistant. "
    )
