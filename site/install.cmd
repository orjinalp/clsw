@echo off
rem clsw kurulumu (Windows cmd):
rem   curl -fsSL https://github.com/orjinalp/clsw/releases/latest/download/install.cmd -o install.cmd ^&^& install.cmd ^&^& del install.cmd
rem Asil is install.ps1 icinde (indirme, SHA-256 dogrulama, kurulum, PATH).
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://github.com/orjinalp/clsw/releases/latest/download/install.ps1 | iex"
if errorlevel 1 (
  echo clsw kurulumu basarisiz.
  exit /b 1
)
endlocal
