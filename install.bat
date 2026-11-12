@echo off
rem Grade Sheet installer.
rem Drag your project's .uproject file onto this file, or run it and paste the path.
rem It copies this plugin into <YourProject>\Plugins\GradeSheet.
setlocal
set "SRC=%~dp0"
set "PROJ=%~1"
if "%PROJ%"=="" set /p "PROJ=Drag your .uproject file here and press Enter: "
set "PROJ=%PROJ:"=%"
if not exist "%PROJ%" (
  echo.
  echo Could not find: %PROJ%
  pause
  exit /b 1
)
for %%I in ("%PROJ%") do set "PROJDIR=%%~dpI"
set "DEST=%PROJDIR%Plugins\GradeSheet"
echo.
echo Installing Grade Sheet into:
echo   %DEST%
robocopy "%SRC%." "%DEST%" /E /XD .git __pycache__ /XF install.bat .gitignore /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
  echo Copy failed.
  pause
  exit /b 1
)
echo.
echo Done. Start (or restart) Unreal and open the project.
echo You will find "Grade Sheet" in the top menu bar and on the toolbar.
pause
