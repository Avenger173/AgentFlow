param(
    [string]$QtBinPath = "D:\IDE\qtcreator\6.11.0\msvc2022_64\bin"
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName System.Drawing

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$executable = Join-Path $repoRoot "build\codex-debug\AgentFlow.exe"
if (!(Test-Path $executable)) { throw "Missing test executable: $executable" }
if (!(Test-Path (Join-Path $QtBinPath "Qt6Core.dll"))) { throw "Missing Qt runtime: $QtBinPath" }

function Wait-Element {
    param([scriptblock]$Find, [string]$Description, [int]$TimeoutMs = 25000)
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMs)
    do {
        $element = & $Find
        if ($null -ne $element) { return $element }
        Start-Sleep -Milliseconds 150
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Timed out waiting for UI element: $Description"
}

function Find-ByIdSuffix {
    param([System.Windows.Automation.AutomationElement]$Root, [string]$Suffix)
    $elements = $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        if ($element.Current.AutomationId.EndsWith($Suffix, [System.StringComparison]::Ordinal)) {
            return $element
        }
    }
    return $null
}

function Find-ByName {
    param([System.Windows.Automation.AutomationElement]$Root, [string]$Name)
    $elements = $Root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        if ($element.Current.Name -eq $Name) { return $element }
    }
    return $null
}

function Find-ProcessWindow {
    param([int]$ProcessId, [string]$Name)
    $elements = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        if (($element.Current.ProcessId -eq $ProcessId) -and ($element.Current.Name -eq $Name)) {
            return $element
        }
    }
    return $null
}

function Invoke-Element {
    param([System.Windows.Automation.AutomationElement]$Element)
    $pattern = $null
    if (!$Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        throw "UI element cannot be invoked: $($Element.Current.AutomationId)"
    }
    $pattern.Invoke()
}

function Save-Evidence {
    param([System.Windows.Automation.AutomationElement]$Window, [string]$Directory)
    $rows = @()
    $elements = $Window.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    foreach ($element in $elements) {
        if ($element.Current.Name -or $element.Current.AutomationId) {
            $rows += "{0}$([char]9){1}$([char]9){2}" -f $element.Current.ControlType.ProgrammaticName,
                $element.Current.Name, $element.Current.AutomationId
        }
    }
    [System.IO.File]::WriteAllLines((Join-Path $Directory "workspace-uia-tree.tsv"), $rows)

    $bounds = $Window.Current.BoundingRectangle
    $bitmap = [System.Drawing.Bitmap]::new([int][Math]::Ceiling($bounds.Width), [int][Math]::Ceiling($bounds.Height))
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen([int]$bounds.X, [int]$bounds.Y, 0, 0, $bitmap.Size)
        $bitmap.Save((Join-Path $Directory "workspace-open.png"), [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$runId = "l1_gui_uia_" + (Get-Date -Format "yyyyMMddTHHmmss")
$evidenceDir = Join-Path $repoRoot "data\media_evaluations\$runId"
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
$previousDataDir = $env:AGENTFLOW_DATA_DIR
$previousPath = $env:PATH
$process = $null
$failure = $null

try {
    $env:AGENTFLOW_DATA_DIR = $evidenceDir
    $env:PATH = "$QtBinPath;$env:PATH"
    $process = Start-Process -FilePath $executable -PassThru
    $mainWindow = Wait-Element -Description "AgentFlow main window" -Find {
        $process.Refresh()
        if ($process.HasExited) { throw "AgentFlow exited: $($process.ExitCode)" }
        if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $null }
        return [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle)
    }

    $backendReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            if ((Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2).status -eq "ok") {
                $backendReady = $true
                break
            }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (!$backendReady) { throw "Local backend did not become ready." }

    Invoke-Element (Wait-Element -Description "vision navigation" -Find {
        Find-ByIdSuffix -Root $mainWindow -Suffix ".navVisionButton"
    })
    Invoke-Element (Wait-Element -Description "open workspace button" -Find {
        Find-ByName -Root $mainWindow -Name "mediaWorkspaceOpenButton"
    })
    $workspace = Wait-Element -Description "media workspace window" -Find {
        Find-ProcessWindow -ProcessId $process.Id -Name "mediaWorkspaceDialog"
    }
    Save-Evidence -Window $workspace -Directory $evidenceDir
    Write-Output "Media workspace Windows GUI open-path verification passed."
    Write-Output "Evidence: $evidenceDir"
} catch {
    $failure = $_
} finally {
    if ($process) {
        $process.Refresh()
        if (!$process.HasExited) {
            $process.CloseMainWindow() | Out-Null
            if (!$process.WaitForExit(8000)) { Stop-Process -Id $process.Id -Force }
        }
    }
    $env:AGENTFLOW_DATA_DIR = $previousDataDir
    $env:PATH = $previousPath
}

if ($failure) { throw "Media workspace GUI verification failed: $($failure.Exception.Message)" }
