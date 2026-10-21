# Mac-CUA 初步适配：CuaDriver 0.25.0 前台版本

本分支将 mac-cua dev/codex_v1.0 的早期提交 fe6f3af 中的原生 App 适配能力接入 Box-Agent。目标为 CuaDriver 0.25.0；保留前台聚焦、桌面按键、剪贴板粘贴和窗口校验语义。不包含后台优先路由、合成焦点或 Swift helper。

调用链：Box-Agent → 11 个 App MCP 工具 → Python 适配层 → 官方 CuaDriver 0.25.0 → macOS 应用。保留默认原生 Driver 接口，通过 plugins.cua.tool_interface: app 显式选择 App 接口；两者复用已有 Driver 生命周期管理。

配置示例（driver_path 指向已经安装的 0.25.0 可执行文件）：

```yaml
plugins:
  cua:
    tool_interface: app
    runtime:
      enabled: true
      mode: standalone
      driver_path: /absolute/path/to/cua-driver-0.25.0
```

先运行指定可执行文件的 --version 核对 0.25.0。此分支不会自动下载或降级系统 Driver，也不修改主线安装器默认版本。较新 Driver 的桌面坐标语义不同，不应将此适配版本直接视为兼容新版。

截图尺寸仅在 Driver schema 支持 max_image_dimension 时请求限制，以配合 Box-Agent 图片输入；这不改变前台输入策略。截图沿用早期基线的激活校验语义，不能承诺观察完全不改变前台。

新增 MCP 支持 Python 模块入口、console entry 和冻结 runtime 的 --cua-mcp 入口。关闭适配器只关闭自身连接和 session，不停止外部 daemon。

验证结果见本次提交说明。源码测试与 stdio 工具发现不代表新打包 runtime 或完整 Office GUI E2E 已通过；本分支未替换用户当前 runtime。

## 本次验证

167 项相关测试通过，覆盖适配器、MCP 服务、真实 stdio 工具发现、截图到模型的传递、生命周期、配置、技能清单和打包入口。`git diff --check` 通过。未运行本分支的完整 Office GUI E2E，未构建或安装新 runtime。

```sh
python -m pytest tests/test_cua_adapter_port.py tests/test_cua_server.py tests/test_cua_app_integration.py tests/test_cua_builtin_skill.py tests/test_cua_ready_tool.py tests/test_cua_driver.py tests/test_cua_lifecycle.py tests/test_cua_offswitch_noop.py tests/test_cua_structured_result.py tests/test_mcp_cua_screenshots.py tests/test_mcp_result_hooks.py tests/test_plugin_config.py tests/test_runtime_entry.py tests/test_build_runtime.py tests/test_generate_skills_manifest.py -q
```
