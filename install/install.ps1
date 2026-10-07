<#
.SYNOPSIS
  Job Aggregator - one-command installer for Windows 10/11 (re-runnable).

.DESCRIPTION
  Installs Python 3.12 (winget), the project venv + requirements, your personal pack
  (config.local.yaml + optional jobs.db; resumes/ ships with the repo), Ollama + llama3.2:3b (free,
  local, no API keys), two Task Scheduler tasks (UI at logon, kept alive; job check on
  weekdays at 7:19, 11:19, 16:19 and at logon, missed runs catch up), and a Desktop
  shortcut to http://localhost:8765. It never sends email.

  Easiest: double-click install\install.bat
  Or in PowerShell:  powershell -ExecutionPolicy Bypass -File install\install.ps1

.EXAMPLE
  install.ps1                                   # full install
  install.ps1 -Pack C:\Users\me\Downloads\personal-pack.zip
  install.ps1 -Uninstall                        # remove tasks + shortcut, keep data
  install.ps1 -NoSchedule -NoOllama -DryRun     # just show what would happen
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive installer output')]
[CmdletBinding()]
param(
    [string]$Pack = "",
    [int]$Port = 0,
    [string]$Model = "",
    [switch]$NoSchedule,
    [switch]$NoOllama,
    [switch]$NoModel,
    [switch]$DryRun,
    [switch]$Uninstall,
    [switch]$Yes,
    [string]$UnitsDir = "",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest = @()
)

# Continue (not Stop): Windows PowerShell 5.1 turns harmless stderr output of native tools into
# terminating errors under "Stop". Failures are checked explicitly via $LASTEXITCODE / Fail.
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest fast on Windows PowerShell 5.1

# Accept unix-style flags too (install.bat --uninstall, --no-schedule, a bare pack path, ...)
foreach ($r in $Rest) {
    switch -Regex ($r) {
        '^--?uninstall$'   { $Uninstall = $true; continue }
        '^--?no-?schedule$' { $NoSchedule = $true; continue }
        '^--?no-?ollama$'  { $NoOllama = $true; continue }
        '^--?no-?model$'   { $NoModel = $true; continue }
        '^--?dry-?run$'    { $DryRun = $true; continue }
        '^(-y|--yes)$'     { $Yes = $true; continue }
        '\.zip$'           { $Pack = $r; continue }
        default            { Write-Warning "Ignoring unknown argument: $r" }
    }
}

$OnWindows = ($env:OS -eq "Windows_NT")
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root
$TaskUI = "JobAggregator-UI"
$TaskAuto = "JobAggregator-Auto"
$Runner = Join-Path $Root "install\run_auto.py"
if (-not $OnWindows) { $Runner = Join-Path $Root "install/run_auto.py" }
$ModelSmall = "llama3.2:1b"
$PyWinUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
$OllamaSetupUrl = "https://ollama.com/download/OllamaSetup.exe"

