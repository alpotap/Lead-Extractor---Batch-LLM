param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$packageName = "Lead-Extractor-Windows"
$distDirectory = Join-Path $repoRoot "dist"
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $distDirectory "$packageName.zip"
} elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $repoRoot $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)

New-Item -ItemType Directory -Path $distDirectory -Force | Out-Null
$stageRoot = Join-Path ([IO.Path]::GetTempPath()) "lead-extractor-package-$([guid]::NewGuid().ToString('N'))"
$packageDirectory = Join-Path $stageRoot $packageName
New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null

try {
    $files = @(
        "app.py",
        "README.md",
        "REFERENCE.md",
        "LICENSE",
        "VERSION",
        "requirements.txt",
        "Start Lead Desk.bat",
        "docs/MACOS.md",
        "docs/LINUX.md",
        "Start Lead Extractor.command",
        "Start Lead Extractor.sh"
    )
    foreach ($file in $files) {
        $destination = Join-Path $packageDirectory $file
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $repoRoot $file) -Destination $destination
    }

    foreach ($directory in @("assets", "config", "models", "services", "scripts")) {
        $source = Join-Path $repoRoot $directory
        $destination = Join-Path $packageDirectory $directory
        New-Item -ItemType Directory -Path $destination -Force | Out-Null
        Get-ChildItem -LiteralPath $source -File -Recurse |
            Where-Object { $_.Name -notin @("settings.json", "secrets.json") -and $_.Extension -ne ".pyc" } |
            ForEach-Object {
                $relativePath = $_.FullName.Substring($source.Length).TrimStart([IO.Path]::DirectorySeparatorChar)
                $target = Join-Path $destination $relativePath
                New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
                Copy-Item -LiteralPath $_.FullName -Destination $target
            }
    }

    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Force
    }
    Compress-Archive -Path (Join-Path $packageDirectory "*") -DestinationPath $OutputPath -CompressionLevel Optimal
    Write-Output "Created $OutputPath"
}
finally {
    $resolvedTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $resolvedStage = [IO.Path]::GetFullPath($stageRoot)
    if ($resolvedStage.StartsWith($resolvedTemp, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolvedStage)) {
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
