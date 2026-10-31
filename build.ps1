param(
    [string]$Python = "python",
    [switch]$SkipInstall,
    # Optional public community endpoint embedded into the release. Values in
    # a distributed executable are obfuscated, but cannot be kept secret from
    # a determined recipient.
    [string]$CommunityWebhook = "",
    [string]$CommunityMessageId = ""
)

$ErrorActionPreference = "Stop"
$BuildArguments = @("--format", "exe", "--python", $Python)
if ($SkipInstall) { $BuildArguments += "--skip-install" }
if ($CommunityWebhook) { $BuildArguments += @("--community-webhook", $CommunityWebhook) }
if ($CommunityMessageId) { $BuildArguments += @("--community-message-id", $CommunityMessageId) }

# Native Windows/Linux/macOS packaging is centralized in the repository-root
# build.py. This wrapper keeps the familiar Windows PowerShell entry point.
& $Python (Join-Path $PSScriptRoot "build.py") @BuildArguments
exit $LASTEXITCODE
