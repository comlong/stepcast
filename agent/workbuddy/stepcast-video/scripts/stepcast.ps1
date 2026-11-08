<#
.SYNOPSIS
  Command-line client for the local StepCast app (http://127.0.0.1:8756), for assistants such as WorkBuddy.
  Windows PowerShell 5.1 or later; nothing else to install. StepCast.exe must be running.

.EXAMPLE
  powershell -NoProfile -ExecutionPolicy Bypass -File stepcast.ps1 health
  powershell -NoProfile -ExecutionPolicy Bypass -File stepcast.ps1 project -Project p_123
  powershell -NoProfile -ExecutionPolicy Bypass -File stepcast.ps1 render -Project p_123 -NoSidecar

  Run "stepcast.ps1 help" for all commands. In WorkBuddy, run it with the PowerShell tool: its Bash tool refuses to start PowerShell.
  Pass JSON and long texts as UTF-8 files (-File): when one PowerShell starts another, the double quotes inside an argument are lost.
#>
param(
    [Parameter(Position = 0)][string]$Command = "help",
    [string]$Project = "",
    [string]$From = "",
    [string]$Step = "",
    [string]$Steps = "",
    [int]$Crop = 0,
    [int]$Position = -1,
    [string]$Name = "",
    [string]$Language = "",
    [string]$Voice = "",
    [string]$Title = "",
    [string]$Subtitle = "",
    [string]$Intro = "",
    [string]$Outro = "",
    [string]$Narration = "",
    [string]$Caption = "",
    [string]$Body = "",
    [string]$Image = "",
    [string]$Out = "",
    [string]$File = "",
    [string]$Json = "",
    [string]$Pages = "",
    [string]$Lang = "",
    [string]$Rect = "",
    [string]$Include = "",
    [string]$Zoom = "",
    [string]$Highlight = "",
    [string]$Id = "",
    [int]$Width = 1920,
    [int]$Height = 1080,
    [switch]$NoSidecar,
    [switch]$NoSubSpace,
    [switch]$NoIntro,
    [switch]$NoOutro,
    [switch]$All,
    [switch]$NoWait,
    [switch]$Auto,
    [string]$Live = "",
    [string]$Folder = "",
    [string]$Python = "",
    [string]$Index = "",
    [string]$Network = "",
    [string]$Locale = "",
    [string]$FfmpegFile = "",
    [switch]$AcceptRisk,
    [switch]$NoInstall,
    [switch]$Force,
    [string]$Base = "http://127.0.0.1:8756"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Utf8 = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONIOENCODING = "utf-8"

# the skill's own copy of StepCast (missing in the copy that comes with StepCast.exe) and where the work folder is remembered
$SkillDir = Split-Path -Parent $PSScriptRoot
$SkillApp = Join-Path $SkillDir "app"
$Pointer = if ($env:STEPCAST_POINTER) { $env:STEPCAST_POINTER } else { Join-Path $env:USERPROFILE ".stepcast\home.json" }
# where the Python components come from: in mainland China the mirrors first (the official PyPI is often slow or blocked there),
# elsewhere the official PyPI first
$PyPI = "https://pypi.org/simple"
# Alibaba Cloud first: reachable from abroad too (a "mainland China" user on a trip), where the Tsinghua mirror lists no packages
$ChinaMirrors = @("https://mirrors.aliyun.com/pypi/simple/", "https://pypi.tuna.tsinghua.edu.cn/simple")
function Get-Indexes([string]$net) { if ($net -eq "cn") { return @($ChinaMirrors) + $PyPI } return @($PyPI) + $ChinaMirrors }

function Fail([string]$msg) {
    [Console]::Error.WriteLine("ERROR: $msg")
    exit 1
}

function Log([string]$msg) { [Console]::Error.WriteLine($msg) }

function Read-JsonFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try { return [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8) | ConvertFrom-Json } catch { return $null }
}

function Write-JsonFile([string]$path, $obj) {
    $dir = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($path, ($obj | ConvertTo-Json -Depth 10), $Utf8)
}

function Get-WorkFolder {
    if ($Folder -and [System.IO.Path]::IsPathRooted($Folder)) { return [System.IO.Path]::GetFullPath($Folder) }
    if ($Folder) { return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $Folder)) }
    $p = Read-JsonFile $Pointer
    if ($p -and $p.home) { return [string]$p.home }
    return ""
}

function Get-SkillVersion {
    $f = Join-Path $SkillApp "VERSION"
    if (Test-Path -LiteralPath $f) { return ([System.IO.File]::ReadAllText($f)).Trim() }
    return ""
}

function Same-Path([string]$a, [string]$b) {
    if (-not $a -or -not $b) { return $false }
    return [System.IO.Path]::GetFullPath($a).TrimEnd('\') -ieq [System.IO.Path]::GetFullPath($b).TrimEnd('\')
}

function Invoke-Native([string]$exe, [string[]]$argv, [switch]$Capture) {
    # Windows PowerShell 5.1 turns a native program's stderr into errors, which "Stop" would make fatal
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Capture) {
            $all = @(& $exe @argv 2>&1)
            $stdout = @($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join "`n"
            $err = @($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { [string]$_ }) -join "`n"
            return @{ code = $LASTEXITCODE; out = $stdout; err = $err }
        }
        & $exe @argv 2>&1 | ForEach-Object { [Console]::Error.WriteLine([string]$_) }
        return @{ code = $LASTEXITCODE; out = "" }
    } catch {
        return @{ code = -1; out = $_.Exception.Message }
    } finally {
        $ErrorActionPreference = $old
    }
}

