$ErrorActionPreference = 'Stop'
$Work = Join-Path $env:USERPROFILE 'Downloads\RTK_SachsenAnhalt'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Zip = Join-Path $Work 'GBIS_Flurstuecke.zip'
$Db  = Join-Path $Work 'alkis_st.sqlite'
$Url = 'https://www.geodatenportal.sachsen-anhalt.de/gfds_webshare/download/LVermGeo/Geodatenportal/externedaten/GBIS_Flurstuecke.zip'
Write-Host '=== Sachsen-Anhalt ALKIS Komplettupdate ===' -ForegroundColor Cyan
Write-Host "Arbeitsordner: $Work"
if (Get-Command py -ErrorAction SilentlyContinue) { $PyExe='py'; $PyArgs=@('-3') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $PyExe='python'; $PyArgs=@() }
else { throw 'Python wurde unter Windows nicht gefunden.' }
if (-not (Test-Path $Zip)) {
  Write-Host '1/4 Amtliche ALKIS-ZIP herunterladen ...'
  Invoke-WebRequest -Uri $Url -OutFile "$Zip.part"
  Move-Item -Force "$Zip.part" $Zip
} else { Write-Host '1/4 ZIP bereits vorhanden - wird wiederverwendet.' }
Write-Host '2/4 Landesdatenbank bauen ...'
& $PyExe @PyArgs (Join-Path (Split-Path -Parent $Here) 'tools\build_alkis_db.py') $Zip "$Db.new"
if ($LASTEXITCODE -ne 0) { throw 'Datenbankbau fehlgeschlagen.' }
Move-Item -Force "$Db.new" $Db
Write-Host '3/4 Datenbank prüfen ...'
& $PyExe @PyArgs (Join-Path (Split-Path -Parent $Here) 'tools\db_info.py') $Db
Write-Host '4/4 Auf Raspberry Pi kopieren ...'
& scp $Db 'pi@rtk.local:/home/pi/rtk-rover/data/alkis_st.sqlite.new'
if ($LASTEXITCODE -ne 0) { throw 'scp fehlgeschlagen.' }
& ssh -tt 'pi@rtk.local' 'sudo systemctl stop rtk-rover; mv /home/pi/rtk-rover/data/alkis_st.sqlite.new /home/pi/rtk-rover/data/alkis_st.sqlite; sudo systemctl start rtk-rover'
if ($LASTEXITCODE -ne 0) { throw 'Aktivierung auf dem Pi fehlgeschlagen.' }
Write-Host 'FERTIG. Browser: http://rtk.local:5000' -ForegroundColor Green
Read-Host 'Enter zum Schließen'
