# Run from your own Windows PowerShell session.
$ErrorActionPreference = "Stop"
$secure = Read-Host "Meko API key" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $env:MEKO_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
}
if (-not $env:MEKO_DATAPACK_ID) {
    $env:MEKO_DATAPACK_ID = "3985023e-ece5-4b81-9833-2132c3b7ce24"
}
Write-Host "Run .\start-live.cmd in this window to link the project, verify Meko and start Scarline."
