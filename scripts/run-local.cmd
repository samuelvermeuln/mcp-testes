@echo off
setlocal

set ROOT_DIR=%~dp0\..
cd /d %ROOT_DIR%

if not exist .venv (
  uv venv
)

call .venv\Scripts\activate.bat
uv pip install -e .

gosystem-test-mcp
