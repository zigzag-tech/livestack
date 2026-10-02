<#
.SYNOPSIS
  Install (or re-point) a Livestack workload worker as a Windows service.

.DESCRIPTION
  Idempotent. Run as Administrator. What it sets up (design:
  openspec/changes/windows-host-worker; runbook: node-py/docs/windows-worker.md):

    - a local account without administrator rights that runs the worker
      (password: random, rotated on every run, held only by the SCM);
    - "Log on as a service" for that account;
    - a bounded workspace: a fixed-maximum VHDX, NTFS, mounted at a folder,
      re-attached at boot by a SYSTEM scheduled task;
    - state and config directories only SYSTEM, Administrators and the account
      can read (the config holds the worker token);
    - the service: base CPython, PYTHONPATH naming the immutable release in the
      service's registry Environment, restart-on-failure actions;
    - LongPathsEnabled (handler trees exceed MAX_PATH).

  It never starts the service: start it after reading the config once more.

.EXAMPLE
  .\install-worker.ps1 -WorkerId xc-win-1-native -Release C:\harmony\releases\livestack-dba3fc37\node-py `
      -Config C:\harmony\staging\xc-win-1-native.json -WorkspaceGB 100
#>
param(
  [Parameter(Mandatory)][string]$WorkerId,
  [Parameter(Mandatory)][string]$Release,      # ...\node-py of an immutable release
  [Parameter(Mandatory)][string]$Config,       # worker config to install (holds the token)
  [int]$WorkspaceGB = 100,
  [string]$Root = 'C:\harmony',
  [string]$Account = 'harmony-worker',
  [string]$Python = 'C:\Program Files\Python312\python.exe',
  [string]$ServiceName = 'LivestackWorkloadWorker'
)
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Output "install-worker: $m" }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'run as Administrator' }
foreach ($p in @($Python, $Config, (Join-Path $Release 'livestack_node\workloads\windows_service.py'))) {
  if (-not (Test-Path $p)) { throw "missing: $p" }
}

# --- long paths -------------------------------------------------------------
Set-ItemProperty HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem LongPathsEnabled 1 -Type DWord

# --- account ----------------------------------------------------------------
Add-Type -AssemblyName System.Web
$password = [System.Web.Security.Membership]::GeneratePassword(32, 6)
$secure = ConvertTo-SecureString $password -AsPlainText -Force
if (Get-LocalUser -Name $Account -ErrorAction SilentlyContinue) {
  Set-LocalUser -Name $Account -Password $secure -PasswordNeverExpires $true
  Step "account $Account exists; password rotated"
} else {
  New-LocalUser -Name $Account -Password $secure -PasswordNeverExpires -UserMayNotChangePassword `
    -Description 'Livestack workload worker (service account)' | Out-Null
  Step "account $Account created"
}
$sid = (Get-LocalUser -Name $Account).SID.Value

# --- user rights: Log on as a service; create symbolic links ----------------
# Symlinks: handlers restore the links a captured source tree carries
# (Benchday's restoreSourceLinks); without the right they fail EPERM.
$work = Join-Path $env:TEMP ('secpol-' + [guid]::NewGuid())
New-Item -ItemType Directory $work | Out-Null
try {
  foreach ($right in @('SeServiceLogonRight', 'SeCreateSymbolicLinkPrivilege')) {
    secedit /export /cfg "$work\cur.inf" /areas USER_RIGHTS | Out-Null
    $line = (Get-Content "$work\cur.inf" | Where-Object { $_ -like "$right*" })
    # secedit names a resolvable account by NAME, otherwise by *SID.
    $held = ($line -split '=', 2)[-1].Split(',') | ForEach-Object { $_.Trim() }
    if (-not ($held -contains "*$sid" -or $held -contains $Account)) {
      $value = if ($line) { ($line -split '=', 2)[1].Trim() + ",*$sid" } else { "*$sid" }
      @('[Unicode]', 'Unicode=yes', '[Version]', 'signature="$CHICAGO$"', 'Revision=1',
        '[Privilege Rights]', "$right = $value") | Set-Content "$work\new.inf" -Encoding Unicode
      secedit /configure /db "$work\new.sdb" /cfg "$work\new.inf" /areas USER_RIGHTS | Out-Null
      Remove-Item "$work\new.sdb" -ErrorAction SilentlyContinue
      Step "granted $right"
    }
  }
} finally { Remove-Item -Recurse -Force $work }