function Say([string]$m)  { Write-Host $m }
function Step([string]$m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "WARNING: $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }
function Test-Cmd([string]$n) { return [bool](Get-Command $n -ErrorAction SilentlyContinue) }
function Read-YesNo([string]$q) {
    if ($Yes) { return $true }
    if (-not [Environment]::UserInteractive) { return $false }
    try { $a = Read-Host "$q [y/N]" } catch { return $false }
    return ($a -match '^(y|yes)$')
}
function Invoke-Step([string]$desc, [scriptblock]$block) {
    if ($DryRun) { Say "  [dry-run] $desc"; return }
    & $block
}
function Sync-PathFromRegistry {
    if (-not $OnWindows) { return }
    $m = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $u = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$m;$u"
}

if ($OnWindows) {
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { Write-Verbose "TLS 1.2 already default" }
    # Files from "Download ZIP" carry a mark-of-the-web; clear it so scripts run quietly.
    if (-not $DryRun) { Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue }
}

$VenvPy = if ($OnWindows) { Join-Path $Root ".venv\Scripts\python.exe" } else { Join-Path $Root ".venv/bin/python" }
$VenvPyw = if ($OnWindows) { Join-Path $Root ".venv\Scripts\pythonw.exe" } else { $VenvPy }

# ============================================================================ uninstall
function Uninstall-JobAggregator {
    Step "Removing scheduled tasks and shortcut (your data, settings, venv and Ollama are kept)"
    foreach ($t in @($TaskUI, $TaskAuto)) {
        if ($DryRun -or -not $OnWindows) { Say "  [dry-run] Unregister-ScheduledTask $t"; continue }
        if (Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue) {
            Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $t -Confirm:$false
            Say "  removed task $t"
        }
    }
    if ($OnWindows -and -not $DryRun) {
        $pat = "*" + $Root + "*run_auto.py*serve*"
        $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue)
        $wrappers = @($procs | Where-Object { $_.CommandLine -like $pat })
        foreach ($w in $wrappers) {
            # the wrapper's child is the actual web server (python -m aggregator serve)
            $procs | Where-Object { $_.ParentProcessId -eq $w.ProcessId } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $w.ProcessId -Force -ErrorAction SilentlyContinue
        }
        if ($wrappers.Count -gt 0) { Say "  stopped the UI" }
        $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Job Aggregator.url"
        if (Test-Path $lnk) { Remove-Item $lnk -Force; Say "  removed $lnk" }
    }
    Say "Done. Data kept in $Root (data\, logs\, config.local.yaml, resumes\)."
}
if ($Uninstall) { Uninstall-JobAggregator; exit 0 }

# ============================================================================ Python 3.10+
function Find-Python {
    # returns the full path of a Python >= 3.10 (prefers 3.12/3.13), or $null
    $probe = "import sys; print(sys.executable); raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)"
    $cands = @()
    if (Test-Cmd "py") { foreach ($v in @("-3.12", "-3.13", "-3.11", "-3.10", "-3")) { $cands += , @("py", $v) } }
    foreach ($n in @("python", "python3")) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike "*WindowsApps*") { $cands += , @($c.Source) }
    }
    if ($OnWindows) {
        foreach ($v in @("312", "313", "311", "310")) {
            $p = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
            if (Test-Path $p) { $cands += , @($p) }
        }
    }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count - 1)] }
            $out = & $exe @pre -c $probe 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -First 1).Trim() }
        } catch { Write-Verbose "probe failed: $($c -join ' ')" }
    }
    return $null
}

Step "Python 3.10+"
$Py = Find-Python
if (-not $Py) {
    if (-not $OnWindows) { Fail "Python 3.10+ not found (non-Windows: use install/install.sh)." }
    if (Test-Cmd "winget") {
        Say "  installing Python 3.12 with winget..."
        Invoke-Step "winget install Python.Python.3.12" {
            winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
        }
    } else {
        Say "  winget not available; downloading the official python.org installer..."
        Invoke-Step "download + run $PyWinUrl" {
            $exe = Join-Path $env:TEMP "python-3.12-installer.exe"
            Invoke-WebRequest -Uri $PyWinUrl -OutFile $exe -UseBasicParsing -ErrorAction Stop
            Start-Process -FilePath $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
        }
    }
    Sync-PathFromRegistry
    $Py = Find-Python
    if (-not $Py -and -not $DryRun) { Fail "Python still not found. Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH') and run this again." }
}
if (-not $Py) { $Py = "python" }
Say "  using $Py"
if (Test-Cmd "git") { Say "  git: found" } else { Say "  git: not installed (optional)" }

