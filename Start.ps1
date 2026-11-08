$ErrorActionPreference = 'Stop'
$selectedPython = $null
$versionProbe = 'import sys, tkinter; assert sys.version_info >= (3, 10); print(sys.executable)'
foreach ($commandName in @('py', 'python', 'python3')) {
    $pythonCommand = Get-Command $commandName -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { continue }
    $probeArguments = if ($commandName -eq 'py') { @('-3', '-c', $versionProbe) } else { @('-c', $versionProbe) }
    try {
        $probeOutput = & $pythonCommand.Source @probeArguments 2>$null
        if ($LASTEXITCODE -eq 0 -and $probeOutput) {
            $selectedPython = [string]($probeOutput | Select-Object -Last 1)
            break
        }
    } catch { continue }
}
if (-not $selectedPython) {
    $pythonDirectory = Join-Path $env:LOCALAPPDATA 'Programs/Python'
    $installedPythons = if (Test-Path -LiteralPath $pythonDirectory) {
        Get-ChildItem -LiteralPath $pythonDirectory -Directory | Sort-Object Name -Descending
    } else { @() }
    foreach ($installation in $installedPythons) {
        $candidate = Join-Path $installation.FullName 'python.exe'
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        try {
            $probeOutput = & $candidate -c $versionProbe 2>$null
            if ($LASTEXITCODE -eq 0 -and $probeOutput) { $selectedPython = [string]($probeOutput | Select-Object -Last 1); break }
        } catch { continue }
    }
}
if (-not $selectedPython) { throw 'Install Python 3.10 or newer with Tcl/Tk support, or run send_history.py with your Python interpreter.' }
& $selectedPython -B (Join-Path $PSScriptRoot 'send_history.py')
if ($LASTEXITCODE -ne 0) { throw "Guild history sender exited with code $LASTEXITCODE" }
