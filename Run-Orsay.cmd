@echo off
rem SPDX-License-Identifier: GPL-2.0-or-later
setlocal
pushd "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0tools\run_orsay.py" %*
) else (
    python "%~dp0tools\run_orsay.py" %*
)
set "samsungf_exit=%errorlevel%"
popd
exit /b %samsungf_exit%
