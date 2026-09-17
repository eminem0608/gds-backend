param(
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [string]$HostName = "localhost",
    [int]$Port = 5432,
    [string]$UserName = "odoo",
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"
$pgDump = Get-Command pg_dump -ErrorAction SilentlyContinue
if (-not $pgDump) {
    throw "pg_dump is not installed or is not available in PATH."
}
if (-not $env:PGPASSWORD) {
    throw "Set the PGPASSWORD environment variable before running this script."
}
if (-not $OutputPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $OutputPath = Join-Path $PSScriptRoot "..\..\docs\database\postgres-schema-$Database-$stamp.sql"
}

$resolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $OutputPath))
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
if (-not $resolvedParent.StartsWith($workspace, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The output directory must remain inside the workspace: $workspace"
}
New-Item -ItemType Directory -Force -Path $resolvedParent | Out-Null

$arguments = @(
    "--host", $HostName,
    "--port", $Port,
    "--username", $UserName,
    "--dbname", $Database,
    "--schema-only",
    "--no-owner",
    "--no-privileges",
    "--file", $OutputPath
)
& $pgDump.Source @arguments
if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed with exit code $LASTEXITCODE"
}
Write-Output "PostgreSQL schema exported to: $([System.IO.Path]::GetFullPath($OutputPath))"
