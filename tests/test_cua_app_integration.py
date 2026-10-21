"""App MCP integration shares the mainline runtime and hides raw driver tools."""
import asyncio
import base64
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from box_agent.acp import runtime_entry
from box_agent.plugins.cua import ready_tool
from box_agent.plugins.cua.config import CuaConfig, CuaRuntimeConfig
from box_agent.tools import mcp_loader
from scripts.build_runtime import pyinstaller_hidden_imports


def service(interface="driver", *, mode="external"):
    return ready_tool.CuaService(CuaConfig(
        tool_interface=interface,
        runtime=CuaRuntimeConfig(enabled=True, mode=mode, driver_path="/trusted/driver",
                                 endpoint="/host/socket" if mode == "external" else None),
    ))


def test_default_interface_keeps_native_driver_command():
    assert CuaConfig().tool_interface == "driver"
    svc = ready_tool.CuaService(CuaConfig(runtime=CuaRuntimeConfig(
        enabled=True, mode="external", driver_path="/trusted/driver", endpoint="/host/socket")))
    try:
        assert svc.server_config == {"command": "/trusted/driver",
                                     "args": ["mcp", "--socket", "/host/socket", "--embedded"],
                                     "disabled": False}
    finally:
        svc.adapter.cleanup()


@pytest.mark.parametrize("frozen", [False, True])
def test_app_interface_uses_host_driver_socket_in_python_and_frozen_runtime(monkeypatch, frozen):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(
        platform="darwin", executable="/runtime/entry", frozen=frozen), raising=False)
    svc = service("app")
    try:
        prefix = ["--cua-mcp"] if frozen else ["-m", "box_agent.mcp_servers.cua_server"]
        assert svc.server_config == {"command": "/runtime/entry",
            "args": prefix + ["--driver", "/trusted/driver", "--socket", "/host/socket", "--embedded"],
            "disabled": False}
    finally:
        svc.adapter.cleanup()


def test_app_interface_uses_owned_private_socket_without_embedded_mode(monkeypatch):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(
        platform="darwin", executable="/runtime/python", frozen=False), raising=False)
    svc = service("app", mode="standalone")
    try:
        args = svc.server_config["args"]
        assert args[-2:] == ["--socket", svc.adapter.endpoint]
        assert "--embedded" not in args
    finally:
        svc.adapter.cleanup()


def test_app_interface_is_rejected_on_unsupported_platform_before_launch(monkeypatch):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(platform="linux"), raising=False)
    with pytest.raises(ValueError, match="macOS"):
        service("app")


def test_app_selection_preserves_host_runtime_identity(monkeypatch):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(
        platform="darwin", executable="/runtime/python", frozen=False), raising=False)
    monkeypatch.setenv("BOX_AGENT_CUA_RUNTIME_CONFIG", json.dumps({
        "enabled": True, "mode": "external", "driver_path": "/host/driver",
        "endpoint": "/host/socket",
    }))
    config = SimpleNamespace(plugins={"cua": {"tool_interface": "app", "runtime": {
        "driver_path": "/session/driver", "endpoint": "/session/socket",
    }}})
    svc = ready_tool.create_cua_service(config)
    try:
        assert svc.settings.tool_interface == "app"
        assert svc.server_config["args"][-5:] == [
            "--driver", "/host/driver", "--socket", "/host/socket", "--embedded"]
    finally:
        svc.adapter.cleanup()


def test_same_name_and_socket_cannot_reuse_a_different_tool_interface(monkeypatch):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(
        platform="darwin", executable="/runtime/python", frozen=False), raising=False)
    first, second = service(), service("app")
    monkeypatch.setattr(mcp_loader, "_cua_service", None)
    try:
        assert mcp_loader.register_cua_service(first) is first
        with pytest.raises(RuntimeError, match="already registered"):
            mcp_loader.register_cua_service(second)
    finally:
        first.adapter.cleanup()
        second.adapter.cleanup()


@pytest.mark.asyncio
async def test_app_readiness_guidance_uses_app_skill_and_not_driver_tokens(monkeypatch):
    monkeypatch.setattr(ready_tool, "sys", SimpleNamespace(
        platform="darwin", executable="/runtime/python", frozen=False), raising=False)
    svc = service("app")
    monkeypatch.setattr(mcp_loader, "reconnect_mcp_server", AsyncMock(return_value={"success": True}))
    from box_agent.tools import mcp_tool_catalog
    monkeypatch.setattr(mcp_tool_catalog, "get_mcp_tool_catalog", lambda: SimpleNamespace(
        wait_until_ready=AsyncMock(return_value=True)))
    try:
        result = await ready_tool.EnsureCuaReadyTool(svc).execute()
        assert result.success
        assert "mac-cua" in result.content and "get_app" in result.content
        assert "snapshot_id" not in result.content
    finally:
        svc.adapter.cleanup()


def test_app_server_forwards_only_connection_arguments_to_official_driver():
    from box_agent.mcp_servers.cua_server import driver_command
    assert driver_command("/trusted/driver", "/host/socket", embedded=True) == [
        "/trusted/driver", "mcp", "--socket", "/host/socket", "--embedded"]
    assert driver_command("/trusted/driver") == ["/trusted/driver", "mcp"]
    with pytest.raises(ValueError, match="socket"):
        driver_command("/trusted/driver", embedded=True)


