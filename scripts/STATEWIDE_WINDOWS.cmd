@echo off
setlocal
set "WORK=%USERPROFILE%\Downloads\RTK_SachsenAnhalt"
if not exist "%WORK%" mkdir "%WORK%"
echo.
echo === RTK Flurstuecksfinder - Sachsen-Anhalt Datenbank ===
echo Arbeitsordner: %WORK%
echo.
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)
echo 1/3 Amtliche ALKIS-Flurstuecke herunterladen...
%PY% "%~dp0..\tools\download_statewide_alkis.py" "%WORK%\GBIS_Flurstuecke.zip"
if errorlevel 1 goto :error
echo.
echo 2/3 Offline-Datenbank bauen...
%PY% "%~dp0..\tools\build_alkis_db.py" "%WORK%\GBIS_Flurstuecke.zip" "%WORK%\alkis_st.sqlite"
if errorlevel 1 goto :error
echo.
echo 3/3 Datenbank auf den Pi kopieren...
scp "%WORK%\alkis_st.sqlite" pi@rtk.local:/home/pi/rtk-rover/data/alkis_st.sqlite.new
if errorlevel 1 goto :error
ssh -tt pi@rtk.local "sudo systemctl stop rtk-rover && mv /home/pi/rtk-rover/data/alkis_st.sqlite.new /home/pi/rtk-rover/data/alkis_st.sqlite && sudo systemctl start rtk-rover"
echo.
echo FERTIG. Browser: http://rtk.local:5000
pause
exit /b 0
:error
echo.
echo FEHLER. Bitte die letzte Meldung kopieren und im Chat senden.
pause
exit /b 1
