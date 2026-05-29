@echo off
REM Double-click to launch the Auto Pipeline UI.
REM Keep the console open while using the tool. Close it to stop.

cd /d "%~dp0"
"C:\Users\PC\AppData\Local\Programs\Python\Python312\python.exe" -m streamlit run app.py --server.headless=false --browser.gatherUsageStats=false --server.port=8503
pause
