@echo off
cd /d "%~dp0"
echo === Git push (no gh CLI needed) ===
echo Repo: https://github.com/chauminbaba389-byte/encryption.git
echo.
if not exist ".git" (
  git init
  git branch -M main
  git remote add origin https://github.com/chauminbaba389-byte/encryption.git 2>nul
)
git add docs/ pipeline_config.json auto_pipeline.py .gitignore
git add "server-emcryption-main/google-services (43).json"
git status
echo.
echo First time push will open browser for GitHub login.
git commit -m "Update Firebase server-fud + config for Pages" 2>nul
if errorlevel 1 echo Nothing new to commit or commit failed.
git push -u origin main
pause
