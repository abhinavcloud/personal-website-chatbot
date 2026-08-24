# Installation Guide

This guide walks through setting up **Abhinav's Personal Website Assistant** from a clean machine — installing Python, creating a virtual environment, and installing dependencies.

## Table of Contents

- [1. Prerequisites](#1-prerequisites)
- [2. Install Python](#2-install-python)
  - [macOS](#macos)
  - [Windows](#windows)
  - [Linux (Debian/Ubuntu)](#linux-debianubuntu)
- [3. Clone the Repository](#3-clone-the-repository)
- [4. Create a Virtual Environment](#4-create-a-virtual-environment)
- [5. Activate the Virtual Environment](#5-activate-the-virtual-environment)
- [6. Install Dependencies](#6-install-dependencies)
- [7. Configure Environment Variables](#7-configure-environment-variables)
- [8. Verify the Installation](#8-verify-the-installation)
- [9. Deactivating / Removing the Environment](#9-deactivating--removing-the-environment)
- [Troubleshooting](#troubleshooting)

---

## 1. Prerequisites

- **Python 3.10 or later**
- **pip** (bundled with modern Python installers)
- **AWS credentials** with access to Amazon Bedrock (for the LLM models used by the agents and steering evaluator)
- **git** (to clone the repository)

Check whether Python is already installed:

```bash
python3 --version
```

If this prints `Python 3.10.x` or higher, skip to [Step 3](#3-clone-the-repository).

## 2. Install Python

### macOS

Using [Homebrew](https://brew.sh/):

```bash
brew install python@3.12
```

Verify:

```bash
python3 --version
```

### Windows

1. Download the installer from [python.org/downloads](https://www.python.org/downloads/)
2. Run the installer and **check "Add python.exe to PATH"** before clicking Install
3. Verify in a new terminal (Command Prompt or PowerShell):

```powershell
python --version
```

### Linux (Debian/Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip -y
```

Verify:

```bash
python3 --version
```

> Other distributions: use your package manager's equivalent (`dnf`, `pacman`, etc.), ensuring both `venv` and `pip` are included.

## 3. Clone the Repository

```bash
git clone <repository-url>
cd <repository-folder>
```

## 4. Create a Virtual Environment

A virtual environment keeps this project's dependencies isolated from other Python projects on your system.

From the project root:

```bash
python3 -m venv venv
```

This creates a `venv/` directory inside the project. (On Windows, you may need `python` instead of `python3`.)

## 5. Activate the Virtual Environment

**macOS / Linux:**

```bash
source venv/bin/activate
```

**Windows (Command Prompt):**

```cmd
venv\Scripts\activate.bat
```

**Windows (PowerShell):**

```powershell
venv\Scripts\Activate.ps1
```

> If PowerShell blocks the script with an execution-policy error, run PowerShell as Administrator once and execute:
> `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`

Once activated, your terminal prompt should be prefixed with `(venv)`.

## 6. Install Dependencies

With the virtual environment active, upgrade `pip` and install the required packages:

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If the project does not yet include a `requirements.txt`, install the core dependencies directly:

```bash
pip install strands boto3 python-dotenv
```

To freeze these into a `requirements.txt` for reproducible installs going forward:

```bash
pip freeze > requirements.txt
```

**Core dependencies:**

| Package | Purpose |
|---|---|
| `strands` | Agent framework (Agents, Swarm, Skills, Hooks) |
| `boto3` | AWS SDK, used for Bedrock model access |
| `python-dotenv` | Loads environment variables from `.env` |

## 7. Configure Environment Variables

Create a `.env` file in the project root:

```env
REGION=us-east-1
MODEL_ID=<your-bedrock-model-id>
```

Also confirm the following paths exist and are writable before first run:

- `SERVER_PATH` → path to `mcp_server_local.py`
- `SHELL_PATH` and `OUTPUT_PATH` → local directories used by the shell sandbox

Ensure your AWS credentials are available to `boto3`, via one of:

```bash
aws configure
```

or environment variables:

```bash
export AWS_ACCESS_KEY_ID=<your-key-id>
export AWS_SECRET_ACCESS_KEY=<your-secret>
export AWS_DEFAULT_REGION=us-east-1
```

or an attached IAM role, if running on AWS infrastructure.

## 8. Verify the Installation

With the virtual environment still active, run:

```bash
python strands-code.py
```

You should see:

```text
Welcome to Abhinav's personal assistant. Type 'exit' to quit.
You:
```

Try a simple prompt (e.g. `Tell me about Abhinav's projects.`) to confirm the MCP server and Bedrock models are reachable. Type `exit` to close the session.

## 9. Deactivating / Removing the Environment

To leave the virtual environment:

```bash
deactivate
```

To remove it entirely (e.g. to start fresh):

```bash
rm -rf venv        # macOS/Linux
rmdir /s /q venv   # Windows (Command Prompt)
```

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| `python3: command not found` | Python not installed or not on PATH | Reinstall Python and ensure "Add to PATH" is checked (Windows) or use `brew`/`apt` (macOS/Linux) |
| `pip: command not found` inside venv | venv created without pip | Recreate with `python3 -m venv venv --upgrade-deps`, or run `python -m ensurepip` |
| `ModuleNotFoundError` after install | Dependencies installed outside the venv | Confirm `(venv)` appears in your prompt before running `pip install` |
| PowerShell activation blocked | Script execution policy | Run `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` as Administrator |
| Bedrock/auth errors on run | Missing/invalid AWS credentials or wrong region | Re-run `aws configure` and confirm `REGION` in `.env` matches an enabled Bedrock region |
| `strands` or other package fails to install | Outdated pip or unsupported Python version | Run `python -m pip install --upgrade pip` and confirm Python is 3.10+ |