function Find-Python([string]$explicit) {
    # the Python that comes with WorkBuddy first, then the py launcher and python on PATH
    $cands = New-Object System.Collections.Generic.List[string]
    if ($explicit) { $cands.Add($explicit) }
    $wb = Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"
    if (Test-Path -LiteralPath $wb) {
        $cur = Join-Path $wb "current"
        if (Test-Path -LiteralPath $cur -PathType Leaf) { $cands.Add((Join-Path (Join-Path $wb ([System.IO.File]::ReadAllText($cur).Trim())) "python.exe")) }
        Get-ChildItem -LiteralPath $wb -Directory | Sort-Object Name -Descending | ForEach-Object { $cands.Add((Join-Path $_.FullName "python.exe")) }
    }
    if ($env:LOCALAPPDATA) { $cands.Add((Join-Path $env:LOCALAPPDATA "Programs\WorkBuddy\resources\vendor\python\python.exe")) }
    foreach ($cmdName in @("py", "python")) {
        foreach ($c in @(Get-Command $cmdName -CommandType Application -All -ErrorAction SilentlyContinue)) {
            if ($c.Source -notlike "*\WindowsApps\*") { $cands.Add($c.Source) }      # skip the Microsoft Store stub
        }
    }
    foreach ($c in $cands) {
        if (-not $c -or -not (Test-Path -LiteralPath $c)) { continue }
        $argv = @("-c", "import os, sys, venv; print(os.path.realpath(sys.executable)); print('%d.%d' % sys.version_info[:2])")
        if ([System.IO.Path]::GetFileName($c) -ieq "py.exe") { $argv = @("-3") + $argv }
        $r = Invoke-Native $c $argv -Capture
        if ($r.code -ne 0) { continue }
        $lines = @($r.out -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        if ($lines.Count -ge 2 -and [version]$lines[1] -ge [version]"3.10") { return @{ exe = $lines[0]; version = $lines[1] } }
    }
    return $null
}

# the port from the work folder's settings, unless -Base is given
$wf = Get-WorkFolder
if (-not $PSBoundParameters.ContainsKey("Base") -and $wf) {
    $c = Read-JsonFile (Join-Path $wf "config.json")
    if ($c -and $c.server_port) { $Base = "http://127.0.0.1:$($c.server_port)" }
}

# An assistant's sandbox (WorkBuddy) lets programs reach the internet only through its own local proxy, given to them as HTTPS_PROXY,
# on a port that changes every session. StepCast doesn't see that variable when it runs outside the sandbox's commands (started by
# start, or by double-click), so every call tells it the current one; StepCast uses it for the online voice while it answers.
$assistantProxy = @($env:HTTPS_PROXY, $env:HTTP_PROXY, $env:ALL_PROXY) | Where-Object { $_ } | Select-Object -First 1
if ($assistantProxy -and $wf -and (Test-Path -LiteralPath (Join-Path $wf "runtime"))) {
    $pf = Join-Path $wf "runtime\assistant-proxy.txt"
    try {
        $old = if (Test-Path -LiteralPath $pf) { [System.IO.File]::ReadAllText($pf).Trim() } else { "" }
        if ($old -ne $assistantProxy) { [System.IO.File]::WriteAllText($pf, $assistantProxy, $Utf8) }
    } catch {}
}

function Try-Health {
    try {
        $r = Invoke-WebRequest -Uri "$Base/api/health" -UseBasicParsing -TimeoutSec 3
        return [System.Text.Encoding]::UTF8.GetString($r.RawContentStream.ToArray()) | ConvertFrom-Json
    } catch { return $null }
}

# ---- ffmpeg: StepCast uses <work folder>\ffmpeg first, then PATH, then the downloaded imageio-ffmpeg ----

function Test-Ffmpeg([string]$exe) {
    if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return $false }
    $r = Invoke-Native $exe @("-hide_banner", "-version") -Capture
    return ($r.code -eq 0 -and $r.out -match "ffmpeg version")
}

function Get-WorkFfmpeg([string]$ffDir) {
    if (-not (Test-Path -LiteralPath $ffDir)) { return "" }
    $f = Get-ChildItem -LiteralPath $ffDir -Recurse -File -Filter "ffmpeg.exe" | Sort-Object { $_.FullName.Length } | Select-Object -First 1
    if ($f -and (Test-Ffmpeg $f.FullName)) { return $f.FullName }
    return ""
}

function Get-VenvFfmpeg([string]$venvPy) {
    if ($env:STEPCAST_TEST_NO_DOWNLOAD) { return "" }          # tests only: as if every download had failed
    $r = Invoke-Native $venvPy @("-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())") -Capture
    if ($r.code -ne 0) { return "" }
    $exe = ($r.out -split "`n")[-1].Trim()
    if (Test-Ffmpeg $exe) { return $exe }
    return ""
}

function Get-PathFfmpeg {
    $c = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c -and (Test-Ffmpeg $c.Source)) { return $c.Source }
    return ""
}

function Get-FfmpegSources {
    $f = Join-Path $SkillApp "ffmpeg-sources.json"
    $s = Read-JsonFile $f
    if (-not $s) { Fail "$f is missing: the skill is incomplete" }
    return $s
}

function Get-FfmpegHelp([string]$net, [string]$ffDir) {
    $s = Get-FfmpegSources
    return [ordered]@{ download = @(if ($net -eq "cn") { $s.cn } else { $s.global }); put_into = $ffDir
                       then = "setup -FfmpegFile <the downloaded file> (a .whl is checked against its SHA-256, .zip / .gz are unpacked), or put ffmpeg.exe into $ffDir and run setup again" }
}

function Import-Ffmpeg([string]$file, [string]$ffDir) {
    # a file the user downloaded: the imageio-ffmpeg wheel (SHA-256 checked), a .zip build, a .gz (npm mirror) or ffmpeg.exe itself
    $src = Full-Path $file
    if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { Fail "file not found: $src" }
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    $dst = Join-Path $ffDir "ffmpeg.exe"
    $ext = [System.IO.Path]::GetExtension($src).ToLower()
    if ($ext -eq ".whl" -or $ext -eq ".zip") {
        if ($ext -eq ".whl" -and (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLower() -ne (Get-FfmpegSources).sha256) {
            Fail "$src is not the imageio-ffmpeg package StepCast expects (its SHA-256 differs): download it again from the links in ffmpeg_help"
        }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [System.IO.Compression.ZipFile]::OpenRead($src)
        try {
            $exes = @($zip.Entries | Where-Object { $_.Name -match '^(ffmpeg|ffprobe)(-win-x86_64-v[\d.]+)?\.exe$' } | Sort-Object { $_.FullName.Length })
            $ff = $exes | Where-Object { $_.Name -like "ffmpeg*" } | Select-Object -First 1
            if (-not $ff) { Fail "there is no ffmpeg.exe in $src" }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($ff, $dst, $true)
            $fp = $exes | Where-Object { $_.Name -like "ffprobe*" } | Select-Object -First 1
            if ($fp) { [System.IO.Compression.ZipFileExtensions]::ExtractToFile($fp, (Join-Path $ffDir "ffprobe.exe"), $true) }
        } finally { $zip.Dispose() }
    } elseif ($ext -eq ".gz") {
        $in = [System.IO.File]::OpenRead($src)
        $gz = New-Object System.IO.Compression.GZipStream($in, [System.IO.Compression.CompressionMode]::Decompress)
        $dstStream = [System.IO.File]::Create($dst)
        try { $gz.CopyTo($dstStream) } finally { $dstStream.Dispose(); $gz.Dispose(); $in.Dispose() }
    } else {
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }
    if (-not (Test-Ffmpeg $dst)) { Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue; Fail "$src doesn't contain a working ffmpeg" }
    Log "ffmpeg from $src is in $ffDir"
}

function Copy-Ffmpeg([string]$exe, [string]$ffDir) {
    # an ffmpeg on PATH: copied into the work folder (with ffprobe and the DLLs of a shared build), so StepCast still finds it when it
    # is started with another PATH (double-click, WMI) or the original is removed; the original itself if the copy doesn't run
    New-Item -ItemType Directory -Force -Path $ffDir | Out-Null
    $dir = Split-Path -Parent $exe
    $copied = @()
    foreach ($f in @(Get-ChildItem -LiteralPath $dir -File | Where-Object { $_.Name -in @("ffmpeg.exe", "ffprobe.exe") -or $_.Extension -ieq ".dll" })) {
        Copy-Item -LiteralPath $f.FullName -Destination $ffDir -Force
        $copied += Join-Path $ffDir $f.Name
    }
    $copy = Join-Path $ffDir "ffmpeg.exe"
    if (Test-Ffmpeg $copy) { return $copy }
    foreach ($c in $copied) { Remove-Item -LiteralPath $c -Force -ErrorAction SilentlyContinue }
    return $exe
}

function Test-HasFfmpeg($state, [string]$work) {
    if ($state -and $state.ffmpeg -and (Test-Path -LiteralPath ([string]$state.ffmpeg))) { return $true }
    return [bool]((Get-WorkFfmpeg (Join-Path $work "ffmpeg")) -or (Get-PathFfmpeg))
}

function Call([string]$Method, [string]$Path, $Data = $null, [string]$ContentType = "") {
    # Invoke-WebRequest + manual UTF-8 decoding: Windows PowerShell 5.1 would read a response without a charset as Latin-1
    $req = @{ Uri = "$Base$Path"; Method = $Method; UseBasicParsing = $true; TimeoutSec = 120 }
    if ($Data -is [byte[]]) {
        $req.Body = $Data
        $req.ContentType = $ContentType
    } elseif ($null -ne $Data) {
        $text = if ($Data -is [string]) { $Data } else { $Data | ConvertTo-Json -Depth 30 -Compress }
        $req.Body = $Utf8.GetBytes($text)
        $req.ContentType = "application/json; charset=utf-8"
    }
    try {
        $r = Invoke-WebRequest @req
    } catch [System.Net.WebException] {
        $resp = $_.Exception.Response
        if ($null -eq $resp) { Fail "StepCast is not reachable at $Base. Start StepCast.exe first. ($($_.Exception.Message))" }
        # Windows PowerShell 5.1 has already read the answer into ErrorDetails (its stream is empty then); newer ones leave the stream
        $detail = if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $_.ErrorDetails.Message } else {
            try { (New-Object System.IO.StreamReader($resp.GetResponseStream(), [System.Text.Encoding]::UTF8)).ReadToEnd() } catch { "" }
        }
        try { $detail = ($detail | ConvertFrom-Json).detail } catch {}
        if ($detail -isnot [string]) { $detail = $detail | ConvertTo-Json -Depth 10 -Compress }
        Fail "HTTP $([int]$resp.StatusCode) $Method $Path : $detail"
    }
    $bytes = $r.RawContentStream.ToArray()
    $s = [System.Text.Encoding]::UTF8.GetString($bytes)
    if ($s.Length -eq 0) { return $null }
    if ($r.Headers["Content-Type"] -like "application/json*") { return $s | ConvertFrom-Json }
    return $s
}

