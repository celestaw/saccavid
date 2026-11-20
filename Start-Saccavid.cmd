@echo off
setlocal
cd /d "%~dp0"
for %%T in (ffmpeg ffprobe ffplay) do (
 where %%T >nul 2>nul || (echo Install FFmpeg with ffplay and add its bin folder to PATH. & goto fail)
)
if not exist ".venv\Scripts\python.exe" (
 py -3.11 -m venv .venv || goto fail
)
".venv\Scripts\python.exe" -c "import PySide6,cv2,numpy,PIL,pygame" >nul 2>nul || (
 ".venv\Scripts\python.exe" -m pip install -r requirements-editor.txt || goto fail
)
".venv\Scripts\python.exe" sacca_editor.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Setup or launch failed. Install Python 3.11 x64 with the py launcher and check the message above.
pause
exit /b 1
