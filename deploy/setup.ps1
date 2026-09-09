<#
.SYNOPSIS
    Provision the hourly Hyperliquid USDC utilization bot on Google Cloud.

.DESCRIPTION
    Creates, in one project: a Secret Manager secret holding the Slack webhook,
    two service accounts, a GCS bucket for the bot's state, a Cloud Run Job
    built from this repository, and a Cloud Scheduler job that triggers it
    hourly.

    A Job rather than a Service because tick_once.py is already a batch
    entrypoint that runs and exits, so no HTTP wrapper is needed -- and because
    Scheduler's :run call returns as soon as the execution is enqueued. Nothing
    holds a connection open for the length of the tick, so a slow Hyperliquid
    read cannot trip a scheduler deadline and provoke a retry that posts twice.
    That matters here in a way it would not for a stateless bot: this one
    mutates dedupe state, so a duplicate run is a duplicate alert.

    Idempotent. Every step checks for the resource first, so re-running after a
    failure resumes rather than erroring, and re-running after a code change
    simply redeploys the job.

    Written for Windows PowerShell 5.1, which has no '&&', '||' or ternaries --
    hence the explicit $LASTEXITCODE checks. Arguments are collected into arrays
    and splatted rather than continued with backticks.

.EXAMPLE
    .\deploy\setup.ps1 -Project plucky-vision-508102-k7 -DryRun -SeedState

    First run. -DryRun deploys with DRY_RUN=1 so the job renders the payload
    into Cloud Logging without posting to Slack. -SeedState uploads the repo's
    state.json so the first tick inherits the band the bot was already tracking
    instead of announcing FIRST_RUN. Re-run without -DryRun to go live.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Project,

    # Singapore: the nearest region to Manila, and one where Cloud Run, Cloud
    # Scheduler, Cloud Storage and Artifact Registry are all available.
    [string]$Region = "asia-southeast1",

    [string]$Job = "hl-usdc-tick",
    [string]$SchedulerJob = "hl-usdc-tick-hourly",

    # Distinct from the perp-premiums bot's 'slack-webhook-url'. The two bots
    # share this project but post to different channels with different webhooks,
    # and crossing them would be silent -- each URL is valid, just wrong.
    [string]$Secret = "hl-usdc-slack-webhook-url",

    # Bucket names are globally unique, so this is derived from the project id.
    [string]$Bucket = "",
    [string]$StateObject = "state.json",

    [string]$Schedule = "0 * * * *",
    [string]$TimeZone = "Asia/Manila",

    # Deploy with DRY_RUN=1: render and log the payload, post nothing.
    [switch]$DryRun,

    # Upload the repository's state.json to the bucket, but only if the bucket
    # holds no state yet. Preserves last_band across the migration.
    [switch]$SeedState
)

$ErrorActionPreference = "Stop"

if (-not $Bucket) { $Bucket = "$Project-hl-usdc-state" }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$JobSa = "hl-usdc-job@$Project.iam.gserviceaccount.com"
$SchedulerSa = "hl-usdc-scheduler@$Project.iam.gserviceaccount.com"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Invoke-GCloud {
    <# Run gcloud and stop the script if it fails. $ErrorActionPreference does
       not apply to native executables, so the exit code is checked by hand. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    Write-Host "    gcloud $($Arguments -join ' ')" -ForegroundColor DarkGray
    & gcloud @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud $($Arguments -join ' ') failed (exit $LASTEXITCODE)"
    }
}

function Test-Exists {
    <# True when the describe call succeeds. Only $LASTEXITCODE is trusted: in
       5.1 a native command's stderr arrives as ErrorRecords, which would
       otherwise trip $ErrorActionPreference = 'Stop'. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $prior = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & gcloud @Arguments 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $prior
    }
}

