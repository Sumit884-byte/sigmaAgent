"""Arka as SigmaAgent's language model, over MCP.

SigmaAgent starts `arka mcp serve` once, keeps it running, and calls its `arka_ask` tool.
Prompts start with "### Task:" so Arka sends them straight to its own model (instead of
memory or web search) and returns only the requested output. Arka picks and pays for the
model, so SigmaAgent needs no Gemini key. Choose it with LLM_PROVIDER=arka; ARKA_MCP_COMMAND
overrides how the server is started (default `arka mcp serve`).

Arka normally compacts prompts over 900 characters into a summary ("Task IR"), which drops
the exact instructions SigmaAgent depends on, so the server is started with
ARKA_PROMPT_COMPACT=0 unless that variable is already set.
"""

from __future__ import annotations

import itertools
import json
import os
import queue
import re
import shlex
import subprocess
import threading
from typing import Any

from pydantic import BaseModel, ValidationError

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT_SECONDS = 90
START_TIMEOUT_SECONDS = 30


class ArkaUnavailable(RuntimeError):
    """Arka's MCP server couldn't be started or didn't answer."""


def _arka_task(prompt: str) -> str:
    # Arka treats prompts mentioning follow-ups as "suggest follow-up questions" requests
    prompt = re.sub(r"(?i)follow[\s_-]?ups?", "next steps", prompt)
    return f"### Task:\n{prompt}"


class ArkaMCPClient:
    """A long-lived stdio MCP connection to Arka (one JSON-RPC message per line)."""

    def __init__(self, command: list[str] | None = None):
        self.command = command or shlex.split(os.getenv("ARKA_MCP_COMMAND", "arka mcp serve"))
        self._process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _start(self) -> None:
        try:
            self._process = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1,
                env={**os.environ, "ARKA_PROMPT_COMPACT": os.getenv("ARKA_PROMPT_COMPACT", "0")},
            )
        except OSError as exc:
            raise ArkaUnavailable(f"couldn't start `{' '.join(self.command)}` ({exc})") from exc
        self._lines = queue.Queue()
        # A reader thread (rather than select) so timeouts also work on Windows pipes
        threading.Thread(target=self._read, args=(self._process, self._lines), daemon=True).start()
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "sigmaagent", "version": "1"},
        }, timeout=START_TIMEOUT_SECONDS)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _read(process: subprocess.Popen, lines: queue.Queue) -> None:
        for line in process.stdout:
            lines.put(line)
        lines.put(None)  # the server exited

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise ArkaUnavailable(f"Arka's MCP server closed the connection ({exc})") from exc

    def _request(self, method: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        request_id = next(self._ids)
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty as exc:
                raise ArkaUnavailable(f"Arka didn't answer within {timeout:.0f}s") from exc
            if line is None:
                raise ArkaUnavailable("Arka's MCP server exited")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # not a protocol message
            if message.get("id") != request_id:
                continue  # notifications or stale replies
            if "error" in message:
                raise ArkaUnavailable(f"Arka returned an error: {message['error'].get('message', message['error'])}")
            return message.get("result") or {}

    def call_tool(self, name: str, arguments: dict[str, Any], *, timeout: float = CALL_TIMEOUT_SECONDS) -> str:
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            try:
                result = self._request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
            except ArkaUnavailable:
                self.close()  # start fresh next time rather than reuse a stuck process
                raise
        text = "\n".join(part.get("text", "") for part in result.get("content", []) if part.get("type") == "text")
        if result.get("isError"):
            raise ArkaUnavailable(f"Arka's {name} failed: {text[:200]}")
        return text.strip()

    def close(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process = None


class ArkaLLM:
    """The language-model interface SigmaAgent needs, answered by Arka."""

    def __init__(self, client: ArkaMCPClient | None = None):
        self.client = client or ArkaMCPClient()

    def complete(self, prompt: str) -> str:
        return self.client.call_tool("arka_ask", {"prompt": _arka_task(prompt)})

    def structured(self, prompt: str, model: type[BaseModel], format_hint: str) -> BaseModel:
        """Ask for JSON matching `model`; retry once if the reply doesn't parse."""
        instruction = (f"{prompt}\n\nReply with only one JSON object and nothing else (no prose, no code fences), "
                       f"in this format:\n{format_hint}")
        reply = self.complete(instruction)
        try:
            return parse_json_reply(reply, model)
        except ValueError as first_error:
            retry = self.complete(f"{instruction}\n\nYour previous reply was not valid ({first_error}). "
                                  f"Previous reply:\n{reply[:2000]}")
            return parse_json_reply(retry, model)


def parse_json_reply(text: str, model: type[BaseModel]) -> BaseModel:
    """The first JSON object in a model reply (tolerating code fences or stray prose), validated."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    try:
        return model.model_validate(json.loads(text[start:end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(str(exc).splitlines()[0]) from exc


_shared: ArkaLLM | None = None
_shared_lock = threading.Lock()


def warm_up() -> None:
    """Start Arka and load its model in the background, so the first Discord message doesn't wait."""
    def run() -> None:
        try:
            shared_llm().complete("Reply with only: ready")
        except ArkaUnavailable as exc:
            print(f"Arka warm-up failed: {exc}")
    threading.Thread(target=run, daemon=True).start()


def shared_llm() -> ArkaLLM:
    """One Arka connection for the whole API process."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ArkaLLM()
        return _shared
