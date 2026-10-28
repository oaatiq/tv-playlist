# Rebuild playlist.m3u the safe way: build -> apply overrides -> verify -> only then replace.
#
# The build alone would throw away everything in overrides/overrides.json, so never run
# build_tivimate.py straight into this repo. Run this instead.
#
#   .\rebuild.ps1                 # build from the committed sources\ lists, show the diff
#   .\rebuild.ps1 -PullFromBox    # refresh sources\ off the MINIX box first, then build
#   .\rebuild.ps1 -Force          # skip the confirmation prompt
#   .\rebuild.ps1 -WhatIf         # build and verify, leave playlist.m3u alone
#
# Nothing is pushed anywhere: this only writes playlist.m3u in the working tree. Committing,
# pushing and reloading the player are left to you.

[CmdletBinding()]
param(
    [switch]$PullFromBox,
    [switch]$Force,
    [switch]$WhatIf,
    [string]$Box = '192.168.2.131:5555',
    [string]$BuildScript,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$repo = $PSScriptRoot
if (-not $BuildScript) {
    $BuildScript = Join-Path (Split-Path $repo -Parent) 'playlists\tivimate\build_tivimate.py'
}

$worker  = 'https://ma-tv.oaatiq.workers.dev'
$epgUrl  = 'https://raw.githubusercontent.com/oaatiq/tv-playlist/guide/epg.xml.gz'
$boxDir  = '/storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/userdata/addon_data/pvr.iptvsimple'
$srcDir  = Join-Path $repo 'sources'
$live    = Join-Path $repo 'playlist.m3u'
$stage   = Join-Path $repo '.rebuild'

function Fail($msg) { Write-Host "FAILED: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if (-not (Test-Path $BuildScript)) { Fail "build script not found: $BuildScript" }

# --- 1. optionally refresh the source lists from the box -----------------------------
if ($PullFromBox) {
    Step "pulling the source lists off $Box"
    $adb = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') `
                         -Filter 'adb.exe' -Recurse -ErrorAction SilentlyContinue |
           Select-Object -First 1 -ExpandProperty FullName
    if (-not $adb) { $adb = 'adb' }
    & $adb connect $Box | Out-Null
    # The lists hold accented channel names, so decode adb's output as UTF-8 explicitly
    # rather than trusting whatever code page the console happens to be on.
    $prevEnc = [Console]::OutputEncoding
    [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
    try {
        foreach ($name in @('direct.m3u', 'world_fr_en.m3u')) {
            $dest = Join-Path $srcDir $name
            # exec-out cat, not pull: the Kodi data dir is not world-readable on Android 11
            $lines = & $adb -s $Box exec-out "cat $boxDir/$name"
            if ($LASTEXITCODE -ne 0) { Fail "could not read $name off the box" }
            if (-not $lines) { Fail "$name came back empty" }
            [IO.File]::WriteAllText($dest, ($lines -join "`n") + "`n",
                                    (New-Object Text.UTF8Encoding $false))
            $n = (Select-String -Path $dest -Pattern '^#EXTINF' -AllMatches).Count
            Write-Host ("    {0,-16} {1} channels" -f $name, $n)
        }
    } finally {
        [Console]::OutputEncoding = $prevEnc
    }
}

foreach ($name in @('direct.m3u', 'world_fr_en.m3u')) {
    if (-not (Test-Path (Join-Path $srcDir $name))) {
        Fail "sources\$name is missing - run with -PullFromBox to fetch it"
    }
}

# --- 2. build into a staging dir, never straight over playlist.m3u -------------------
Step 'building from the source lists'
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory $stage | Out-Null

& $Python -I $BuildScript `
    --direct (Join-Path $srcDir 'direct.m3u') `
    --world (Join-Path $srcDir 'world_fr_en.m3u') `
    --out $stage --out-name 'playlist.m3u' `
    --grouping country --maroc-worker $worker --epg-url $epgUrl
if ($LASTEXITCODE -ne 0) { Fail 'build_tivimate.py failed' }

# --- 3. re-apply everything this repo owns -------------------------------------------
Step 'applying overrides'
& $Python -I (Join-Path $repo 'apply_overrides.py') (Join-Path $stage 'playlist.m3u')
if ($LASTEXITCODE -ne 0) { Fail 'apply_overrides.py failed' }

# --- 4. check it, and show what moved against the playlist in use --------------------
Step 'verifying'
& $Python -I (Join-Path $repo 'verify_playlist.py') (Join-Path $stage 'playlist.m3u') --compare $live
if ($LASTEXITCODE -ne 0) { Fail 'verify_playlist.py rejected the result; playlist.m3u left alone' }

# --- 5. replace only once it has passed ----------------------------------------------
if ($WhatIf) {
    Write-Host ''
    Write-Host "-WhatIf: playlist.m3u not touched. Result is in $stage\playlist.m3u" -ForegroundColor Yellow
    exit 0
}
if (-not $Force) {
    Write-Host ''
    $answer = Read-Host 'Replace playlist.m3u with this? [y/N]'
    if ($answer -ne 'y') {
        Write-Host "left alone. Result is in $stage\playlist.m3u" -ForegroundColor Yellow
        exit 0
    }
}

Copy-Item (Join-Path $stage 'playlist.m3u') $live -Force
Remove-Item $stage -Recurse -Force
Write-Host ''
Write-Host 'playlist.m3u updated.' -ForegroundColor Green
Write-Host 'Next, if the diff above looks right:'
Write-Host '    git add -A; git commit -m "Rebuild playlist"; git push'
Write-Host '    then XCIPTV -> Update Contents on the box'
