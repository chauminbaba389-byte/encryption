@echo off
cd /d "%~dp0"
echo Push encryption project + config.json to GitHub Pages...
echo.
git add docs/config.json docs/.nojekyll pipeline_config.json auto_pipeline.py
git add -A
git status
echo.
set /p MSG=Commit message (Enter = update config): 
if "%MSG%"=="" set MSG=update config.json for Pages
git commit -m "%MSG%"
git push -u origin main
echo.
pause