function Upload([string]$Path, [string]$file) {
    # multipart/form-data by hand: Invoke-WebRequest -Form needs PowerShell 7
    $boundary = "----StepCast" + [guid]::NewGuid().ToString("N")
    $fname = [System.IO.Path]::GetFileName($file)
    $head = "--$boundary`r`nContent-Disposition: form-data; name=`"file`"; filename=`"$fname`"`r`nContent-Type: application/octet-stream`r`n`r`n"
    $ms = New-Object System.IO.MemoryStream
    foreach ($part in @($Utf8.GetBytes($head), [System.IO.File]::ReadAllBytes($file), $Utf8.GetBytes("`r`n--$boundary--`r`n"))) {
        $ms.Write($part, 0, $part.Length)
    }
    return Call POST $Path $ms.ToArray() "multipart/form-data; boundary=$boundary"
}

function Show($obj) { $obj | ConvertTo-Json -Depth 30 }

function Need([string]$value, [string]$what) { if (-not $value) { Fail "missing -$what" } }

function Wait-StepCastJob([string]$jid, [string]$label, [bool]$always = $false) {
    if ($NoWait -and -not $always) { return Call GET "/api/jobs/$jid" }
    $last = ""
    while ($true) {
        $j = Call GET "/api/jobs/$jid"
        if ($j.status -ne "pending" -and $j.status -ne "running") { break }
        $msg = "$label $([int]($j.progress * 100))% $($j.message)"
        if ($msg -ne $last) { [Console]::Error.WriteLine($msg); $last = $msg }
        Start-Sleep -Seconds 2
    }
    if ($j.status -ne "done") { Fail "$label failed: $($j.error)" }
    return $j
}

function Get-Proj([string]$projId) { Need $projId "Project"; return Call GET "/api/projects/$projId" }

function Find-Step($proj, [string]$ref) {
    Need $ref "Step"
    foreach ($s in $proj.steps) { if ($s.id -eq $ref) { return $s } }
    if ($ref -match '^\d+$') { foreach ($s in $proj.steps) { if ($s.index -eq [int]$ref) { return $s } } }
    Fail "step not found: $ref (use the step id or its index; indexes start at 0)"
}

function Bool([string]$v) { return @("1", "true", "yes", "on") -contains $v.ToLower() }

function Web-Info($s) {
    # a slide of an HTML deck: what can be clicked, the narration's sentences and the clicks, each numbered as `web -File` takes them
    $w = $s.web
    $ctl = @($w.controls | Where-Object { $null -ne $_ })
    $controls = @(for ($i = 0; $i -lt $ctl.Count; $i++) {
        $c = $ctl[$i]
        $label = if ($c.text) { $c.text } elseif ($c.name) { $c.name } else { $c.tag }
        "$i $label"
    })
    $blk = @($w.blocks | Where-Object { $null -ne $_ })
    $blocks = @(for ($i = 0; $i -lt $blk.Count; $i++) { "$i $($blk[$i].key)" })
    $units = @($w.units | Where-Object { $null -ne $_ })
    $sentences = @(for ($i = 0; $i -lt $units.Count; $i++) { "$i $($units[$i].text)" })
    $clicks = @(foreach ($a in @($w.actions)) {
        if ($null -eq $a) { continue }
        $what = if ($a.kind -eq "next") { "next part" } elseif ($a.kind -eq "focus") { "highlight block $($a.control)" } else { "$($a.kind) control $($a.control)" }
        $when = if ($a.unit -lt 0) { "at character $($a.pos)" } elseif ($a.unit -ge $units.Count) { "after the narration" } else { "at sentence $($a.unit)" }
        "$what $when"
    })
    return [ordered]@{ live = [bool]$w.live; set_by_hand = [bool]$w.manual; controls = $controls; blocks = $blocks; sentences = $sentences; clicks = $clicks }
}

function Full-Path([string]$path) {
    # .NET resolves relative paths against the process folder, not PowerShell's current location
    if ([System.IO.Path]::IsPathRooted($path)) { return [System.IO.Path]::GetFullPath($path) }
    return [System.IO.Path]::GetFullPath((Join-Path (Get-Location).ProviderPath $path))
}