# ============================================================================ venv + requirements
Step "Virtual environment + Python packages"
Invoke-Step "$Py -m venv .venv; pip install -r requirements.txt" {
    if (Test-Path $VenvPy) {
        & $VenvPy -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -ne 0) { Warn "existing .venv is broken (copied from another computer?); recreating"; Remove-Item -Recurse -Force (Join-Path $Root ".venv") }
    }
    if (-not (Test-Path $VenvPy)) {
        & $Py -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
    }
    & $VenvPy -m pip install --disable-pip-version-check -q --upgrade pip
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed (check your internet connection and run this again)" }
    Say "  ready: $(Join-Path $Root '.venv')"
}
New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs"), (Join-Path $Root "data") | Out-Null

# ============================================================================ personal pack
Step "Personal pack (your private settings + saved jobs)"
$importer = Join-Path $Root "install/import_personal_pack.py"
if ($DryRun) {
    Say "  [dry-run] $VenvPy $importer $Pack"
} elseif ($Pack) {
    if (-not (Test-Path $Pack)) { Fail "personal pack not found: $Pack" }
    & $VenvPy $importer $Pack
} else {
    & $VenvPy $importer --find *> $null
    if ($LASTEXITCODE -eq 0) { & $VenvPy $importer } else { Say "  no personal-pack.zip found next to the repo or in Downloads" }
}
$LocalCfg = Join-Path $Root "config.local.yaml"
if (-not (Test-Path $LocalCfg) -and -not $DryRun) {
    Copy-Item (Join-Path $Root "config.local.example.yaml") $LocalCfg
    Warn "Created config.local.yaml from the example: open it in Notepad and fill in your name, phone and email."
}
if (-not (Test-Path (Join-Path $Root "resumes/profile_text.txt")) -and -not $DryRun) {
    Warn "No resumes\profile_text.txt: add your resume as plain text there so jobs get match scores."
}

function Get-Cfg([string]$expr, [string]$default) {
    if (-not (Test-Path $VenvPy)) { return $default }
    $v = & $VenvPy -c "from aggregator.config import load_config; cfg = load_config(); print($expr)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { return ($v | Select-Object -First 1).Trim() }
    return $default
}
if ($Port -le 0) { $Port = [int](Get-Cfg 'cfg["server"]["port"]' "8765") }
$CfgModel = Get-Cfg 'cfg["llm"]["model"]' "llama3.2:3b"
$OllamaUrl = Get-Cfg 'cfg["llm"]["ollama_url"].rstrip("/")' "http://localhost:11434"
$Url = "http://localhost:$Port"

