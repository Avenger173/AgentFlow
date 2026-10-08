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
        try { $element = & $Find } catch {
            if ($_.Exception.ToString() -notmatch "0x8000FFFF") { throw }
            $element = $null
        }
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

function Find-ByIdSuffixOrName {
    param(
        [System.Windows.Automation.AutomationElement]$Root,
        [string]$Suffix,
        [string]$Name
    )
    $byId = Find-ByIdSuffix -Root $Root -Suffix $Suffix
    if ($null -ne $byId) { return $byId }
    return Find-ByName -Root $Root -Name $Name
}

function Invoke-Element {
    param([System.Windows.Automation.AutomationElement]$Element)
    $pattern = $null
    if (!$Element.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$pattern)) {
        throw "UI element cannot be invoked: $($Element.Current.AutomationId)"
    }
    $pattern.Invoke()
}

function Save-Screenshot {
    param([System.Windows.Automation.AutomationElement]$Window, [string]$Path)
    $bounds = $Window.Current.BoundingRectangle
    $bitmap = [System.Drawing.Bitmap]::new([int][Math]::Ceiling($bounds.Width), [int][Math]::Ceiling($bounds.Height))
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen([int]$bounds.X, [int]$bounds.Y, 0, 0, $bitmap.Size)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
}

$runId = "mm4_video_gui_" + (Get-Date -Format "yyyyMMddTHHmmss")
$evidenceDir = Join-Path $repoRoot "data\media_evaluations\$runId"
New-Item -ItemType Directory -Force -Path $evidenceDir | Out-Null
$previousDataDir = $env:AGENTFLOW_DATA_DIR
$previousPath = $env:PATH
$previousChatMode = $env:AGENTFLOW_CHAT_MODE
$process = $null
$failure = $null

try {
    $env:AGENTFLOW_DATA_DIR = $evidenceDir
    $env:PATH = "$QtBinPath;$env:PATH"
    $env:AGENTFLOW_CHAT_MODE = "mock"
    $process = Start-Process -FilePath $executable -PassThru
    $mainWindow = Wait-Element -Description "AgentFlow main window" -Find {
        $process.Refresh()
        if ($process.HasExited) { throw "AgentFlow exited: $($process.ExitCode)" }
        if ($process.MainWindowHandle -eq [IntPtr]::Zero) { return $null }
        return [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$process.MainWindowHandle)
    }

    $videoNavigation = Wait-Element -Description "video navigation" -Find {
        Find-ByIdSuffix -Root $mainWindow -Suffix ".navVideoButton"
    }
    Invoke-Element -Element $videoNavigation

    $chooseButton = Wait-Element -Description "choose video button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoChooseButton" -Name "选择并导入视频"
    }
    $goalEdit = Wait-Element -Description "video goal editor" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoGoalEdit" -Name "剪辑目标输入"
    }
    $candidateButton = Wait-Element -Description "generate candidate button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoCandidateButton" -Name "生成剪辑候选"
    }
    $briefGoalEdit = Wait-Element -Description "video brief goal editor" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoBriefGoalEdit" -Name "讲解目标输入"
    }
    $briefButton = Wait-Element -Description "generate video brief button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoBriefButton" -Name "生成离线讲解网页"
    }
    $fullSrtButton = Wait-Element -Description "full SRT button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoFullSrtButton" -Name "保存完整 SRT"
    }
    $cutSrtButton = Wait-Element -Description "cut SRT button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoCutSrtButton" -Name "保存成片 SRT"
    }
    $renderButton = Wait-Element -Description "render MP4 button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoRenderButton" -Name "确认渲染 MP4"
    }
    $delegateButton = Wait-Element -Description "delegate video button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoDelegateButton" -Name "交给调度台"
    }
    $transcribeButton = Wait-Element -Description "submit transcription button" -Find {
        Find-ByIdSuffixOrName -Root $mainWindow -Suffix ".videoTranscribeButton" -Name "提交转写"
    }
    if (!$chooseButton.Current.IsEnabled -or !$goalEdit.Current.IsEnabled -or !$briefGoalEdit.Current.IsEnabled) {
        throw "Video workspace must allow material selection and both goal inputs."
    }
    if ($transcribeButton.Current.IsEnabled -or $candidateButton.Current.IsEnabled -or $fullSrtButton.Current.IsEnabled `
        -or $cutSrtButton.Current.IsEnabled -or $renderButton.Current.IsEnabled -or $briefButton.Current.IsEnabled `
        -or $delegateButton.Current.IsEnabled) {
        throw "Video actions must stay disabled before selecting and transcribing a local file."
    }
    Save-Screenshot -Window $mainWindow -Path (Join-Path $evidenceDir "video-workspace-open.png")
    Write-Output "Media video workspace GUI smoke passed."
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
    $env:AGENTFLOW_CHAT_MODE = $previousChatMode
}

if ($failure) { throw "Media video workspace GUI verification failed: $($failure.Exception.Message)" }
