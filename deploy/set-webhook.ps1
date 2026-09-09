<#
.SYNOPSIS
    Store this bot's Slack webhook URL in Secret Manager as a new version.

.DESCRIPTION
    Reads the URL from the clipboard by default, because Read-Host
    -AsSecureString swallows Ctrl+V in several Windows terminals -- it accepts
    the keystroke but registers nothing, so a paste silently becomes an empty
    or one-character secret that only surfaces much later as a 404 from Slack.

    The value is validated against the real webhook shape before it is stored
    and is never printed, only described.

    Use this to set the webhook for the first time or to rotate it; the Cloud
    Run job reads ':latest', so a new version takes effect on the next tick
    with no redeploy.

    Note the default secret name. The perp-premiums bot shares this project and
    owns 'slack-webhook-url'; writing this bot's URL there would silently
    redirect that bot's digests into this bot's channel.

.EXAMPLE
    # Copy the webhook URL to the clipboard first, then:
    .\deploy\set-webhook.ps1 -Project plucky-vision-508102-k7

.EXAMPLE
    # If the clipboard is not an option, read it from a file instead.
    .\deploy\set-webhook.ps1 -Project plucky-vision-508102-k7 -FromFile C:\tmp\hook.txt
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Project,

    [string]$Secret = "hl-usdc-slack-webhook-url",

    # Read the URL from this file rather than the clipboard.
    [string]$FromFile
)

$ErrorActionPreference = "Stop"

# Slack incoming webhooks are always three path segments under /services/.
$PATTERN = '^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+$'

function Initialize-GCloudPath {
    if (Get-Command gcloud -ErrorAction SilentlyContinue) { return }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
    if (Get-Command gcloud -ErrorAction SilentlyContinue) { return }
    $candidates = @(
        "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "$env:APPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "$env:ProgramFiles\Google\Cloud SDK\google-cloud-sdk\bin",
        "${env:ProgramFiles(x86)}\Google\Cloud SDK\google-cloud-sdk\bin"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate "gcloud.cmd")) {
            $env:Path = "$candidate;$env:Path"
            return
        }
    }
    throw "gcloud not found. Install the Google Cloud CLI and re-run."
}

Initialize-GCloudPath

if ($FromFile) {
    if (-not (Test-Path $FromFile)) { throw "No such file: $FromFile" }
    $url = ([IO.File]::ReadAllText($FromFile)).Trim()
    $origin = "file $FromFile"
} else {
    $raw = Get-Clipboard -Raw
    if ($null -eq $raw) { throw "The clipboard is empty. Copy the webhook URL, then re-run." }
    $url = $raw.Trim()
    $origin = "clipboard"
}

# Fail here rather than at 03:00 on a Tuesday. A wrong value costs one silent
# hour per tick and reports nothing, because Slack answers a bad webhook with a
# 404 that only appears in the job logs.
if ($url -notmatch $PATTERN) {
    throw ("The $origin does not hold a Slack webhook URL (got $($url.Length) " +
        "characters). Expected https://hooks.slack.com/services/T.../B.../...")
}

$tail = $url.Substring($url.Length - 4)
Write-Host "Secret : $Secret"
Write-Host "Source : $origin"
Write-Host "Shape  : OK -- $($url.Length) characters, ending ...$tail"

function Test-SecretExists {
    <# Only $LASTEXITCODE is trusted: in 5.1 a native command's stderr arrives
       as ErrorRecords, which would otherwise trip 'Stop'. #>
    $prior = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & gcloud secrets describe $Secret --project $Project 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prior
    }
}

if (Test-SecretExists) {
    Write-Host "Adding a new version to '$Secret'."
    $url | & gcloud secrets versions add $Secret --data-file=- --project $Project
} else {
    Write-Host "Creating secret '$Secret'."
    $url | & gcloud secrets create $Secret --data-file=- --replication-policy=automatic --project $Project
}
if ($LASTEXITCODE -ne 0) { throw "Could not write the secret (exit $LASTEXITCODE)" }

# Read it straight back. The pipe appends a newline, which config.py strips, so
# the check compares trimmed values.
$stored = ((& gcloud secrets versions access latest --secret=$Secret --project $Project) | Out-String).Trim()
if ($stored -ne $url) { throw "Read-back mismatch: the stored secret is not what was sent." }

Write-Host ""
Write-Host "Stored and verified by read-back." -ForegroundColor Green
Write-Host "The job reads ':latest', so the next tick picks this up with no redeploy."