function Read-Text([string]$path) {
    $full = Full-Path $path
    if (-not (Test-Path -LiteralPath $full)) { Fail "file not found: $full" }
    return [System.IO.File]::ReadAllText($full, [System.Text.Encoding]::UTF8)
}

function Read-Json([string]$text, [string]$what) {
    try { return $text | ConvertFrom-Json }
    catch {
        if ($text -match '[{\[]' -and $text -notmatch '"') {
            Fail "$what is not valid JSON: its double quotes were lost on the way (PowerShell drops them when it starts another PowerShell). Write the JSON to a UTF-8 file and pass -File, or use -Title / -Language / -Voice ..."
        }
        Fail "$what is not valid JSON: $($_.Exception.Message)"
    }
}

function Write-Text([string]$path, [string]$text) {
    $full = Full-Path $path
    [System.IO.File]::WriteAllText($full, $text, $Utf8)
    return $full
}

function Parse-Pages([string]$spec) {
    # "1-3,5" -> 1,2,3,5 (slide numbers as in PowerPoint, starting at 1)
    $nums = foreach ($part in ($spec -split "[,\s]+" | Where-Object { $_ })) {
        if ($part -match '^(\d+)-(\d+)$') { [int]$Matches[1]..[int]$Matches[2] }
        elseif ($part -match '^\d+$') { [int]$part }
        else { Fail "-Pages: '$part' is not a page number or range like 2-5" }
    }
    return @($nums | Sort-Object -Unique)
}

