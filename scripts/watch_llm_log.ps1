$ErrorActionPreference = 'Stop'
$appRoot = Split-Path -Parent $PSScriptRoot
$settingsPath = Join-Path $appRoot 'config\settings.json'

if (-not (Test-Path -LiteralPath $settingsPath)) {
    Write-Host "Settings file not found: $settingsPath" -ForegroundColor Red
    Read-Host 'Press Enter to close'
    exit 1
}

$settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
$dataPath = [Environment]::ExpandEnvironmentVariables([string]$settings.data_path)
if ($dataPath.StartsWith('~')) {
    $suffix = $dataPath.Substring(1).TrimStart('\', '/')
    $dataPath = if ($suffix) { Join-Path $HOME $suffix } else { $HOME }
}
$dataPath = [IO.Path]::GetFullPath($dataPath)
$folderId = [string]$settings.active_folder_id
$workspaceName = [string]$settings.default_folder_name
if (-not $workspaceName) { $workspaceName = 'Default' }

if ($folderId) {
    if ($folderId -notmatch '^[a-fA-F0-9]{32}$') {
        Write-Host 'The active folder ID in settings is invalid.' -ForegroundColor Red
        Read-Host 'Press Enter to close'
        exit 1
    }
    $folderManifest = Join-Path $dataPath 'folders.json'
    if (Test-Path -LiteralPath $folderManifest) {
        $folder = Get-Content -LiteralPath $folderManifest -Raw | ConvertFrom-Json |
            Where-Object { [string]$_.id -eq $folderId } | Select-Object -First 1
        if ($folder) { $workspaceName = [string]$folder.name }
    }
    $workspaceRoot = Join-Path $dataPath "workspaces\$folderId"
} else {
    $workspaceRoot = $dataPath
}

$logPath = Join-Path $workspaceRoot 'logs\llm_raw.jsonl'
Write-Host "Watching LLM activity for '$workspaceName'" -ForegroundColor Cyan
Write-Host "Log: $logPath" -ForegroundColor DarkGray
Write-Host 'Waiting for LLM log entries. Press Ctrl+C to stop.' -ForegroundColor DarkGray

while (-not (Test-Path -LiteralPath $logPath)) { Start-Sleep -Milliseconds 500 }

Get-Content -LiteralPath $logPath -Tail 20 -Wait | ForEach-Object {
    $line = $_
    try {
        $entry = $line | ConvertFrom-Json
        $stamp = [string]$entry.timestamp
        $event = [string]$entry.event
        $task = [string]$entry.task
        $source = [string]$entry.source_file
        $segment = if ($entry.segment_count) { " segment $($entry.segment)/$($entry.segment_count)" } else { '' }
        Write-Host "$stamp  $event  $task  $source$segment" -ForegroundColor Cyan

        if ($event -eq 'request_sent' -and $entry.request) {
            Write-Host "  Outgoing URL: $($entry.request.url)" -ForegroundColor Yellow
            Write-Host '  Outgoing payload:' -ForegroundColor Yellow
            $payloadText = $entry.request.payload | ConvertTo-Json -Depth 100
            Write-Host $payloadText -ForegroundColor Gray
        }

        if ($entry.error) { Write-Host "  Error: $($entry.error)" -ForegroundColor Red }
        if ($entry.error_body) { Write-Host "  Provider: $($entry.error_body)" -ForegroundColor DarkRed }
        if ($entry.response_text) {
            $response = [string]$entry.response_text
            if ($response.Length -gt 1200) { $response = $response.Substring(0, 1200) + ' … [truncated]' }
            Write-Host "  Response: $response" -ForegroundColor Gray
        }
        if ($entry.prompt -and -not $entry.request) {
            Write-Host '  Prompt:' -ForegroundColor Yellow
            Write-Host ([string]$entry.prompt) -ForegroundColor Gray
        }
    } catch {
        Write-Host $line -ForegroundColor Gray
    }
}