function Initialize-GCloudPath {
    <# Make 'gcloud' callable in this session, whatever the shell inherited.

       A terminal opened before the installer ran still holds the pre-install
       PATH. VSCode's integrated terminals are worse: they inherit the editor's
       environment, captured when VSCode launched, so even a brand-new terminal
       keeps the stale copy until the whole editor restarts. Re-reading PATH
       from the registry fixes both without anyone restarting anything. #>
    if (Get-Command gcloud -ErrorAction SilentlyContinue) { return }

    Write-Host "    gcloud not in this session's PATH; re-reading it from the registry."
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
    if (Get-Command gcloud -ErrorAction SilentlyContinue) { return }

    # Still nothing: look where the installer actually puts it.
    $candidates = @(
        "$env:LOCALAPPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "$env:APPDATA\Google\Cloud SDK\google-cloud-sdk\bin",
        "$env:ProgramFiles\Google\Cloud SDK\google-cloud-sdk\bin",
        "${env:ProgramFiles(x86)}\Google\Cloud SDK\google-cloud-sdk\bin",
        "$env:USERPROFILE\google-cloud-sdk\bin"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path (Join-Path $candidate "gcloud.cmd")) {
            Write-Host "    Found it at $candidate"
            $env:Path = "$candidate;$env:Path"
            return
        }
    }

    throw ("gcloud not found. Install the Google Cloud CLI from " +
        "https://cloud.google.com/sdk/docs/install, then re-run this script.")
}

Initialize-GCloudPath

if ($DryRun) { $mode = "DRY RUN (nothing reaches Slack)" } else { $mode = "LIVE" }
Write-Host "Project : $Project"
Write-Host "Region  : $Region"
Write-Host "Bucket  : gs://$Bucket"
Write-Host "Mode    : $mode"

# --- 1. APIs ----------------------------------------------------------------
# Enabling an already-enabled API is a no-op, so this needs no existence check.
Write-Step "Enabling APIs (slow on a fresh project)"
$apiArgs = @(
    "services", "enable",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "--project", $Project
)
Invoke-GCloud @apiArgs

# --- 2. The webhook ---------------------------------------------------------
Write-Step "Slack webhook secret"
if (Test-Exists secrets describe $Secret --project $Project) {
    Write-Host "    Secret '$Secret' already exists; its value is left alone."
    Write-Host "    To rotate: .\deploy\set-webhook.ps1 -Project $Project"
} else {
    # Read-Host -AsSecureString swallows Ctrl+V in several Windows terminals:
    # it accepts the keystroke and registers nothing, so a paste becomes a
    # one-character secret that surfaces much later as a 404 from Slack. The
    # clipboard is read directly instead, and the shape is checked before the
    # value is stored. set-webhook.ps1 does the same thing on its own.
    Write-Host "    Reading the webhook URL from the clipboard."
    Write-Host "    This must be THIS bot's webhook, not the perp-premiums one." -ForegroundColor Yellow
    $raw = Get-Clipboard -Raw
    if ($null -eq $raw) {
        throw ("The clipboard is empty. Copy the webhook URL and re-run, or use " +
            ".\deploy\set-webhook.ps1 -Project $Project -FromFile <path>")
    }
    $plain = $raw.Trim()
    $pattern = '^https://hooks\.slack\.com/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9_-]+$'
    if ($plain -notmatch $pattern) {
        throw ("The clipboard does not hold a Slack webhook URL (got " +
            "$($plain.Length) characters). Expected " +
            "https://hooks.slack.com/services/T.../B.../...")
    }
    Write-Host "    Shape OK -- $($plain.Length) characters."
    # config.py strips the trailing newline the pipe appends.
    $plain | & gcloud secrets create $Secret --data-file=- --replication-policy=automatic --project $Project
    if ($LASTEXITCODE -ne 0) { throw "Could not create secret '$Secret'" }
    Remove-Variable plain
}

# --- 3. Identities ----------------------------------------------------------
# Two accounts rather than one: the trigger has no business reading the webhook
# or the state, and the job has no business being able to trigger itself.
Write-Step "Service accounts"
if (Test-Exists iam service-accounts describe $JobSa --project $Project) {
    Write-Host "    $JobSa exists."
} else {
    Invoke-GCloud iam service-accounts create hl-usdc-job --display-name "HL USDC bot job runtime" --project $Project
}
if (Test-Exists iam service-accounts describe $SchedulerSa --project $Project) {
    Write-Host "    $SchedulerSa exists."
} else {
    Invoke-GCloud iam service-accounts create hl-usdc-scheduler --display-name "HL USDC bot scheduler trigger" --project $Project
}

