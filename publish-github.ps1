# Creates GitHub repo if missing, then pushes. First run opens browser for GitHub login.

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RepoRoot

$gh = "${env:ProgramFiles}\GitHub CLI\gh.exe"
if (-not (Test-Path -LiteralPath $gh)) {
    Write-Host "Install GitHub CLI: winget install GitHub.cli"
    exit 1
}

$null = & $gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "GitHub login required (once). Starting auth..."
    & $gh auth login -h github.com -p https -w
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Auth failed. Run manually: `"$gh`" auth login -h github.com -p https -w"
        exit 1
    }
}

& $gh auth setup-git

$login = (& $gh api user -q .login).Trim()
if (-not $login) {
    Write-Host "Could not read GitHub login after auth."
    exit 1
}

$repoName = "KB_TG_nedvizhka_tg"
$originUrl = "https://github.com/$login/$repoName.git"

if (git remote get-url origin 2>$null) {
    git remote set-url origin $originUrl
} else {
    git remote add origin $originUrl
}

$viewErr = & $gh repo view "$login/$repoName" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating repo $login/$repoName on GitHub..."
    & $gh repo create $repoName --public --description "KB Telegram bot (nedvizhka)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host $viewErr
        exit 1
    }
} else {
    Write-Host "Repo exists, pushing only."
}

$branch = (git branch --show-current).Trim()
if (-not $branch) {
    Write-Host "Could not detect current branch."
    exit 1
}

Write-Host "Pushing branch $branch..."
git push -u origin $branch
if ($LASTEXITCODE -ne 0) {
    exit 1
}

Write-Host "Done: https://github.com/$login/$repoName"
