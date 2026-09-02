@echo off
cd /d "%~dp0"
python translate_epub.py %*
pause
