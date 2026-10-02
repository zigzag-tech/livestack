<#
.SYNOPSIS
  Install (or re-point) the compilation launch verifier for one Windows worker slot.

.DESCRIPTION
  Idempotent. Run as Administrator. The verifier is a LocalSystem service on a
  named pipe (design: openspec/changes/windows-host-worker, "Verifier"):

    - C:\ProgramData\livestack, owned by Administrators, writable only by
      SYSTEM and Administrators (the client's trust walk refuses anything else);
    - <dir>\<worker>-verifier.json: the verifier config, readable only by SYSTEM
      and Administrators (it holds the worker's authority token);
    - <dir>\compilation-launch.json: the registry {"version":1,"slots":{worker: pipe}},
      readable by everyone, merged per slot;
    - the service, LocalSystem, PYTHONPATH naming the immutable release,
      restart-on-failure actions. It is started.

  -Config is a staged JSON with: version, worker, worker_sid (or omit and pass
  -Account), host, authority, token, journal. machine_id and pipe are filled in.
#>
param(
  [Parameter(Mandatory)][string]$Release,      # ...\node-py of an immutable release
  [Parameter(Mandatory)][string]$Config,
  [string]$Account = 'harmony-worker',
  [string]$Python = 'C:\Program Files\Python312\python.exe',
  [string]$ServiceName = 'LivestackCompilationVerifier'
)
$ErrorActionPreference = 'Stop'
function Step($m) { Write-Output "install-verifier: $m" }

$dir = Join-Path $env:ProgramData 'livestack'
New-Item -ItemType Directory -Force $dir | Out-Null
icacls $dir /setowner Administrators | Out-Null
icacls $dir /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' 'Users:(OI)(CI)RX' | Out-Null
if ($LASTEXITCODE) { throw "icacls $dir failed" }

$value = Get-Content $Config -Raw | ConvertFrom-Json
$worker = $value.worker
if (-not $value.worker_sid) {
  $value | Add-Member -NotePropertyName worker_sid -NotePropertyValue (Get-LocalUser -Name $Account).SID.Value
}
$guid = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
$value | Add-Member -Force -NotePropertyName machine_id -NotePropertyValue ($guid -replace '-', '').ToLower()
$pipe = "\\.\pipe\livestack-compilation-$worker"
$value | Add-Member -Force -NotePropertyName pipe -NotePropertyValue $pipe
$installed = Join-Path $dir "$worker-verifier.json"
[IO.File]::WriteAllText($installed, ($value | ConvertTo-Json -Compress))
icacls $installed /inheritance:r /grant:r 'SYSTEM:F' 'Administrators:F' | Out-Null
if ($LASTEXITCODE) { throw "icacls $installed failed" }

$registry = Join-Path $dir 'compilation-launch.json'
$slots = @{}
if (Test-Path $registry) {
  (Get-Content $registry -Raw | ConvertFrom-Json).slots.PSObject.Properties | ForEach-Object { $slots[$_.Name] = $_.Value }
}
$slots[$worker] = $pipe
[IO.File]::WriteAllText($registry, (@{version = 1; slots = $slots} | ConvertTo-Json -Compress))
Step "registry slot $worker -> $pipe"

$bin = "`"$Python`" -m livestack_node.workloads.launch_verifier --config `"$installed`" --service-name $ServiceName"
$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
  if ($existing.Status -ne 'Stopped') { sc.exe stop $ServiceName | Out-Null; (Get-Service $ServiceName).WaitForStatus('Stopped', '00:00:30') }
  sc.exe delete $ServiceName | Out-Null
  for ($i = 0; $i -lt 50 -and (Get-Service $ServiceName -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 200 }
}
# New-Service: Windows PowerShell 5.1 does not escape quotes inside native arguments.
New-Service -Name $ServiceName -BinaryPathName $bin -DisplayName "Livestack compilation verifier ($worker)" `
  -StartupType Automatic | Out-Null
Set-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName" Environment `
  -Type MultiString -Value @("PYTHONPATH=$Release", 'PYTHONDONTWRITEBYTECODE=1')
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/30000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null
sc.exe start $ServiceName | Out-Null
Step "service $ServiceName started (log: $dir\verifier.log)"
