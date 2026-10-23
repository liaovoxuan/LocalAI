@echo off
set "LOCALAI_EXE=%~dp0artifacts\LocalAI\LocalAI.exe"
if not exist "%LOCALAI_EXE%" set "LOCALAI_EXE=%~dp0dist\LocalAI.exe"
if not exist "%LOCALAI_EXE%" (
  echo LocalAI executable not found in artifacts\LocalAI or dist.
  pause
  exit /b 1
)
"%LOCALAI_EXE%"
pause