# ============================================================================ Ollama
function Test-Ollama { try { Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 3 | Out-Null; return $true } catch { return $false } }
function Find-Ollama {
    $c = Get-Command "ollama" -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    if ($OnWindows) {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
        if (Test-Path $p) { return $p }
    }
    return $null
}
function Get-RamGB {
    try {
        if ($OnWindows) { return [int]([math]::Floor((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)) }
        $kb = (Get-Content /proc/meminfo | Select-String '^MemTotal:\s+(\d+)').Matches[0].Groups[1].Value
        return [int]([math]::Floor([double]$kb / 1MB))
    } catch { return 0 }
}

if (-not $NoOllama) {
    Step "Ollama (local AI for drafts; free and open source, no API keys)"
    $Ollama = Find-Ollama
    if ($Ollama) {
        Say "  already installed: $Ollama"
    } elseif (-not $OnWindows) {
        Warn "Ollama not found (non-Windows test run); skipping install"
    } else {
        if (Test-Cmd "winget") {
            Invoke-Step "winget install Ollama.Ollama" {
                winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements
            }
        }
        Sync-PathFromRegistry
        if (-not (Find-Ollama)) {
            Invoke-Step "download + run $OllamaSetupUrl" {
                $exe = Join-Path $env:TEMP "OllamaSetup.exe"
                Invoke-WebRequest -Uri $OllamaSetupUrl -OutFile $exe -UseBasicParsing -ErrorAction Stop
                Start-Process -FilePath $exe -ArgumentList "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES" -Wait
            }
            Sync-PathFromRegistry
        }
        $Ollama = Find-Ollama
    }
    if (-not $DryRun) {
        if (Test-Ollama) { Say "  running at $OllamaUrl" }
        elseif ($Ollama) {
            # The Ollama app starts itself at every login; start it now too.
            & $VenvPy -c "import sys; sys.path.insert(0, 'install'); import run_auto; print('  ollama:', run_auto.ensure_ollama(run_auto._cfg()))"
        }
    }
    if (-not $NoModel) {
        $gb = Get-RamGB
        if (-not $Model) {
            $Model = $CfgModel
            if ($gb -gt 0 -and $gb -lt 8 -and $Model -ne $ModelSmall) {
                Warn "This PC has about $gb GB of RAM. $Model works best with 8 GB+; $ModelSmall is lighter (shorter, plainer drafts)."
                if (Read-YesNo "Use the smaller $ModelSmall instead?") { $Model = $ModelSmall }
            }
        }
        Say "  RAM: $gb GB; model: $Model"
        if ($DryRun -or -not $Ollama) {
            Say "  [dry-run] ollama pull $Model"
        } else {
            $have = (& $Ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] })
            if ($have -contains $Model -or $have -contains "$Model`:latest") { Say "  $Model already downloaded" }
            else {
                Say "  downloading $Model (about 1-2 GB, one time)..."
                & $Ollama pull $Model
                if ($LASTEXITCODE -ne 0) { Warn "Could not download $Model now; drafts use templates until you run: ollama pull $Model" }
            }
        }
        if ($Model -ne $CfgModel -and -not $DryRun -and (Test-Path $LocalCfg)) {
            if (-not (Select-String -Path $LocalCfg -Pattern '^llm:' -Quiet)) {
                Add-Content -Path $LocalCfg -Encoding ASCII -Value "`r`n# set by install.ps1`r`nllm:`r`n  model: `"$Model`""
                Say "  config.local.yaml: llm.model = $Model"
            } else { Warn "config.local.yaml already has an llm: section; set  model: `"$Model`"  in it yourself." }
        }
    }
} else {
    Step "Skipping Ollama (-NoOllama): drafts will use the built-in templates"
}

# ============================================================================ Task Scheduler XML
function Esc([string]$s) { return [System.Security.SecurityElement]::Escape($s) }
$UserId = if ($OnWindows) { [System.Security.Principal.WindowsIdentity]::GetCurrent().Name } else { "$env:USER" }