Write-Step "IAM: the job may read the secret and write logs"
$secretBinding = @(
    "secrets", "add-iam-policy-binding", $Secret,
    "--member", "serviceAccount:$JobSa",
    "--role", "roles/secretmanager.secretAccessor",
    "--project", $Project
)
Invoke-GCloud @secretBinding

# A custom service account carries no roles at all. Without logWriter the tick
# runs but its output never reaches Cloud Logging, which is the only place the
# decision line from runner.py can be inspected.
$logBinding = @(
    "projects", "add-iam-policy-binding", $Project,
    "--member", "serviceAccount:$JobSa",
    "--role", "roles/logging.logWriter",
    "--condition", "None"
)
Invoke-GCloud @logBinding

# --- 4. State ---------------------------------------------------------------
# The bot remembers last_post_ts and last_band between ticks. Cloud Run's
# filesystem is ephemeral, so that has to live somewhere else, or the bot
# forgets its band every hour and re-fires crossings it has already announced.
Write-Step "State bucket"
if (Test-Exists storage buckets describe "gs://$Bucket" --project $Project) {
    Write-Host "    gs://$Bucket exists."
} else {
    $bucketArgs = @(
        "storage", "buckets", "create", "gs://$Bucket",
        "--project", $Project,
        "--location", $Region,
        "--uniform-bucket-level-access",
        "--public-access-prevention"
    )
    Invoke-GCloud @bucketArgs
}

# objectUser covers get, create, delete and list -- the store overwrites one
# object in place. Older gcloud installs may not know the role; objectAdmin is
# the equivalent predecessor.
$storageBinding = @(
    "storage", "buckets", "add-iam-policy-binding", "gs://$Bucket",
    "--member", "serviceAccount:$JobSa",
    "--role", "roles/storage.objectUser",
    "--project", $Project
)
try {
    Invoke-GCloud @storageBinding
} catch {
    Write-Host "    roles/storage.objectUser was rejected; falling back to objectAdmin." -ForegroundColor Yellow
    # Index 7 is the role value; see the array above.
    $storageBinding[7] = "roles/storage.objectAdmin"
    Invoke-GCloud @storageBinding
}

if ($SeedState) {
    $localState = Join-Path $RepoRoot "state.json"
    if (Test-Exists storage objects describe "gs://$Bucket/$StateObject" --project $Project) {
        Write-Host "    gs://$Bucket/$StateObject already exists; not overwriting it."
    } elseif (-not (Test-Path $localState)) {
        Write-Host "    No local state.json to seed from; the first tick will be FIRST_RUN."
    } else {
        # Carries last_band across the migration, so the first Cloud Run tick
        # does not re-announce a crossing the Actions run already sent.
        Invoke-GCloud storage cp $localState "gs://$Bucket/$StateObject" --project $Project
    }
}

# --- 5. The job -------------------------------------------------------------
# --source builds on Cloud Build, so no local Docker is needed. The first run
# offers to create the 'cloud-run-source-deploy' Artifact Registry repository;
# answer yes.
#
# --max-retries 0, unlike the perp-premiums job, which uses 2. Walk the failure
# modes. A failed Hyperliquid read posts nothing and saves nothing, so the next
# hourly tick recovers and no crossing is lost -- decide() compares the live
# band against the stored one, so a crossing is delayed, never dropped. A failed
# Slack post raises before store.save(), so state stays put and the next tick
# retries by itself. The only case a retry would change is a Slack post that
# succeeded followed by a state write that did not -- and there a retry re-reads
# stale state and posts a duplicate. Retrying can only hurt.
Write-Step "Building and deploying the Cloud Run job"
Push-Location $RepoRoot
try {
    $deployArgs = @(
        "run", "jobs", "deploy", $Job,
        "--source", ".",
        "--region", $Region,
        "--project", $Project,
        "--service-account", $JobSa,
        "--env-vars-file", "deploy/env.yaml",
        "--set-secrets", "SLACK_WEBHOOK_URL=${Secret}:latest",
        "--task-timeout", "5m",
        "--max-retries", "0",
        "--memory", "512Mi"
    )
    Invoke-GCloud @deployArgs

    # --env-vars-file cannot be combined with --update-env-vars, so the
    # overrides are a second call rather than flags on the deploy. STATE_BUCKET
    # is re-asserted here so -Bucket and -Project stay authoritative even if
    # deploy/env.yaml drifts.
    $overrides = "STATE_BUCKET=$Bucket,STATE_OBJECT=$StateObject"
    if ($DryRun) { $overrides = "$overrides,DRY_RUN=1" }
    Invoke-GCloud run jobs update $Job --region $Region --project $Project --update-env-vars $overrides
} finally {
    Pop-Location
}