def test_app_tool_catalog_contains_only_eleven_public_app_tools():
    from box_agent.mcp_servers.cua_server import TOOLS
    assert {t.name for t in TOOLS} == {
        "list_apps", "get_app", "list_windows", "get_state", "click", "drag",
        "scroll", "set_value", "type_text", "paste", "press_key"}


@pytest.mark.parametrize("failure", [False, True])
def test_frozen_app_mcp_entry_forwards_arguments_and_restores_stdout(monkeypatch, tmp_path, failure):
    assert hasattr(runtime_entry, "CUA_MCP_ARG")
    calls = []
    def main(args):
        calls.append(args)
        sys.stdout.buffer.close()
        if failure:
            raise RuntimeError("MCP failed")
    monkeypatch.setitem(sys.modules, "box_agent.mcp_servers.cua_server", SimpleNamespace(main=main))
    with (tmp_path / "protocol.stdout").open("w+") as stdout:
        monkeypatch.setattr(sys, "argv", ["box-agent-acp", "--cua-mcp", "--driver", "/trusted/driver",
                                          "--socket", "/host/socket", "--embedded"])
        monkeypatch.setattr(sys, "stdout", stdout)
        monkeypatch.setattr(sys, "stderr", io.StringIO())
        if failure:
            with pytest.raises(RuntimeError, match="MCP failed"):
                runtime_entry.main()
        else:
            runtime_entry.main()
        assert calls == [["--driver", "/trusted/driver", "--socket", "/host/socket", "--embedded"]]
        assert sys.stdout is stdout and not stdout.closed


def test_frozen_runtime_collects_app_mcp_modules():
    assert {"box_agent.mcp_servers.cua_server", "box_agent.mcp_servers.cua_adapter"} <= set(
        pyinstaller_hidden_imports())


@pytest.mark.skipif(sys.platform != "darwin", reason="App MCP requires macOS")
@pytest.mark.parametrize("entry", [
    ["-m", "box_agent.mcp_servers.cua_server"],
    ["-m", "box_agent.acp.runtime_entry", "--cua-mcp"],
])
@pytest.mark.asyncio
async def test_real_stdio_discovery_exposes_app_tools_without_starting_driver(entry, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from box_agent.mcp_servers.cua_server import TOOLS

    # Discovery must not launch a Driver, require TCC, or touch the GUI.
    params = StdioServerParameters(command=sys.executable, args=entry + [
        "--driver", str(tmp_path / "absent-driver"),
        "--socket", str(tmp_path / "absent-driver.sock"), "--embedded",
    ])

    async def discover():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                assert result.serverInfo.name == "box-agent-cua"
                catalog = await session.list_tools()
                assert {tool.name for tool in catalog.tools} == {tool.name for tool in TOOLS}
                assert len(catalog.tools) == 11
                assert catalog.nextCursor is None

    await asyncio.wait_for(discover(), timeout=30)


@pytest.mark.parametrize("image_input", [True, False, None])
@pytest.mark.asyncio
async def test_app_screenshot_reaches_vision_hook_without_raw_driver_guidance(image_input, tmp_path):
    from mcp.types import CallToolResult
    from PIL import Image
    from box_agent.mcp_servers import cua_server
    from box_agent.plugins.cua.wiring import build_cua_bindings
    from box_agent.tools.mcp_result_hooks import bind_mcp_result_adapter
    from tests.test_cua_adapter_port import FakeDriver

    image_bytes = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image_bytes, format="PNG")
    driver = FakeDriver()
    driver.responses["get_window_state"]["content"][0]["data"] = base64.b64encode(
        image_bytes.getvalue()).decode("ascii")
    app_service = cua_server.CuaService(lambda timeout: driver)
    try:
        app_service.call("get_app", {"app": "TextEdit"})
        args = {"app": "TextEdit", "screenshot": True}
        items, error = app_service.call("get_state", args)
        assert not error
        response = CallToolResult(content=cua_server._mcp_content(items), isError=False)
        description = next(tool for tool in cua_server.TOOLS if tool.name == "get_state")
        tool = mcp_loader.MCPTool(
            name="get_state", description=description.description,
            parameters=description.inputSchema,
            session=SimpleNamespace(call_tool=AsyncMock(return_value=response)),
            server_name="computer-use", remote_name="get_state",
        )
        bindings = build_cua_bindings(
            llm=SimpleNamespace(capabilities={"image_input": image_input}),
            config=CuaConfig(tool_interface="app"), sidecar_dir=tmp_path / "images",
        )
        with bind_mcp_result_adapter(bindings):
            output = await tool.execute(**args)
        assert output.success and "Window:" in output.content
        assert "element_token" not in output.content and "snapshot_id" not in output.content
        assert len(list((tmp_path / "images").glob("*.png"))) == 1
        if image_input is True:
            images = [block for block in output.transient_followup_content
                      if block["type"] == "input_image"]
            assert len(images) == 1 and images[0]["width"] == 8
        else:
            assert output.transient_followup_content is None
    finally:
        app_service.close()
