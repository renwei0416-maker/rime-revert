@echo off
setlocal
rem ===================================================================
rem  build.cmd - build sendKeyCode.dll (Rime / Weasel Lua C module, x64)
rem  Compiles ONLY sendKeyCode.c - no other source file of the project.
rem  Requires: MSYS2 MinGW-w64 x64 gcc, plus rime.dll of the target Weasel
rem            (rime.dll embeds Lua 5.4 and provides luaL_*/lua_* imports).
rem ===================================================================

rem --- configuration: override with environment variables if needed ---
if "%MINGW_BIN%"==""  set "MINGW_BIN=C:\msys64\mingw64\bin"
if "%WEASEL_DIR%"=="" set "WEASEL_DIR=C:\Program Files\Rime\weasel-0.17.4"

if not exist "%MINGW_BIN%\gcc.exe" (
  echo [ERROR] gcc not found: "%MINGW_BIN%\gcc.exe"
  echo         install: winget install MSYS2.MSYS2  then  pacman -S mingw-w64-x86_64-gcc
  exit /b 1
)
if not exist "%WEASEL_DIR%\rime.dll" (
  echo [ERROR] rime.dll not found: "%WEASEL_DIR%\rime.dll"
  echo         set WEASEL_DIR to the Weasel install directory
  exit /b 1
)

rem --- IMPORTANT: the gcc child process cc1 needs mingw64\bin in PATH,
rem --- otherwise it exits 1 silently without any compiler error message.
set "PATH=%MINGW_BIN%;%PATH%"

cd /d "%~dp0"

echo [1/2] compiling and linking ^(sendKeyCode.c only^) ...
gcc -shared -fPIC -o sendKeyCode.dll sendKeyCode.c -Iinclude -L"%WEASEL_DIR%" -luser32 -lrime -O2 -Wl,--no-insert-timestamp
if errorlevel 1 (
  echo [ERROR] build failed
  exit /b 1
)

echo [2/2] done
for %%F in ("sendKeyCode.dll") do echo   output: %%~fF  ^(%%~zF bytes^)
certutil -hashfile "sendKeyCode.dll" SHA256 | findstr /v ":"
endlocal
