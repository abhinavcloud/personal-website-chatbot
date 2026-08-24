Contributions are welcome! To contribute:

1. Fork the repository and create a feature branch (`git checkout -b feature/my-change`)
2. Make your changes, keeping agent system prompts and steering rules in sync with any behavioral changes
3. Test locally by running `python strands-code.py` and exercising both the `mcp_agent` and `shell_agent` paths
4. Open a pull request with a clear description of what changed and why

For larger changes (new agents, new tools, changes to the steering rules or Swarm config), please open an issue first to discuss the approach before submitting a PR.

**Guidelines:**
- Keep agent system prompts concise and single-purpose — avoid letting one agent absorb another's responsibilities
- Any new tool that touches the filesystem should stay bound to the sandbox pattern used by `ShellManager`
- Log new tool calls through `ToolLoggerHook` for consistency
- Don't commit `.env`, AWS credentials, or session data under `agent_sessions/`