# --- directories and ACLs ---------------------------------------------------
function Lock($path, $rights) {
  # SYSTEM and Administrators full; the account gets $rights; nothing inherited.
  # Inheritance flags only on directories: on a file they leave it with NO ACE.
  $i = if (Test-Path $path -PathType Container) { '(OI)(CI)' } else { '' }
  icacls $path /inheritance:r /grant:r "SYSTEM:${i}F" "Administrators:${i}F" "*${sid}:${i}$rights" | Out-Null
  if ($LASTEXITCODE) { throw "icacls $path failed" }
}
$state = Join-Path $Root "state\$WorkerId"
$configDir = Join-Path $Root 'config'
$volumes = Join-Path $Root 'volumes'
$mount = Join-Path $Root "work\$WorkerId"
foreach ($d in @($state, $configDir, $volumes, $mount)) { New-Item -ItemType Directory -Force $d | Out-Null }
Lock $state 'M'
Lock $configDir 'RX'
Lock $volumes 'RX'
$installed = Join-Path $configDir "$WorkerId.json"
Copy-Item $Config $installed -Force
Lock $installed 'R'
# Releases: administrators write, the account reads.
icacls (Split-Path $Release -Parent) /grant "*${sid}:(OI)(CI)RX" | Out-Null

# --- bounded workspace: VHDX mounted at a folder ---------------------------
$vhd = Join-Path $volumes "$WorkerId.vhdx"
if (-not (Test-Path $vhd)) {
  $size = $WorkspaceGB * 1024
  @("create vdisk file=`"$vhd`" maximum=$size type=expandable", "select vdisk file=`"$vhd`"", 'attach vdisk',
    'create partition primary', "format fs=ntfs quick label=HW-$($WorkerId.Substring(0, [Math]::Min(8, $WorkerId.Length)))",
    "assign mount=`"$mount`"") | Set-Content "$volumes\create.txt" -Encoding ASCII
  diskpart /s "$volumes\create.txt" | Out-Null
  Remove-Item "$volumes\create.txt"
  if ($LASTEXITCODE) { throw 'diskpart failed creating the workspace volume' }
  Step "workspace volume $vhd ($WorkspaceGB GB) mounted at $mount"
} elseif (-not (Get-DiskImage -ImagePath $vhd).Attached) {
  Mount-DiskImage -ImagePath $vhd -NoDriveLetter | Out-Null
  Step "workspace volume re-attached"
}
Lock $mount 'M'
# Re-attach at boot: diskpart attachments do not survive a reboot; the folder
# mount point does (the mount manager remembers the volume).
$attach = "Mount-DiskImage -ImagePath '$vhd' -NoDriveLetter -ErrorAction SilentlyContinue"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -Command `"$attach`""
Register-ScheduledTask -TaskName "LivestackWorkspace-$WorkerId" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
  -User SYSTEM -RunLevel Highest -Force | Out-Null

# --- service ----------------------------------------------------------------
$bin = "`"$Python`" -m livestack_node.workloads.windows_service --config `"$installed`" --service-name $ServiceName"
# New-Service, not `sc.exe create`: Windows PowerShell 5.1 does not escape the
# quotes inside a native argument, so sc.exe never sees a quoted binPath.
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
  if ($existing.Status -ne 'Stopped') { throw "stop $ServiceName first (sc.exe stop $ServiceName); it is $($existing.Status)" }
  sc.exe delete $ServiceName | Out-Null
  for ($i = 0; $i -lt 50 -and (Get-Service $ServiceName -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 200 }
}
New-Service -Name $ServiceName -BinaryPathName $bin -DisplayName "Livestack workload worker ($WorkerId)" `
  -StartupType Automatic -Credential (New-Object PSCredential ".\$Account", $secure) | Out-Null
sc.exe config $ServiceName start= delayed-auto | Out-Null
if ($LASTEXITCODE) { throw 'sc.exe config failed' }
Step "service $ServiceName installed"
$password = $null
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName" Environment `
  -Type MultiString -Value @("PYTHONPATH=$Release", 'PYTHONDONTWRITEBYTECODE=1')
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null
Step "ready: sc.exe start $ServiceName   (log: $state\worker.log)"
