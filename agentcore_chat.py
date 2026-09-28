#!/usr/bin/env python3
"""Sign in with Google through Cognito, then chat with deployed AgentCore Runtime.

Configure the .env file beside this script, then run: python agentcore_chat.py
See .env.example for the required settings. Existing environment variables override
.env values, and command-line arguments override both.

A temporary localhost listener receives the OAuth callback; the agent runs
entirely in AWS. Login uses PKCE and keeps tokens in memory only. Run again
to sign in after expiry, using --session-id to resume the conversation.
The runtime derives the user identity from the token; this client reuses a
separate conversation ID and sends only the latest prompt on each turn.

Requires: python -m pip install -r requirements-chat.txt
"""

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import httpx
from dotenv import load_dotenv


def build_session_id() -> str:
    """AgentCore requires runtimeSessionId to be at least 33 characters."""
    return f"session-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"


def login_with_google(domain: str, client_id: str, callback_url: str,
                      timeout: float = 300.0) -> str:
    """Receive a state-checked OAuth code and exchange it using PKCE S256."""
    domain = domain.rstrip("/")
    origin = urlsplit(domain)
    callback = urlsplit(callback_url)
    if (origin.scheme != "https" or not origin.hostname or origin.path
            or origin.query or origin.fragment or origin.username or origin.password):
        raise ValueError("Cognito domain must be an HTTPS origin without a path")
    if (callback.scheme != "http" or callback.hostname not in ("localhost", "127.0.0.1")
            or not callback.port or not callback.path or callback.query
            or callback.fragment or callback.username or callback.password):
        raise ValueError("Callback must be an HTTP localhost URL with a port and path")
    if not client_id.strip() or timeout <= 0:
        raise ValueError("A Cognito app client ID and positive login timeout are required")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format, *args):
            pass  # Never log callback URLs containing authorization codes.

        def reply(self, status, message):
            body = message.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            incoming = urlsplit(self.path)
            if incoming.path != callback.path:
                self.reply(404, "Not found")
                return
            params = parse_qs(incoming.query, keep_blank_values=True)
            states = params.get("state", [])
            if len(states) != 1 or not secrets.compare_digest(
                states[0].encode("utf-8"), state.encode("utf-8")
            ):
                self.reply(400, "Invalid login state. Use the original sign-in window.")
                return
            if "error" in params:
                result["error"] = "Sign-in was cancelled or rejected. Run the client again."
                self.reply(400, result["error"])
                return
            codes = params.get("code", [])
            if len(codes) != 1 or not codes[0]:
                self.reply(400, "Missing or invalid authorization code")
                return
            result["code"] = codes[0]
            self.reply(200, "Login callback received. Return to the terminal to continue.")

    # Bind before opening the browser, and only accept loopback connections.
    try:
        server = HTTPServer(("127.0.0.1", callback.port), CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot listen on callback port {callback.port}. Close the app using it "
            "or choose another callback URL registered in Cognito."
        ) from exc

    with server:
        server.timeout = 0.5
        login_url = domain + "/oauth2/authorize?" + urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": callback_url,
            "scope": "openid email",
            "identity_provider": "Google",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        print(f"Opening Google sign-in. Waiting up to {timeout:g} seconds for login...")
        try:
            opened = webbrowser.open(login_url)
        except webbrowser.Error:
            opened = False
        if not opened:
            print(f"Open this sign-in URL in your browser:\n{login_url}")
        deadline = time.monotonic() + timeout
        while not result and time.monotonic() < deadline:
            server.handle_request()

    if not result:
        raise RuntimeError("Login timed out. Run the client again to sign in.")
    if "error" in result:
        raise RuntimeError(result["error"])

    response = httpx.post(domain + "/oauth2/token", data={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "redirect_uri": callback_url,
        "code": result["code"],
        "code_verifier": verifier,
    }, timeout=30.0)
    if response.is_error:
        # Do not print response bodies or tokens.
        raise RuntimeError(f"Cognito token exchange failed (HTTP {response.status_code}). "
                           "Check the app client and registered callback URL.")
    tokens = response.json()
    if (not isinstance(tokens, dict)
            or not isinstance(tokens.get("access_token"), str)
            or not tokens["access_token"]
            or str(tokens.get("token_type", "")).lower() != "bearer"):
        raise RuntimeError("Cognito did not return a valid bearer access token")
    return tokens["access_token"]


