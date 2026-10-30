@echo off
cd /d "%~dp0"
python -m src.forge_runtime.ollama_cli --action chat --workspace "%~dp0workspaces\forge-ollama-chat"
if errorlevel 1 pause