function Get-TaskXml([string]$kind) {
    $u = Esc $UserId
    $cmd = Esc $VenvPyw
    $wd = Esc $Root
    if ($kind -eq "ui") {
        $desc = "Job Aggregator web UI on $Url (starts at logon, restarts if it stops)."
        $runArgs = Esc ('-X utf8 "' + $Runner + '" serve --port ' + $Port + ' --supervise')
        $triggers = "    <LogonTrigger><Enabled>true</Enabled><UserId>$u</UserId><Delay>PT30S</Delay></LogonTrigger>"
        $limit = "PT0S"
        $restart = "<RestartOnFailure><Interval>PT1M</Interval><Count>255</Count></RestartOnFailure>"
    } else {
        $desc = "Job Aggregator check: weekdays 7:19, 11:19, 16:19 and at logon; missed runs catch up. Writes follow-up DRAFTS only; never sends email."
        $runArgs = Esc ('-X utf8 "' + $Runner + '" auto --if-missed --ensure-ui --port ' + $Port)
        $t = @("    <LogonTrigger><Enabled>true</Enabled><UserId>$u</UserId><Delay>PT2M</Delay></LogonTrigger>")
        foreach ($hm in @("07:19", "11:19", "16:19")) {
            $t += "    <CalendarTrigger><Enabled>true</Enabled><StartBoundary>2026-01-05T$($hm):00</StartBoundary><ScheduleByWeek><DaysOfWeek><Monday /><Tuesday /><Wednesday /><Thursday /><Friday /></DaysOfWeek><WeeksInterval>1</WeeksInterval></ScheduleByWeek></CalendarTrigger>"
        }
        $triggers = $t -join "`r`n"
        $limit = "PT2H"
        $restart = "<RestartOnFailure><Interval>PT10M</Interval><Count>2</Count></RestartOnFailure>"
    }
    return @"
<?xml version="1.0"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>$u</Author>
    <Description>$(Esc $desc)</Description>
  </RegistrationInfo>
  <Triggers>
$triggers
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$u</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>$limit</ExecutionTimeLimit>
    <Priority>7</Priority>
    $restart
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$cmd</Command>
      <Arguments>$runArgs</Arguments>
      <WorkingDirectory>$wd</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

$xmlUI = Get-TaskXml "ui"
$xmlAuto = Get-TaskXml "auto"
if ($UnitsDir -or $DryRun) {
    if (-not $UnitsDir) { $UnitsDir = Join-Path ([IO.Path]::GetTempPath()) ("jobagg-tasks-" + [guid]::NewGuid().ToString("N").Substring(0, 8)) }
    New-Item -ItemType Directory -Force -Path $UnitsDir | Out-Null
    Set-Content -Path (Join-Path $UnitsDir "$TaskUI.xml") -Value $xmlUI -Encoding Unicode
    Set-Content -Path (Join-Path $UnitsDir "$TaskAuto.xml") -Value $xmlAuto -Encoding Unicode
    Say "  generated task XML in $UnitsDir"
}

function Register-JobAggTask([string]$name, [string]$xml) {
    try {
        Register-ScheduledTask -TaskName $name -Xml $xml -Force -ErrorAction Stop | Out-Null
    } catch {
        # Fallback: schtasks wants a UTF-16 file
        $f = Join-Path $env:TEMP "$name.xml"
        Set-Content -Path $f -Value $xml -Encoding Unicode
        schtasks /Create /TN $name /XML $f /F | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "could not register task $name : $($_.Exception.Message)" }
    }
    Say "  task registered: $name"
}

if (-not $NoSchedule) {
    Step "Schedule: weekdays 7:19 / 11:19 / 16:19 + at logon (missed runs catch up); UI starts at logon"
    if ($DryRun -or -not $OnWindows) {
        Say "  [dry-run] Register-ScheduledTask $TaskUI and $TaskAuto (see XML above)"
    } else {
        Register-JobAggTask $TaskUI $xmlUI
        Register-JobAggTask $TaskAuto $xmlAuto
        Start-ScheduledTask -TaskName $TaskUI -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskAuto -ErrorAction SilentlyContinue   # runs now only if a weekday slot was missed (first install: yes)
        Say "  started the UI; the first job check runs in the background now (5-15 minutes)"
    }
} else {
    Step "Skipping schedule (-NoSchedule). Start the UI yourself: $VenvPy $Runner serve"
}

# ============================================================================ desktop shortcut
Step "Desktop shortcut"
if ($DryRun -or -not $OnWindows) {
    Say "  [dry-run] Desktop\Job Aggregator.url -> $Url"
} else {
    $desk = [Environment]::GetFolderPath("Desktop")
    $lnk = Join-Path $desk "Job Aggregator.url"
    Set-Content -Path $lnk -Encoding ASCII -Value "[InternetShortcut]`r`nURL=$Url/`r`n"
    Say "  $lnk -> $Url"
}

Say ""
Say "=================================================================="
Say " Job Aggregator is set up."
Say "   Open:      $Url   (or the 'Job Aggregator' icon on your Desktop)"
if (-not $NoSchedule) { Say "   Automatic: weekdays 7:19, 11:19, 16:19 + at logon (catch-up)." }
Say "   Results:   $Root\logs\latest-digest.md (and the UI)"
Say "   Email:     NOTHING is ever sent automatically - drafts only."
Say "   Remove the scheduled tasks later:  install\install.bat -Uninstall"
Say "=================================================================="
