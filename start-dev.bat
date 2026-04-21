@echo off
REM Start the engram-harness dev environment
REM  - FastAPI backend on http://localhost:8420
REM  - Vite frontend on http://localhost:5173

setlocal

set ROOT=%~dp0

echo Starting FastAPI backend on :8420 ...
start "harness-backend" cmd /k "cd /d %ROOT% && python -m uvicorn harness.server:app --host 127.0.0.1 --port 8420 --reload"

echo Waiting for backend to start...
timeout /t 2 /nobreak >nul

echo Starting Vite dev server on :5173 ...
start "harness-frontend" cmd /k "cd /d %ROOT%\frontend && npm run dev"

echo.
echo Both servers are starting in separate windows.
echo   Backend:  http://localhost:8420
echo   Frontend: http://localhost:5173
echo.
echo Open http://localhost:5173 in your browser.
