Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function New-TextFromCodePoints {
    param([int[]]$CodePoints)

    return [string]::Concat(($CodePoints | ForEach-Object { [char]$_ }))
}

function Get-CommandLine {
    param($Process)

    if ($null -eq $Process.CommandLine) {
        return ''
    }

    return [string]$Process.CommandLine
}

$launcherName = (New-TextFromCodePoints @(0x4E2D, 0x570B, 0x91AB, 0x76AE, 0x819A, 0x79D1, 0x6253, 0x5361, 0x7A0B, 0x5F0F)) + '.pyw'
$patterns = @(
    $launcherName,
    'src/autoclock.py',
    'src\autoclock.py'
)

$processes = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
    Where-Object {
        $cmd = Get-CommandLine $_
        foreach ($pattern in $patterns) {
            if ($cmd.IndexOf($pattern, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                return $true
            }
        }
        return $false
    }

$rows = @($processes | Sort-Object CreationDate)

Write-Host 'Autoclock duplicate cleanup'
Write-Host '==========================='

if ($rows.Count -eq 0) {
    Write-Host 'No running autoclock Python process was found.'
    exit 0
}

$view = $rows | Select-Object `
    @{Name='PID'; Expression={$_.ProcessId}},
    @{Name='Started'; Expression={
        if ($_.CreationDate) {
            [Management.ManagementDateTimeConverter]::ToDateTime($_.CreationDate).ToString('yyyy-MM-dd HH:mm:ss')
        } else {
            ''
        }
    }},
    @{Name='Name'; Expression={$_.Name}},
    @{Name='CommandLine'; Expression={(Get-CommandLine $_)}}

$view | Format-Table -AutoSize -Wrap

if ($rows.Count -le 1) {
    Write-Host 'Only one autoclock process is running. No cleanup is needed.'
    exit 0
}

$keep = $rows[-1]
$targets = @($rows | Where-Object { $_.ProcessId -ne $keep.ProcessId })

Write-Host ''
Write-Host ("Keeping newest process PID {0} and stopping {1} older process(es)." -f $keep.ProcessId, $targets.Count)
Write-Host 'Type CLEAN to continue, or press Enter to cancel.'
$confirm = Read-Host 'Confirm'

if ($confirm -ne 'CLEAN') {
    Write-Host 'Canceled. No process was stopped.'
    exit 0
}

$failures = 0
foreach ($target in $targets) {
    try {
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        Write-Host ("Stopped PID {0}" -f $target.ProcessId)
    } catch {
        $failures += 1
        Write-Warning ("Failed to stop PID {0}: {1}" -f $target.ProcessId, $_.Exception.Message)
    }
}

Write-Host ''
Write-Host 'Done. If old tray icons remain visible, they are usually stale Windows tray entries; hover over them or sign out to refresh.'

if ($failures -gt 0) {
    exit 1
}

exit 0