function Draw-Slide([string]$path, [string]$title, [string]$payload) {
    # A 1920x1080 page in StepCast's look: dark background, panel, accent bar, title and lines of text.
    # Lines starting with spaces are drawn smaller and indented; lines starting with "·" or "-" are dimmed.
    Add-Type -AssemblyName System.Drawing
    $W = 1920; $H = 1080
    $bmp = New-Object System.Drawing.Bitmap($W, $H)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = "AntiAlias"
    $g.TextRenderingHint = "AntiAliasGridFit"
    $g.Clear([System.Drawing.Color]::FromArgb(14, 17, 22))
    $panel = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(18, 22, 29))
    $g.FillRectangle($panel, 42, 36, $W - 84, $H - 176)
    $accent = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(255, 92, 57))
    $g.FillRectangle($accent, 135, 108, 70, 12)
    $family = "Microsoft YaHei"
    $fT = New-Object System.Drawing.Font($family, 46, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Point)
    $fB = New-Object System.Drawing.Font($family, 31, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Point)
    $fS = New-Object System.Drawing.Font($family, 27, [System.Drawing.FontStyle]::Regular, [System.Drawing.GraphicsUnit]::Point)
    $cT = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(245, 247, 250))
    $cB = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(215, 220, 228))
    $cD = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(160, 167, 178))
    $g.DrawString($title, $fT, $cT, 231, 70)
    $y = 235
    foreach ($raw in ($payload -replace "`r", "" -split "`n")) {
        $line = $raw.TrimEnd()
        if (-not $line) { $y += 26; continue }
        $ind = $raw.Length - $raw.TrimStart(" ").Length
        $t = $line.Trim()
        $brush = if ($t.StartsWith("·") -or $t.StartsWith("-")) { $cD } else { $cB }
        $font = if ($ind -eq 0) { $fB } else { $fS }
        $g.DrawString($t, $font, $brush, 150 + $ind * 20, $y)
        $y += if ($ind -eq 0) { 66 } else { 56 }
    }
    $g.Dispose()
    $full = Full-Path $path
    $bmp.Save($full, [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
    return $full
}

switch ($Command.ToLower()) {

    "help" {
@"
StepCast client — commands. Steps are named by id (s_…) or index (starting at 0).
  status                                   what to do next: first-use | update | start | ready | start-exe
  setup     -Folder D:\StepCast -Network cn|global -AcceptRisk [-Locale zh-CN] [-Index <PyPI mirror>] [-Python python.exe]
                                           install StepCast into the work folder (Python environment, ffmpeg, program,
                                           settings in the computer's display language or -Locale); -Network cn = mainland
                                           China (Chinese PyPI mirrors first). Again after a skill update: no other parameters.
                                           No ffmpeg could be downloaded: -FfmpegFile <.whl|.zip|.gz|.exe the user downloaded>,
                                           or ffmpeg.exe put into <work folder>\ffmpeg (an ffmpeg on PATH is used too)
  start | stop [-Force]                    start StepCast from the work folder / stop it (-Force: even while it renders)
  health                                   version, whether the built-in AI is off, data folder
  ai-off | ai-on                           switch StepCast's own AI off / on (off = this assistant writes all texts)
  projects                                 list projects
  project   -Project P                     steps of a project (index, id, kind, clicked element / slide text and notes, narration)
  shot      -Project P -Step S             local path of a step's screenshot or slide picture (look at it before writing)
  new       -Name N [-Language zh-CN] [-Voice zh-CN-XiaoxiaoNeural]   new empty project
  import    -File deck.pptx [-Name N] [-Language zh-CN] [-Voice ..] [-Pages "1-5,8"]
                                           PPT / PDF / HTML deck (.html, or a .zip of its folder) -> new project, one step
                                           per slide; speaker notes become the narration, slides without notes stay empty
                                           (no AI, no voice-over yet). HTML slides are recorded moving when rendered
  web       -Project P -Step S [-File clicks.json | -Auto | -Live true|false]
                                           an HTML slide: its controls, narration sentences and clicks, numbered from 0;
                                           -File sets the clicks: {"actions": [{"kind": "click", "control": 0, "unit": 1}]}
                                           (kind click|hover|next|focus, focus = highlight, its "control" numbers the blocks;
                                           unit = the sentence it starts with; the number of sentences = after the
                                           narration), -Auto places them from the notes and narration again,
                                           -Live false = a still picture (true = moving again)
  copy      -Project P -From SRC -Steps "3,5,s_ab12" [-Crop 22] [-Position i]   copy recorded steps into P
  crop      -Project P -Crop 22 [-Steps "id,id"]        cut pixels from every screenshot edge (once!)
  slide     -Out page.png -Title T -File lines.txt      draw a 1920x1080 text page as PNG (or -Body "line`nline")
  image     -Project P -Image page.png [-Title T] [-File narration.txt] [-Position i]   insert a picture as a slide step
  step      -Project P -Step S [-Title] [-File narration.txt | -Narration] [-Caption] [-Include true|false]
            [-Zoom true|false] [-Highlight true|false] [-Rect "x,y,w,h"]
  delete    -Project P -Step S                          remove a step
  set       -Project P [-File project.json] [-Title] [-Subtitle] [-Intro] [-Outro] [-Language] [-Voice]
            project.json e.g. {"intro": "..", "outro": "..", "settings": {"intro_enabled": true}}
  script-export -Project P -Out script.json             all texts as JSON (edit, then import)
  script-import -Project P -File script.json            write titles / narration / intro / outro back (steps matched by id)
  voice     -Project P [-All] [-Voice V]                voice-over (Edge voices; -All = redo every step)
  render    -Project P [-Width 1920 -Height 1080] [-NoSidecar] [-NoSubSpace] [-NoIntro] [-NoOutro]
  sentences -Project P -Out sentences.json              subtitle sentences of the rendered video, for translation
  subs-import -Project P -Lang en-US -File texts.json   second-language subtitles: JSON {"key":..,"texts":[..]}, key from sentences
  job       -Id J                                       state of a background job
Add -NoWait to voice / render to return at once. Pass JSON and long texts as UTF-8 files (-File), not on the command line.
"@
    }

    "status" {
        $work = Get-WorkFolder
        $state = if ($work) { Read-JsonFile (Join-Path $work "setup.json") } else { $null }
        $h = Try-Health
        $skillVersion = Get-SkillVersion
        $hasApp = Test-Path -LiteralPath (Join-Path $SkillApp "app.py")
        $next = if (-not $state) { if ($h) { "ready" } elseif ($hasApp) { "first-use" } else { "start-exe" } }
                elseif ($hasApp -and $state.version -ne $skillVersion) { "update" }
                elseif (-not (Test-HasFfmpeg $state $work)) { "ffmpeg" }
                elseif (-not $h) { "start" } else { "ready" }
        $server = if ($h) { [ordered]@{ version = $h.version; data_dir = $h.data_dir; ai_disabled = $h.ai_disabled } } else { $null }
        if ($h) {
            # the proxy the online voice goes through (null = direct; older versions don't say)
            try { $server.voice_proxy = (Invoke-RestMethod -Uri "$Base/api/network" -UseBasicParsing -TimeoutSec 10).voice_proxy } catch {}
        }
        $result = [ordered]@{ next = $next; skill_version = $skillVersion; work_folder = $work
                           installed_version = if ($state) { $state.version } else { "" }
                           network = if ($state -and $state.network) { $state.network } else { "" }
                           computer_language = (Get-UICulture).Name; server = $server; base = $Base }
        if ($next -eq "ffmpeg") { $result.ffmpeg_help = Get-FfmpegHelp ([string]$state.network) (Join-Path $work "ffmpeg") }
        Show $result
    }

    "setup" {
        if (-not (Test-Path -LiteralPath (Join-Path $SkillApp "app.py"))) { Fail "this copy of the skill has no StepCast program (no app folder): ask the user to start StepCast.exe instead" }
        $work = Get-WorkFolder
        if (-not $work) { Fail "missing -Folder: ask the user which folder StepCast should use (for example $env:USERPROFILE\StepCast)" }
        if ($work.StartsWith($SkillDir, [System.StringComparison]::OrdinalIgnoreCase)) { Fail "the work folder must not be inside the skill folder: updating the skill would replace it" }
        $statePath = Join-Path $work "setup.json"
        $state = Read-JsonFile $statePath
        if (-not $AcceptRisk -and -not ($state -and $state.accepted_at)) { Fail "first use: show the user the notice (SKILL.md, First use) and run setup with -AcceptRisk once they agree" }
        $net = if ($Network) { $Network.ToLower() } elseif ($state -and $state.network) { [string]$state.network } else { "" }
        if (@("cn", "global") -notcontains $net) { Fail "missing -Network: ask the user whether this computer is in mainland China (-Network cn) or elsewhere (-Network global)" }
        $indexes = @(@($Index, $env:PIP_INDEX_URL) | Where-Object { $_ })
        if (-not $Index) { $indexes += Get-Indexes $net }
        if ($NoInstall -and -not $Python) { Fail "-NoInstall needs -Python: the Python whose installed packages StepCast should use" }
        $h = Try-Health
        if ($h -and (Same-Path $h.data_dir (Join-Path $work "projects"))) { Fail "StepCast is running from $work : run stop first (once its renders have finished)" }
        $runtime = Join-Path $work "runtime"
        New-Item -ItemType Directory -Force -Path $runtime | Out-Null

        # 1. Python environment: rebuilt only when the requirements changed or it no longer works
        $venv = Join-Path $runtime "venv"
        $venvPy = Join-Path $venv "Scripts\python.exe"
        $req = Join-Path $SkillApp "requirements.txt"
        $ffReq = Join-Path $SkillApp "ffmpeg-requirements.txt"
        $reqHash = (Get-FileHash -LiteralPath $req -Algorithm SHA256).Hash + (Get-FileHash -LiteralPath $ffReq -Algorithm SHA256).Hash
        $probe = @("-c", "import fastapi, uvicorn, PIL, pymupdf, pptx, edge_tts, numpy, win32com.client")
        $pip = @("-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--prefer-binary", "--timeout", "60", "--retries", "2")
        $basePython = if ($state) { [string]$state.python } else { "" }
        $fresh = (Test-Path -LiteralPath $venvPy) -and $state -and $state.requirements -eq $reqHash -and -not $Python -and (Invoke-Native $venvPy $probe -Capture).code -eq 0
        if (-not $fresh) {
            $py = Find-Python $Python
            if (-not $py) { Fail "no Python 3.10 or later found. WorkBuddy brings its own; otherwise install Python, or pass -Python C:\path\to\python.exe" }
            $basePython = $py.exe
            if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
            Log "creating the Python environment with $($py.exe) (Python $($py.version))"
            if ((Invoke-Native $py.exe @("-m", "venv", $venv)).code -ne 0) { Fail "could not create the Python environment in $venv" }
            $purelib = @("-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])")
            if ($NoInstall) {
                # offline / development: the packages of the given Python's environment are used as they are
                $shared = (Invoke-Native $Python $purelib -Capture).out.Trim()
                $own = (Invoke-Native $venvPy $purelib -Capture).out.Trim()
                [System.IO.File]::WriteAllText((Join-Path $own "stepcast_shared.pth"), "import site; site.addsitedir(r'$shared')`n", $Utf8)
            } else {
                $done = $false
                foreach ($ix in $indexes) {
                    Log "installing StepCast's components from $ix (about 70 MB the first time, a few minutes)"
                    if ((Invoke-Native $venvPy ($pip + @("-i", $ix, "-r", $req))).code -eq 0) { $done = $true; break }
                    Log "failed with $ix"
                }
                if (-not $done) { Fail "installing the components failed with every source ($($indexes -join ', ')). Check the network or proxy, or pass -Index with the company's PyPI mirror" }
            }
        }
        $check = Invoke-Native $venvPy $probe -Capture
        if ($check.code -ne 0) { Fail "the Python environment in $venv is incomplete (run setup again): $($check.err)$($check.out)" }

        # 2. ffmpeg: one in the work folder (put there by the user, or given with -FfmpegFile); otherwise the download, checked
        #    against its SHA-256; when every source fails, one already on this computer (PATH), copied into the work folder
        $ffDir = Join-Path $work "ffmpeg"
        if ($FfmpegFile) { Import-Ffmpeg $FfmpegFile $ffDir }
        $ffmpeg = Get-WorkFfmpeg $ffDir
        $ffSource = if ($ffmpeg) { "work folder" } else { "" }
        if (-not $ffmpeg) {
            $ffmpeg = Get-VenvFfmpeg $venvPy
            if (-not $ffmpeg -and -not $NoInstall -and -not $env:STEPCAST_TEST_NO_DOWNLOAD) {
                foreach ($ix in $indexes) {
                    Log "installing ffmpeg from $ix (imageio-ffmpeg, about 30 MB, checked against its SHA-256)"
                    if ((Invoke-Native $venvPy ($pip + @("-i", $ix, "--no-deps", "--require-hashes", "-r", $ffReq))).code -eq 0) { break }
                    Log "failed with $ix"
                }
                $ffmpeg = Get-VenvFfmpeg $venvPy
            }
            if ($ffmpeg) { $ffSource = "download (imageio-ffmpeg)" }
        }
        if (-not $ffmpeg) {
            $onPath = Get-PathFfmpeg
            if ($onPath) {
                Log "no ffmpeg could be downloaded: using the one on this computer, $onPath"
                $ffmpeg = Copy-Ffmpeg $onPath $ffDir
                $ffSource = "this computer: $onPath"
            }
        }

        # 3. the program and the Chrome extension: replaced by every setup
        $appDst = Join-Path $runtime "app"
        if (Test-Path -LiteralPath $appDst) { Remove-Item -LiteralPath $appDst -Recurse -Force }
        Copy-Item -LiteralPath $SkillApp -Destination $appDst -Recurse
        $extDst = Join-Path $work "extension"
        if (Test-Path -LiteralPath $extDst) { Remove-Item -LiteralPath $extDst -Recurse -Force }
        Copy-Item -LiteralPath (Join-Path $SkillDir "extension") -Destination $extDst -Recurse

        # 4. settings: written once, never touched by an update. Interface and narration in the computer's display language;
        #    the assistant writes the texts, so StepCast's own AI is off
        $cfgPath = Join-Path $work "config.json"
        $written = $null
        if (-not (Test-Path -LiteralPath $cfgPath)) {
            $loc = if ($Locale) { $Locale } else { (Get-UICulture).Name }
            $env:VT_HOME = $work
            $r = Invoke-Native $venvPy @((Join-Path $appDst "app.py"), "--init-settings", $loc) -Capture
            Remove-Item Env:VT_HOME
            if ($r.code -ne 0) { Fail "could not write the settings: $($r.err)$($r.out)" }
            $written = ($r.out -split "`n")[-1] | ConvertFrom-Json
        }

        # 5. double-click to start. ASCII only: cmd reads batch files in the OEM code page, and %~dp0 keeps any folder name
        $launcher = @(
            "@echo off",
            "rem StepCast: keep this window open while you use StepCast; closing it stops StepCast.",
            "title StepCast",
            "set ""VT_HOME=%~dp0.""",
            """%~dp0runtime\venv\Scripts\python.exe"" ""%~dp0runtime\app\app.py"" %*",
            "if errorlevel 1 pause"
        ) -join "`r`n"
        [System.IO.File]::WriteAllText((Join-Path $work "StepCast.cmd"), $launcher + "`r`n", [System.Text.Encoding]::ASCII)

        $accepted = if ($state -and $state.accepted_at) { [string]$state.accepted_at } else { (Get-Date).ToString("s") }
        Write-JsonFile $statePath ([ordered]@{ version = Get-SkillVersion; accepted_at = $accepted; network = $net; python = $basePython
                                               requirements = $reqHash; ffmpeg = $ffmpeg; ffmpeg_source = $ffSource
                                               updated_at = (Get-Date).ToString("s") })
        Write-JsonFile $Pointer ([ordered]@{ home = $work })
        $result = [ordered]@{ work_folder = $work; version = Get-SkillVersion; network = $net; package_indexes = @($indexes)
                           python = $basePython; ffmpeg = $ffmpeg; ffmpeg_source = $ffSource; start = Join-Path $work "StepCast.cmd"
                           extension = $extDst; settings = $cfgPath; new_settings = $written }
        # a workaround someone put into the environment earlier (e.g. a proxy patch): StepCast doesn't need it any more
        $foreign = Join-Path $venv "Lib\site-packages\sitecustomize.py"
        if (Test-Path -LiteralPath $foreign) {
            $result.note = "$foreign is not part of StepCast (an earlier workaround?). StepCast now finds the proxy itself; ask the user whether to delete it"
        }
        if (-not $ffmpeg) {
            # everything else is in place: once the user has an ffmpeg, setup again only takes seconds
            $result.ffmpeg_help = Get-FfmpegHelp $net $ffDir
            Show $result
            Fail "everything is installed except ffmpeg (no source could be reached and there is none on this computer). Tell the user where to download it and where to put it (ffmpeg_help), then run setup again; with a downloaded file: setup -FfmpegFile <file>"
        }
        Show $result
    }

    "start" {
        $h = Try-Health
        if ($h) {
            Show ([ordered]@{ running = $true; already = $true; version = $h.version; data_dir = $h.data_dir; editor = "$Base/" })
            break
        }
        $work = Get-WorkFolder
        $launcher = if ($work) { Join-Path $work "StepCast.cmd" } else { "" }
        if (-not $launcher -or -not (Test-Path -LiteralPath $launcher)) { Fail "StepCast isn't set up on this computer yet: run status, then setup (SKILL.md, First use)" }
        $line = "cmd.exe /c """"$launcher"" --no-browser"""
        $how = ""
        # through WMI the new window doesn't belong to this command, so it keeps running when the command ends
        try {
            $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $line; CurrentDirectory = $work }
            if ($r.ReturnValue -eq 0) { $how = "wmi" }
        } catch {}
        if (-not $how) {
            Start-Process -FilePath "cmd.exe" -ArgumentList "/c """"$launcher"" --no-browser""" -WorkingDirectory $work
            $how = "start-process"
        }
        for ($i = 0; $i -lt 60 -and -not $h; $i++) { Start-Sleep -Seconds 1; $h = Try-Health }
        if (-not $h) { Fail "StepCast did not start within 60 s. Ask the user to double-click $launcher (its window shows what went wrong)" }
        Show ([ordered]@{ running = $true; started_with = $how; version = $h.version; data_dir = $h.data_dir; editor = "$Base/" })
    }

    "stop" {
        if (-not (Try-Health)) { Show @{ running = $false }; break }
        $busy = @((Call GET "/api/jobs").jobs | Where-Object { $_.status -eq "pending" -or $_.status -eq "running" })
        if ($busy.Count -and -not $Force) { Fail "StepCast is busy ($(($busy | ForEach-Object { $_.kind }) -join ', ')): wait until it has finished, or pass -Force" }
        $work = Get-WorkFolder
        $appPy = if ($work) { (Join-Path $work "runtime\app\app.py").ToLower() } else { "" }
        $port = ([uri]$Base).Port
        $stopped = @()
        foreach ($procId in @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)) {
            $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId"
            # only a StepCast started from the work folder (StepCast.cmd -> venv python -> python running app.py)
            if (-not $p -or -not $appPy -or -not $p.CommandLine -or -not $p.CommandLine.ToLower().Contains($appPy)) { continue }
            $chain = @($p)
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)"
            if ($parent -and $parent.CommandLine -like "*app.py*") { $chain += $parent; $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($parent.ParentProcessId)" }
            if ($parent -and $parent.Name -ieq "cmd.exe" -and $parent.CommandLine -like "*StepCast.cmd*") { $chain += $parent }
            foreach ($c in $chain) { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue; $stopped += $c.ProcessId }
        }
        if (-not $stopped.Count) { Fail "the StepCast on port $port wasn't started from the work folder: ask the user to close its window" }
        for ($i = 0; $i -lt 20 -and (Try-Health); $i++) { Start-Sleep -Milliseconds 500 }
        Show ([ordered]@{ running = [bool](Try-Health); stopped = $stopped })
    }

    "health" { Show (Call GET "/api/health") }

    "ai-off" { $null = Call POST "/api/settings" @{ ai_disabled = $true }; Show @{ ai_disabled = $true } }
    "ai-on" { $null = Call POST "/api/settings" @{ ai_disabled = $false }; Show @{ ai_disabled = $false } }

    "projects" { Show ((Call GET "/api/projects").projects | Select-Object id, name, source, updated_at) }

    "project" {
        $p = Get-Proj $Project
        $rows = foreach ($s in $p.steps) {
            $row = [ordered]@{ index = $s.index; id = $s.id; kind = $s.kind; include = $s.include }
            if ($s.kind -eq "slide") {
                $row.slide_text = $s.slide_text; $row.notes = $s.slide_notes
                if ($s.web) { $row.html = Web-Info $s }
            } else {
                $row.clicked = if ($s.target) { $s.target.text } else { "" }
                $row.rect = if ($s.target -and $s.target.rect) { "{0:N0},{1:N0},{2:N0},{3:N0}" -f $s.target.rect.x, $s.target.rect.y, $s.target.rect.w, $s.target.rect.h } else { "" }
                $row.value = $s.value; $row.page = $s.page_title; $row.url = $s.url
            }
            $row.title = $s.title; $row.narration = $s.narration; $row.has_voice = [bool]$s.audio
            $row
        }
        Show ([ordered]@{ id = $p.id; name = $p.name; source = $p.source; language = $p.language; voice = $p.voice; title = $p.title
                          intro = $p.intro; outro = $p.outro; output = $p.output; steps = @($rows) })
    }

    "shot" {
        $p = Get-Proj $Project
        $s = Find-Step $p $Step
        $h = Call GET "/api/health"
        Join-Path (Join-Path (Join-Path $h.data_dir $p.id) "screenshots") $s.screenshot
    }

    "new" {
        Need $Name "Name"
        $p = Call POST "/api/projects" @{ name = $Name; language = $Language }
        if ($Voice) { $p = Call PATCH "/api/projects/$($p.id)" @{ voice = $Voice } }
        Show ([ordered]@{ id = $p.id; name = $p.name; language = $p.language; voice = $p.voice })
    }

    "import" {
        Need $File "File"
        $path = Full-Path $File
        if (-not (Test-Path -LiteralPath $path)) { Fail "file not found: $path" }
        if (@(".pptx", ".ppt", ".pdf", ".html", ".htm", ".zip") -notcontains [System.IO.Path]::GetExtension($path).ToLower()) {
            Fail "only .pptx / .ppt / .pdf and HTML decks (.html / .htm, or a .zip of the deck's folder) can be imported"
        }
        $j = Upload "/api/import/slides" $path
        $man = (Wait-StepCastJob $j.id "reading the deck" $true).result
        # notes as narration, slides without notes left empty, no voice-over: StepCast's AI is never asked
        $payload = @{ name = if ($Name) { $Name } else { [System.IO.Path]::GetFileNameWithoutExtension($path) }
                      notes_mode = "verbatim"; missing = "empty"; language = $Language; voice = $Voice
                      auto_voice = $false; auto_generate = $false }
        if ($Pages) { $payload.selected = @(Parse-Pages $Pages) }
        $c = Call POST "/api/import/slides/$($man.id)/create" $payload
        $res = (Wait-StepCastJob $c.id "creating the project" $true).result
        $summary = [ordered]@{ project = $res.project_id; slides = $res.slides; slides_with_notes = $man.with_notes
                               engine = $man.engine; warning = $res.warning }
        # an HTML deck: how StepCast turns its pages, and how many slides have stage directions (clicks) in their notes
        if ($man.web) { $summary.html = [ordered]@{ kind = $man.web.kind; turns_pages_by = $man.web.nav; slides_with_directions = $man.web.cues } }
        Show $summary
    }

    "web" {
        $p = Get-Proj $Project
        $s = Find-Step $p $Step
        if (-not $s.web) { Fail "step $Step is not a slide of an HTML deck: only those have controls and clicks" }
        $payload = $null
        if ($File) {
            $data = Read-Json (Read-Text $File) $File
            if ($data -and $data.PSObject.Properties["actions"]) { $acts = @($data.actions) }
            elseif ($data -is [array]) { $acts = @($data) }
            elseif ($data -and $data.PSObject.Properties["kind"]) { $acts = @($data) }
            else { Fail "$File needs {""actions"": [{""kind"": ""click"", ""control"": 0, ""unit"": 1}]} (an empty list removes every click)" }
            $payload = @{ actions = @($acts | Where-Object { $null -ne $_ }) }
        } elseif ($Auto) {
            $payload = @{ auto = $true }
        } elseif ($Live) {
            $payload = @{ live = (Bool $Live) }
        }
        if ($null -ne $payload) {
            $null = Call PUT "/api/projects/$Project/steps/$($s.id)/web" $payload
            $s = Find-Step (Get-Proj $Project) $s.id
        }
        Show (Web-Info $s)
    }

    "copy" {
        Need $Project "Project"; Need $From "From"; Need $Steps "Steps"
        $payload = @{ source = $From; steps = @($Steps -split "[,\s]+" | Where-Object { $_ }); crop = $Crop }
        if ($Position -ge 0) { $payload.position = $Position }
        Show (Call POST "/api/projects/$Project/steps/copy" $payload).added
    }

    "crop" {
        Need $Project "Project"
        $payload = @{ crop = $Crop }
        if ($Steps) { $payload.steps = @($Steps -split "[,\s]+" | Where-Object { $_ }) }
        Show (Call POST "/api/projects/$Project/steps/crop" $payload).cropped
    }

    "slide" {
        Need $Out "Out"; Need $Title "Title"
        $text = if ($File) { Read-Text $File } else { $Body -replace '`n', "`n" -replace '\\n', "`n" }
        Draw-Slide $Out $Title $text
    }

    "image" {
        Need $Project "Project"; Need $Image "Image"
        $path = Full-Path $Image
        if (-not (Test-Path -LiteralPath $path)) { Fail "file not found: $path" }
        $b64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($path))
        $narr = if ($File) { Read-Text $File } else { $Narration }
        $payload = @{ image = $b64; title = $Title; narration = $narr }
        if ($Position -ge 0) { $payload.position = $Position }
        Show (Call POST "/api/projects/$Project/steps/image" $payload).added
    }

    "step" {
        $p = Get-Proj $Project
        $s = Find-Step $p $Step
        $payload = @{}
        if ($PSBoundParameters.ContainsKey("Title")) { $payload.title = $Title }
        if ($PSBoundParameters.ContainsKey("Narration")) { $payload.narration = $Narration; if (-not $PSBoundParameters.ContainsKey("Caption")) { $payload.caption = $Narration } }
        if ($File) { $t = Read-Text $File; $payload.narration = $t; $payload.caption = $t }
        if ($PSBoundParameters.ContainsKey("Caption")) { $payload.caption = $Caption }
        if ($Include) { $payload.include = Bool $Include }
        if ($Zoom) { $payload.zoom = Bool $Zoom }
        if ($Highlight) { $payload.highlight = Bool $Highlight }
        if ($Rect) {
            $v = $Rect -split "," | ForEach-Object { [double]$_ }
            if ($v.Count -ne 4) { Fail "-Rect needs x,y,w,h (CSS pixels of the recorded page)" }
            $payload.target_rect = @{ x = $v[0]; y = $v[1]; w = $v[2]; h = $v[3] }
        }
        $r = Call PATCH "/api/projects/$Project/steps/$($s.id)" $payload
        Show ([ordered]@{ id = $r.id; index = $r.index; title = $r.title; narration = $r.narration; zoom = $r.zoom; highlight = $r.highlight })
    }

    "delete" {
        $p = Get-Proj $Project
        $s = Find-Step $p $Step
        Show (Call DELETE "/api/projects/$Project/steps/$($s.id)")
    }

    "set" {
        Need $Project "Project"
        $data = $null
        if ($File) { $data = Read-Json (Read-Text $File) $File }
        elseif ($Json) { $data = Read-Json $Json "-Json" }
        if ($null -ne $data -and $data -isnot [System.Management.Automation.PSCustomObject]) { Fail "the project JSON must be an object like {""intro"": ""..""}" }
        $extra = [ordered]@{}
        foreach ($k in @("Title", "Subtitle", "Intro", "Outro", "Language", "Voice")) {
            if ($PSBoundParameters.ContainsKey($k)) { $extra[$k.ToLower()] = $PSBoundParameters[$k] }
        }
        if ($null -eq $data -and $extra.Count -eq 0) { Fail "nothing to set: give -File project.json and/or -Title / -Subtitle / -Intro / -Outro / -Language / -Voice" }
        if ($null -eq $data) { $data = $extra }
        else { foreach ($k in $extra.Keys) { $data | Add-Member -NotePropertyName $k -NotePropertyValue $extra[$k] -Force } }
        $p = Call PATCH "/api/projects/$Project" $data
        Show ([ordered]@{ id = $p.id; title = $p.title; subtitle = $p.subtitle; intro = $p.intro; outro = $p.outro
                          language = $p.language; voice = $p.voice; settings = $p.settings })
    }

    "script-export" {
        Need $Project "Project"; Need $Out "Out"
        $s = Call GET "/api/projects/$Project/export/script"
        $text = if ($s -is [string]) { $s } else { $s | ConvertTo-Json -Depth 30 }
        Write-Text $Out $text
    }

    "script-import" {
        Need $Project "Project"; Need $File "File"
        $text = Read-Text $File
        $data = Read-Json $text $File
        # check every step item before writing anything: an item that matches no step (wrong id, or an index counted from 1) would be lost silently
        $p = Get-Proj $Project
        $ids = @{}; $indexes = @{}
        foreach ($s in $p.steps) { $ids[[string]$s.id] = $true; $indexes[[string]$s.index] = $true }
        $bad = @(foreach ($it in @($data.steps)) {
            if ($null -eq $it) { continue }
            if ($it.id) { if (-not $ids.ContainsKey([string]$it.id)) { "id $($it.id)" } }
            elseif ($null -ne $it.index) { if (-not $indexes.ContainsKey([string]$it.index)) { "index $($it.index)" } }
            else { "an item without id or index" }
        })
        if ($bad.Count) { Fail "nothing imported: no such step in $Project for $($bad -join ', '). Use the ids from script-export / project (indexes start at 0)." }
        $p = Call POST "/api/projects/$Project/import/script" $text
        Show ([ordered]@{ id = $p.id; title = $p.title; steps = $p.steps.Count; steps_in_file = @($data.steps).Count })
    }

    "voice" {
        Need $Project "Project"
        $j = Call POST "/api/projects/$Project/tts" @{ voice = $Voice; only_missing = (-not $All) }
        $res = (Wait-StepCastJob $j.id "voice-over").result
        if (-not $NoWait -and $res.fallback) {
            Log "the online voice failed for $($res.fallback) paragraph(s), read by Windows' own voice instead: $($res.fallback_reason) (proxy: $(if ($res.proxy) { $res.proxy } else { 'none' })). See SKILL.md, Troubleshooting"
        }
        Show $res
    }

    "render" {
        Need $Project "Project"
        $payload = @{ width = $Width; height = $Height; burn_subtitles = $true }
        if ($NoSidecar) { $payload.sidecar_subtitles = $false }
        if ($NoSubSpace) { $payload.second_sub_space = $false }
        if ($NoIntro) { $payload.intro_enabled = $false }
        if ($NoOutro) { $payload.outro_enabled = $false }
        $j = Call POST "/api/projects/$Project/render" $payload
        $done = Wait-StepCastJob $j.id "render"
        if ($NoWait) { Show $done; break }
        $h = Call GET "/api/health"
        Show ([ordered]@{ video = Join-Path (Join-Path (Join-Path $h.data_dir $Project) "output") $done.result.file
                          seconds = $done.result.duration; warning = $done.result.warning })
    }

    "sentences" {
        Need $Project "Project"; Need $Out "Out"
        $s = Call GET "/api/projects/$Project/subtitles2/sentences"
        Write-Text $Out ($s | ConvertTo-Json -Depth 30)
    }

    "subs-import" {
        Need $Project "Project"; Need $Lang "Lang"; Need $File "File"
        $data = Read-Json (Read-Text $File) $File
        # the key from `sentences` is required: it tells whether the video was rendered again since then
        if ($data -is [array] -or -not $data.key) { Fail "$File needs {""key"": ""<key from the sentences file>"", ""texts"": [...]}" }
        $payload = @{ texts = @($data.texts); key = $data.key }
        $st = Call POST "/api/projects/$Project/subtitles2/$Lang/import" $payload
        Show ($st.tracks | Select-Object lang, name, vtt, srt, stale)
    }

    "job" { Need $Id "Id"; Show (Call GET "/api/jobs/$Id") }

    default { Fail "unknown command: $Command (run: stepcast.ps1 help)" }
}
