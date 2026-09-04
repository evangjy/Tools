@echo off
cd /d "%~dp0"
python translate_epub.py %*
if errorlevel 1 (
    echo.
    echo [FAILED] Translation did not complete. No partial EPUB was published.
) else (
    echo.
    echo [DONE] Translation completed successfully.
)
pause
