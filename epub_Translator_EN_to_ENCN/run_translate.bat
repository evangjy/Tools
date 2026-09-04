@echo off
cd /d "%~dp0"
python translate_epub_free_guard.py %*
if errorlevel 1 (
    echo.
    echo [FAILED] Translation stopped/failed. If any translation completed, the bilingual partial EPUB was preserved.
) else (
    echo.
    echo [DONE] Translation completed successfully.
)
pause
