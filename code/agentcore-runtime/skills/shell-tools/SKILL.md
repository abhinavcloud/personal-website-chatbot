---
name: shell-tools
description: Perform sandboxed shell operations (read, write, list) inside the allowed workspace filesystem.
allowed-tools: sandbox_write, sandbox_read, sandbox_list
---
# Shell Operations on Workspace Filesystem

1. PII may be saved to files; this is authorized.
2. sandbox_write → write or create a file.
3. sandbox_read → read a file.
4. sandbox_list → list files in the sandbox.