def main() -> None:
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    endpoint_arn = os.getenv("AGENT_RUNTIME_ENDPOINT_ARN", "")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cognito-client-id", default=os.getenv("COGNITO_APP_CLIENT_ID"),
                        help="Cognito app client ID (not the Google OAuth client ID)")
    parser.add_argument("--cognito-domain", default=os.getenv("COGNITO_DOMAIN"))
    parser.add_argument("--callback-url", default=os.getenv("COGNITO_CALLBACK_URL"),
                        help="Exact callback URL registered on the Cognito app client")
    parser.add_argument(
        "--agent-runtime-arn",
        default=os.getenv("AGENT_RUNTIME_ARN"),
        help="ARN of the aws_bedrockagentcore_agent_runtime (the RUNTIME, not the endpoint)",
    )
    parser.add_argument(
        "--qualifier",
        default=os.getenv("AGENT_ENDPOINT_QUALIFIER", endpoint_arn.rsplit("/", 1)[-1]),
        help="Endpoint name (aws_bedrockagentcore_agent_runtime_endpoint). "
             "Pass '' or 'DEFAULT' to use the runtime's auto-created default endpoint instead.",
    )
    parser.add_argument("--session-id", default=None,
                         help="Reuse a specific session id to resume a prior conversation "
                              "for the signed-in user. "
                              "Defaults to a new random session.")
    args = parser.parse_args()

    for attribute, variable, flag in (
        ("cognito_client_id", "COGNITO_APP_CLIENT_ID", "--cognito-client-id"),
        ("cognito_domain", "COGNITO_DOMAIN", "--cognito-domain"),
        ("callback_url", "COGNITO_CALLBACK_URL", "--callback-url"),
        ("agent_runtime_arn", "AGENT_RUNTIME_ARN", "--agent-runtime-arn"),
    ):
        if not (getattr(args, attribute) or "").strip():
            parser.error(f"Set {variable} in .env or the environment, or pass {flag}")
    if not re.fullmatch(r"arn:aws:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[A-Za-z0-9_-]+",
                        args.agent_runtime_arn):
        parser.error("Provide a valid AgentCore runtime ARN")
    session_id = args.session_id or build_session_id()
    if not re.fullmatch(r"[A-Za-z0-9_-]{33,256}", session_id):
        parser.error("Session ID must contain 33-256 letters, digits, underscores or hyphens")
    try:
        access_token = login_with_google(args.cognito_domain, args.cognito_client_id, args.callback_url)
    except (httpx.HTTPError, ValueError, RuntimeError, OSError) as exc:
        print(f"[error] Login failed: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        return

    region = args.agent_runtime_arn.split(":")[3]
    encoded_arn = quote(args.agent_runtime_arn, safe="")

    invoke_url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded_arn}/invocations"
    )

    params = {
        "qualifier": args.qualifier or "DEFAULT",
    }

    print("Signed in successfully.")
    print(f"Session ID: {session_id}")
    print("(save this to resume the same conversation later with --session-id)")
    print("Type your message and press Enter. Type 'exit' to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        request_payload = {
            "prompt": user_input,
            "request_id": uuid.uuid4().hex,
        }
        # Matches payload.get("prompt", "") in your entrypoint exactly --
        # no history array, the container's own session memory handles that.
        try:
            response = httpx.post(
                invoke_url,
                params=params,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
                    "Accept": "application/json",
                },
                json=request_payload,
                timeout=300.0,
            )

            if response.status_code in (401, 403):
                print(
                    "Authentication or authorization failed. "
                    "Run again to sign in, and use --session-id to resume. "
                    "If it persists, check the runtime authorization settings."
                )
                break

            response.raise_for_status()
            data = response.json()

        except (httpx.HTTPError, ValueError) as exc:
            print(f"\n[error] Runtime invocation failed: {exc}\n")
            continue

        if "error" in data:
            print(f"Agent [error]: {data['error']}\n")
            continue

        print(f"Agent: {data.get('result', data)}\n")


if __name__ == "__main__":
    main()
