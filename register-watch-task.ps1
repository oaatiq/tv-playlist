# Registers the Windows scheduled task that runs watch_playlist.py (current user, no admin).
#
# Daily at 05:30 and again at 13:30, plus at logon if a run was missed. The boxes are at a
# relative's house and nobody there can diagnose a dead channel, so the published playlist
# has to look after itself: this tests every channel, drops what has been failing for three
# runs in a row, and pushes. XCIPTV re-reads the list on launch once its copy is more than
# about six hours old, so the fix arrives on the TV by itself.
#
# It only commits when something actually changed, so most runs are silent.
#
# Remove with:
#   Unregister-ScheduledTask -TaskName 'tv-playlist - channel health check' -Confirm:$false
#
# Watch what it has been doing:
#   Get-Content C:\Users\aatiq\MINIX-U8K\tv-playlist\logs\watch-*.log -Tail 30
#   git -C C:\Users\aatiq\MINIX-U8K\tv-playlist log --oneline --grep='Health check'

$name    = 'tv-playlist - channel health check'
$dir     = 'C:\Users\aatiq\MINIX-U8K\tv-playlist'
$pythonw = 'C:\Users\aatiq\AppData\Local\Programs\Python\Python314\pythonw.exe'

if (-not (Test-Path $pythonw)) { throw "pythonw not found at $pythonw" }
if (-not (Test-Path (Join-Path $dir 'watch_playlist.py'))) { throw "watch_playlist.py not found in $dir" }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "-I `"$dir\watch_playlist.py`"" -WorkingDirectory $dir
$morning  = New-ScheduledTaskTrigger -Daily -At '05:30'
$midday   = New-ScheduledTaskTrigger -Daily -At '13:30'
$logon    = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $name -Action $action -Trigger @($morning, $midday, $logon) `
    -Settings $settings -Principal $principal -Force `
    -Description 'Tests every channel in the published playlist, removes what stays dead and pushes, so the TVs repair themselves (see watch_playlist.py).' | Out-Null

Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
