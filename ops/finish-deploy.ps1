<#
.SYNOPSIS
    Ship the pending work and turn on write auth, in the one order that is safe.

.DESCRIPTION
    PowerShell twin of ops/finish-deploy.sh, for running from a normal PowerShell
    prompt instead of Git Bash. Same three steps, same gate.

    The steps have a hard dependency that is easy to get wrong and unpleasant to undo:

        1. push            -> the VPS auto-deploys within 5 minutes of a green CI
        2. API_ADMIN_TOKEN -> ONLY once the new frontend is actually live
        3. auto-deploy.sh  -> any time after

    Setting the token first locks you out of your own site: the currently-deployed
    frontend has no lock button, so its sync and base-currency controls start getting
    401s with no way to supply the key. Recovering means another ssh trip to unset it.

    So this refuses to reach step 2 until /health reports the commit it pushed.
    Every step asks first and can be skipped; nothing here is silent.

.EXAMPLE
    pwsh -NoProfile -File .\ops\finish-deploy.ps1
#>
[CmdletBinding()]
param(
    [string]$DeployHost = 'portfolio.srv1211053.hstgr.cloud',
    [string]$SshKey     = "$HOME\.ssh\id_ed25519_hostinger"
)

$ErrorActionPreference = 'Stop'

$Remote  = "root@$DeployHost"
$EnvPath = '/root/IBKR_investment_tracker/backend/.env'
$Health  = "https://$DeployHost/health"

function Say  { param($m) Write-Host "`n== $m" -ForegroundColor White }
function Warn { param($m) Write-Host "!! $m" -ForegroundColor Yellow }
function Die  { param($m) Write-Host "XX $m" -ForegroundColor Red; exit 1 }

function Ask {
    param($Question)
    (Read-Host "$Question [y/N]") -match '^[Yy]$'
}

# Berlin time from .NET, which knows the DST rules. Never from the shell clock.
function Get-BerlinTime {
    foreach ($id in @('Europe/Berlin', 'W. Europe Standard Time')) {
        try {
            $tz = [System.TimeZoneInfo]::FindSystemTimeZoneById($id)
            return [System.TimeZoneInfo]::ConvertTimeFromUtc([DateTime]::UtcNow, $tz)
        } catch { continue }
    }
    return $null
}

# --- preflight -------------------------------------------------------------
Say 'Preflight'

Set-Location (Join-Path $PSScriptRoot '..')

if (git status --porcelain) { Die 'Working tree is dirty. Commit or stash first.' }

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne 'main') { Die "On '$branch', not main." }

$ahead = [int](git rev-list --count origin/main..main).Trim()
Write-Host "Branch main, tree clean, $ahead commit(s) to push."
if ($ahead -eq 0) { Warn 'Nothing to push - skipping to the verification step.' }

# A deploy rebuilding during a Berlin sync slot used to lose that sync outright. The
# persistent job store now recovers a miss under 30 min, but it is not live until this
# very deploy lands, so the first push still has to dodge the slots by hand.
$berlin = Get-BerlinTime
if ($null -eq $berlin) {
    Warn 'Could not resolve the Europe/Berlin timezone, so the sync-slot check was skipped.'
    Warn 'Slots are on the hour at 00/08/11/13/15/18/20/22 Berlin.'
    if (-not (Ask 'Continue anyway?')) { Die 'Stopped.' }
} else {
    $nowMin = $berlin.Hour * 60 + $berlin.Minute
    # Keep in step with the Bash twin and ops/auto-deploy.sh; all three are copies of
    # ALL_SYNC_HOURS in backend/app/services/scheduler_service.py, and
    # tests/test_deploy_guard_hours.py fails if any of them disagrees.
    # 00:00  08:00  11:00  13:00  15:00  18:00  20:00  22:00
    $near = @(0, 480, 660, 780, 900, 1080, 1200, 1320) | Where-Object {
        $d = [Math]::Abs($nowMin - $_)
        # Wrap around midnight, so 23:55 is 5 minutes from the 00:00 slot, not 1435.
        if ($d -gt 720) { $d = 1440 - $d }
        $d -le 12
    }
    if ($near) {
        $slot = '{0:00}:{1:00}' -f [int]($near[0] / 60), ($near[0] % 60)
        Warn ("It is {0} Berlin - within 12 minutes of the {1} sync slot." -f $berlin.ToString('HH:mm'), $slot)
        Warn 'Auto-deploy rebuilds in ~90s and would land on top of it.'
        if (-not (Ask 'Push anyway?')) { Die 'Stopped. Try again in ~15 minutes.' }
    } else {
        Write-Host ("Berlin time {0} - clear of every sync slot." -f $berlin.ToString('HH:mm'))
    }
}

# --- step 1: push ----------------------------------------------------------
$target = (git rev-parse HEAD).Trim()
if ($ahead -gt 0) {
    Say 'Step 1/3 - push to origin/main'
    git --no-pager log --oneline origin/main..main | ForEach-Object { "    $_" }
    if (-not (Ask 'Push these?')) { Die 'Stopped before pushing.' }
    git push origin main
    if ($LASTEXITCODE -ne 0) { Die 'Push failed.' }
    Write-Host 'Pushed. The VPS cron picks it up within 5 minutes of a green CI.'
}

# --- step 2 gate: the deploy must actually be live -------------------------
Say "Waiting for $($target.Substring(0,7)) to go live at $Health"
Write-Host 'Auto-deploy runs every 5 min once CI is green, and the rebuild takes a few minutes, so allow ~10 min.'
Write-Host '(Ctrl-C is safe - the deploy continues; re-run this script to pick up here.)'

