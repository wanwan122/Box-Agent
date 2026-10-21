"""Expose the native App adapter as a stdio MCP server for Box-Agent.

mac-cua wraps the same adapter in a persistent JavaScript REPL. Box-Agent has
no REPL tool, so each App method becomes one direct MCP tool keyed by the App
reference name returned from ``get_app``.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
import time
from typing import Any

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

from box_agent.mcp_servers.cua_adapter import (
    DRIVER_TIMEOUT_SECONDS,
    ActionError,
    Adapter,
    McpClient,
    data,
)

DEFAULT_DRIVER = "/Applications/CuaDriver.app/Contents/MacOS/cua-driver"
DRIVER_ENV = "BOX_AGENT_CUA_DRIVER"
# Optional directory for raw driver call/result evidence (one JSONL per server process).
EVIDENCE_ENV = "BOX_AGENT_CUA_EVIDENCE_DIR"
CALL_TIMEOUT_SECONDS = 120

INSTRUCTIONS = """Native macOS App control. Start with get_app("Microsoft Word") (name, bundle ID or
.app path); it binds to a running app or launches it once, and returns the current AX state.
Pass that same app string to every other tool. The adapter resolves the current window, an
open app menu, or an attached Save/Open sheet automatically; there is no window-selection API.
Element IDs are positions in the latest observation only. After any action, dialog, menu or
window change, observe again (get_state) before using an element ID. Screenshot coordinates
refer to the latest screenshot from get_state(screenshot=true); never reuse old coordinates.
Actions return no state: observe to verify. Never replay an action whose outcome is unknown.
Native Open/Save/Go to Folder dialogs: never use set_value; focus the field, press cmd+a, then
paste(text). Use set_value only for writable fields in an ordinary window. Use type_text to
insert at the cursor, paste for paths, long, multiline or non-ASCII text."""

_APP = {"type": "string", "description": "App reference passed to get_app."}
_TARGET = {
    "description": "Element ID from the latest get_state, or [x, y] in the latest screenshot's pixels.",
    "anyOf": [
        {"type": "integer", "minimum": 0},
        {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
    ],
}
_POINT = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> Tool:
    return Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


TOOLS = [
    _tool("list_apps", "List installed and running macOS apps.", {}, []),
    _tool(
        "get_app",
        "Bind to a running app or launch it once in the background, wait for its first window, "
        "and return its current AX state. Does not create or erase documents. " + INSTRUCTIONS,
        {"app": {"type": "string", "description": "App name, bundle ID or absolute .app path."}},
        ["app"],
    ),
    _tool(
        "list_windows",
        "List the app's visible windows (id, title, bounds, layer, isActive). Read-only; "
        "a new supplemental window is verified once, which invalidates earlier element IDs.",
        {"app": _APP},
        ["app"],
    ),
    _tool(
        "get_state",
        "Observe the current app surface: active menu, attached sheet or current window. "
        "Subsequent calls return diffs (~ replaced line, + added line, removed IDs). "
        "screenshot=true also activates the window and returns one window screenshot; "
        "ax=false returns only the screenshot and keeps existing element IDs.",
        {
            "app": _APP,
            "screenshot": {"type": "boolean", "default": False},
            "ax": {"type": "boolean", "default": True},
            "disable_diffing": {"type": "boolean", "default": False},
        },
        ["app"],
    ),
    _tool(
        "click",
        "Click an element ID or screenshot point. Elements without AXPress are clicked at the "
        "center of their visible frame when screenshot geometry is verified.",
        {
            "app": _APP,
            "target": _TARGET,
            "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            "click_count": {"type": "integer", "minimum": 1, "default": 1},
        },
        ["app", "target"],
    ),
    _tool(
        "drag",
        "Drag between two points of the latest screenshot.",
        {"app": _APP, "from": _POINT, "to": _POINT},
        ["app", "from", "to"],
    ),
    _tool(
        "scroll",
        "Scroll an element or screenshot point by 1-50 driver page steps.",
        {
            "app": _APP,
            "target": _TARGET,
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "pages": {"type": "integer", "minimum": 1, "maximum": 50, "default": 1},
        },
        ["app", "target", "direction"],
    ),
    _tool(
        "set_value",
        "Replace the whole value of a writable editable control in an ordinary window. Never use "
        "it in native Open/Save/Go to Folder dialogs, sheets or rich-text editors.",
        {"app": _APP, "element": {"type": "integer", "minimum": 0}, "value": {"type": "string"}},
        ["app", "element", "value"],
    ),
    _tool(
        "type_text",
        "Type text into the focused control as keyboard input.",
        {"app": _APP, "text": {"type": "string"}},
        ["app", "text"],
    ),
    _tool(
        "paste",
        "Paste plain text into the focused control via the clipboard (Cmd+V). The text stays on "
        "the clipboard. After an error, observe before continuing; do not replay.",
        {"app": _APP, "text": {"type": "string"}},
        ["app", "text"],
    ),
    _tool(
        "press_key",
        "Press a key or combination, e.g. Return, Escape, cmd+s, cmd+shift+g.",
        {"app": _APP, "key": {"type": "string"}},
        ["app", "key"],
    ),
]

_SCREENSHOT_ONLY = "getScreenshot"


class CuaService:
    """Serialize model tool calls onto one adapter and its driver connection."""

    def __init__(self, driver_factory, evidence=None) -> None:
        self.factory = driver_factory
        self.evidence = evidence
        self.adapter: Adapter | None = None
        self.apps: dict[str, str] = {}

    def backend(self, deadline: float) -> Adapter:
        if self.adapter is None:
            timeout = max(0.001, min(DRIVER_TIMEOUT_SECONDS, deadline - time.monotonic()))
            self.adapter = Adapter(self.factory(timeout), self.evidence)
            self.apps.clear()
        self.adapter.deadline = deadline
        return self.adapter

    def handle(self, app: Any) -> str:
        if not isinstance(app, str) or app not in self.apps:
            raise ValueError("Unknown app reference; call get_app first (again after a reset).")
        return self.apps[app]

    def call(self, name: str, args: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        deadline = time.monotonic() + CALL_TIMEOUT_SECONDS
        try:
            return self._dispatch(name, args, deadline), False
        except ActionError as failure:
            detail = json.dumps(
                {k: v for k, v in data(failure.result).items()
                 if k in {"effect", "status", "error", "reason", "refusal", "message", "code"}},
                ensure_ascii=False,
            )
            texts = [c.get("text", "") for c in failure.result.get("content", []) if c.get("type") == "text"]
            return [_text("\n".join([detail, *texts]))], True
        except (ValueError, KeyError, TypeError) as failure:
            return [_text(str(failure))], True
        except (TimeoutError, EOFError, OSError, RuntimeError) as failure:
            # The connection is poisoned and effects may be unknown. Drop every
            # reference so the model must re-observe instead of replaying.
            self.close()
            return [_text(f"{failure}\nCUA connection reset; app references cleared. "
                          "Call get_app again and observe before continuing; do not replay.")], True
        finally:
            if self.adapter is not None:
                self.adapter.deadline = None

    def _dispatch(self, name: str, args: dict[str, Any], deadline: float) -> list[dict[str, Any]]:
        adapter = self.backend(deadline)
        if name == "list_apps":
            return [_text(json.dumps(adapter.control("listApps"), ensure_ascii=False))]
        if name == "get_app":
            app = args.get("app")
            value = adapter.control("getApp", None, {"app": app})
            self.apps[app] = value["handle"]
            return [_text(value["state"])]
        handle = self.handle(args.get("app"))
        if name == "list_windows":
            return [_text(json.dumps(adapter.control("listWindows", handle), ensure_ascii=False))]
        if name == "get_state":
            ax = args.get("ax", True)
            screenshot = args.get("screenshot", False)
            if not ax and not screenshot:
                raise ValueError("get_state needs ax or screenshot")
            method = ("getAXStateAndScreenshot" if ax and screenshot else
                      "getAXState" if ax else _SCREENSHOT_ONLY)
            value = adapter.control(method, handle, {"disableDiffing": args.get("disable_diffing", False)})
            content = [_text(value["state"])] if value.get("state") else []
            if value.get("image"):
                content.append(value["image"])
            elif screenshot and ax:
                content.append(_text("Screenshot unavailable; do not choose coordinates."))
            return content
        if name == "click":
            options = {"mouseButton": args.get("button", "left"), "clickCount": args.get("click_count", 1)}
            value = adapter.control("click", handle, {"target": args.get("target"), "options": options})
        elif name == "drag":
            value = adapter.control("drag", handle, {"from": args.get("from"), "to": args.get("to")})
        elif name == "scroll":
            value = adapter.control("scroll", handle, {
                "target": args.get("target"), "direction": args.get("direction"),
                "pages": args.get("pages", 1)})
        elif name == "set_value":
            value = adapter.control("setValue", handle, {"target": args.get("element"), "value": args.get("value")})
        elif name == "type_text":
            value = adapter.control("typeText", handle, {"text": args.get("text")})
        elif name == "paste":
            value = adapter.control("paste", handle, {"text": args.get("text")})
        elif name == "press_key":
            value = adapter.control("pressKey", handle, {"key": args.get("key")})
        else:
            raise ValueError(f"Unknown tool: {name}")
        effect = value.get("effect")
        return [_text(f"Done (driver effect: {effect}). Observe to verify." if effect
                      else "Done. Observe to verify.")]

    def close(self) -> None:
        adapter, self.adapter = self.adapter, None
        self.apps.clear()
        if adapter is None:
            return
        try:
            adapter.close()
        except Exception:
            pass
        finally:
            adapter.driver.close()


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _mcp_content(items: list[dict[str, Any]]) -> list[TextContent | ImageContent]:
    content: list[TextContent | ImageContent] = []
    for item in items:
        if item.get("type") == "image":
            content.append(ImageContent(type="image", data=item["data"], mimeType=item.get("mimeType", "image/png")))
        else:
            content.append(TextContent(type="text", text=item.get("text", "")))
    return content


def build_server(service: CuaService) -> Server:
    server = Server("box-agent-cua", instructions=INSTRUCTIONS)
    lock = asyncio.Lock()

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]):
        from mcp.types import CallToolResult

        async with lock:
            items, is_error = await asyncio.to_thread(service.call, name, arguments or {})
        return CallToolResult(content=_mcp_content(items), isError=is_error)

    return server


def driver_command(driver: str, socket: str | None = None, *, embedded: bool = False) -> list[str]:
    """Connect to the lifecycle owner's endpoint without managing its daemon."""
    if embedded and not socket:
        raise ValueError("embedded mode requires an explicit socket")
    command = [driver, "mcp"]
    if socket:
        command.extend(["--socket", socket])
    if embedded:
        command.append("--embedded")
    return command


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="App MCP tools over the official macOS CuaDriver")
    parser.add_argument("--driver", default=os.environ.get(DRIVER_ENV) or DEFAULT_DRIVER)
    parser.add_argument("--socket", help="Driver socket supplied by the runtime owner")
    parser.add_argument("--embedded", action="store_true", help="Connect to a host-owned Driver")
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        parser.error("The App CUA interface requires macOS")
    try:
        command = driver_command(args.driver, args.socket, embedded=args.embedded)
    except ValueError as error:
        parser.error(str(error))
    evidence = None
    if os.environ.get(EVIDENCE_ENV):
        directory = os.path.expanduser(os.environ[EVIDENCE_ENV])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        evidence = open(os.path.join(directory, f"cua-driver-{os.getpid()}.jsonl"), "a", encoding="utf-8")
    service = CuaService(lambda timeout: McpClient(command, timeout=timeout), evidence)
    server = build_server(service)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        asyncio.run(run())
    finally:
        service.close()
        if evidence is not None:
            evidence.close()


if __name__ == "__main__":
    main()
