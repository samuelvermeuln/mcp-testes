@echo off
setlocal

set ROOT_DIR=%~dp0\..
cd /d %ROOT_DIR%

if not exist .venv (
  uv venv
)

call .venv\Scripts\activate.bat
uv pip install -e .

set CONFIG_PATH=%1
if "%CONFIG_PATH%"=="" set CONFIG_PATH=%ROOT_DIR%\config.toml
set GOSYSTEM_MCP_CONFIG_TOML=%CONFIG_PATH%

gosystem-test-mcp
