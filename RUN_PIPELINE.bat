@echo off
cd /d "%~dp0"
echo.
echo  Astik pipeline: input\*.apk -^> encrypt -^> dropper -^> output\
echo.
python auto_pipeline.py
echo.
pause