Write-Step "IAM: the scheduler may invoke the job"
$invokerBinding = @(
    "run", "jobs", "add-iam-policy-binding", $Job,
    "--member", "serviceAccount:$SchedulerSa",
    "--role", "roles/run.invoker",
    "--region", $Region,
    "--project", $Project
)
Invoke-GCloud @invokerBinding

# --- 6. The schedule --------------------------------------------------------
# OAuth rather than OIDC: the target is a Google API, not an endpoint of ours.
# The :run call only enqueues an execution and returns, so 30s is generous and a
# slow tick can never cause the scheduler to retry mid-flight. These retries
# cover a genuinely failed enqueue -- they cannot double-run a tick that has
# already started, which is what --max-retries on the job governs.
Write-Step "Hourly trigger"
$uri = "https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$Project/jobs/${Job}:run"
$schedulerArgs = @(
    "--location", $Region,
    "--project", $Project,
    "--schedule", $Schedule,
    "--time-zone", $TimeZone,
    "--uri", $uri,
    "--http-method", "POST",
    "--oauth-service-account-email", $SchedulerSa,
    "--attempt-deadline", "30s",
    "--max-retry-attempts", "3",
    "--min-backoff", "10s",
    "--max-backoff", "60s"
)
# The array must be splatted, not passed as one positional argument: a bare
# array would arrive as a single joined string and gcloud would reject it.
if (Test-Exists scheduler jobs describe $SchedulerJob --location $Region --project $Project) {
    $schedulerCmd = @("scheduler", "jobs", "update", "http", $SchedulerJob) + $schedulerArgs
} else {
    $schedulerCmd = @("scheduler", "jobs", "create", "http", $SchedulerJob) + $schedulerArgs
}
Invoke-GCloud @schedulerCmd

# --- 7. The two things that fail silently ------------------------------------
# Without STATE_BUCKET the job falls back to a local file on an ephemeral disk,
# reads no state, decides FIRST_RUN and posts -- every hour, forever. And a
# webhook that landed as a literal rather than a secret reference is readable by
# anyone who can describe the job. Neither shows up as an error at deploy time.
Write-Step "Verifying the job spec"
$spec = (& gcloud run jobs describe $Job --region $Region --project $Project --format=json) | Out-String
if ($spec -notmatch "STATE_BUCKET") {
    throw ("STATE_BUCKET is not in the deployed job spec. The bot would use an " +
        "ephemeral local file and post FIRST_RUN every hour. Do not go live.")
}
if ($spec -match "hooks\.slack\.com") {
    throw ("The webhook appears as a literal value in the job spec rather than a " +
        "Secret Manager reference. Do not go live.")
}
Write-Host "    STATE_BUCKET present; webhook is a secret reference." -ForegroundColor Green

Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "Force one now : gcloud run jobs execute $Job --region $Region --project $Project --wait"
Write-Host "List runs     : gcloud run jobs executions list --job $Job --region $Region --project $Project"
Write-Host "Read state    : gcloud storage cat gs://$Bucket/$StateObject"
if ($DryRun) {
    Write-Host ""
    Write-Host "DRY_RUN=1 is set, so the job posts nothing -- but it DOES still write" -ForegroundColor Yellow
    Write-Host "state to GCS, which is what makes a dry run a real test of the bucket" -ForegroundColor Yellow
    Write-Host "wiring. Re-run this script without -DryRun once the payload looks right." -ForegroundColor Yellow
}