# Two accepted signals, because an exact sha match is not always available:
#
#   * commit == target                     -> unambiguous, the normal case.
#   * commit == "unknown" AND the response carries the new-build marker fields.
#     deploy.sh pulls the repo *itself*, so the copy already executing is the one from
#     before the pull - bash does not reload a running script. Any deploy that changes
#     deploy.sh therefore runs the OLD logic once, and the GIT_COMMIT export it gained
#     is missing for exactly that run. Matching only on sha hangs the full 15 minutes
#     over a cosmetic stamp. Hit on 2026-07-31.
#
# write_auth_enabled is the marker: it exists in no build predating this work, so a
# rolled-back old build cannot satisfy it.
$deadline = (Get-Date).AddMinutes(15)
$landed = $false
while ((Get-Date) -lt $deadline) {
    try   { $resp = Invoke-RestMethod -Uri $Health -TimeoutSec 10 }
    catch { $resp = $null }
    $live = if ($resp) { $resp.commit } else { $null }

    if ($live -and $live.Substring(0, [Math]::Min(7, $live.Length)) -eq $target.Substring(0, 7)) {
        Write-Host "Live commit is $($live.Substring(0,7)) - the deploy landed." -ForegroundColor Green
        $landed = $true; break
    }
    if ($live -eq 'unknown' -and $null -ne $resp.PSObject.Properties['write_auth_enabled']) {
        Write-Host "New build is live (commit reads 'unknown' - expected once, when the deploy" -ForegroundColor Green
        Write-Host "changed deploy.sh itself; the next deploy stamps the real sha)." -ForegroundColor Green
        $landed = $true; break
    }

    Write-Host ("  still {0}, waiting..." -f $(if ($live) { $live } else { 'old build' }))
    Start-Sleep -Seconds 20
}

if (-not $landed) {
    Warn "The new build never appeared at $Health."
    Warn 'Check /root/auto-deploy.log - a failed health check rolls back automatically,'
    Warn 'which looks identical to "still waiting" from out here.'
    Die  'NOT setting the API token: the frontend carrying the lock button is not live yet.'
}

# --- step 2: write auth ----------------------------------------------------
Say 'Step 2/3 - turn on write auth'

ssh -i $SshKey $Remote "grep -qs '^API_ADMIN_TOKEN=.\+' '$EnvPath'"
if ($LASTEXITCODE -eq 0) {
    Write-Host 'API_ADMIN_TOKEN is already set on the VPS. Nothing to do.'
} else {
    Write-Host 'Right now anyone who can reach the host can change the base currency, edit the'
    Write-Host 'watchlist, and start syncs that spend the IBKR and Yahoo budgets.'
    if (Ask 'Generate a token and install it?') {
        # URL-safe base64, equivalent to Python's secrets.token_urlsafe(32). Works on
        # both Windows PowerShell 5.1 and PowerShell 7.
        $bytes = New-Object byte[] 32
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')

        # Written straight over ssh; never into a file in this repo, which is public.
        #
        # `up -d`, NOT `restart`: compose reads env_file when it *creates* a container, so
        # `restart` reuses the existing one with its original environment and the new token
        # is silently ignored - the script then reports success over a site whose write API
        # is still wide open. `up -d` notices the changed config and recreates.
        # Cost us a confused ten minutes on 2026-07-31.
        $cmd = "printf '\nAPI_ADMIN_TOKEN=%s\n' '$token' >> '$EnvPath' && cd /root/IBKR_investment_tracker/backend && docker compose up -d"
        ssh -i $SshKey $Remote $cmd | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 'Failed to install the token.' }

        Write-Host ''
        Write-Host '  Token (shown once - paste it into the lock button beside Sync):' -ForegroundColor Cyan
        Write-Host "      $token" -ForegroundColor Cyan
        Write-Host ''

        # Confirm rather than assume: /health reports the flag the middleware actually read.
        Start-Sleep -Seconds 15
        try { $ok = (Invoke-RestMethod -Uri $Health -TimeoutSec 10).write_auth_enabled } catch { $ok = $false }
        if ($ok) {
            Write-Host 'Confirmed: /health reports write_auth_enabled=true.' -ForegroundColor Green
            Write-Host 'Hard-refresh the site (Ctrl+Shift+R) and the lock icon appears beside Sync.'
        } else {
            Warn '/health still reports write_auth_enabled=false - the container did not pick up'
            Warn 'the token. Re-run:  docker compose up -d  in /root/IBKR_investment_tracker/backend'
        }
    } else {
        Warn "Skipped. The footer will keep reporting 'write API unauthenticated'."
    }
}

# --- step 3: the guarded deploy script -------------------------------------
Say 'Step 3/3 - install the sync-slot-guarded auto-deploy script'
if (Ask 'Install ops/auto-deploy.sh to /root/auto-deploy.sh?') {
    scp -i $SshKey ops/auto-deploy.sh "${Remote}:/tmp/auto-deploy.sh"
    if ($LASTEXITCODE -ne 0) { Die 'scp failed.' }
    ssh -i $SshKey $Remote 'cp /tmp/auto-deploy.sh /root/auto-deploy.sh.new && chmod 755 /root/auto-deploy.sh.new && mv -f /root/auto-deploy.sh.new /root/auto-deploy.sh && rm -f /tmp/auto-deploy.sh'
    if ($LASTEXITCODE -ne 0) { Die 'install failed.' }
    Write-Host "Installed. Watch /root/auto-deploy.log for a 'SKIP: within 10min' line near a slot."
} else {
    Warn 'Skipped. Deploys can still land inside a sync slot; the job store recovers a'
    Warn 'miss under 30 minutes, so this is belt-and-braces rather than the only defence.'
}

Say 'Done'
try { Invoke-RestMethod -Uri $Health -TimeoutSec 10 | ConvertTo-Json -Compress } catch { Warn 'Health check unreachable.' }
