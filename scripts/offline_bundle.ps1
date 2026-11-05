<#
.SYNOPSIS
    Build an offline bundle for an AIR-GAPPED server.

.DESCRIPTION
    PowerShell port of scripts/offline_bundle.sh. Run on a machine WITH internet (Windows with
    Docker Desktop, or Linux/macOS with pwsh + docker), from the project root:

        .\scripts\offline_bundle.ps1 [out_dir]

    Produces (in out_dir): app_image.tar.gz, ollama_image.tar.gz, ollama_models.tgz,
    compose files, install_offline.sh, bundle.env, SHA256SUMS

    Model choice for the production servers (3 x 72 CPU cores / 786 GB RAM, no GPU): on CPU, speed is
    set by the parameters active per token, not by RAM. qwen3.5:35b-a3b is a mixture-of-experts model
    (35B total, ~3B active): about the speed of a 3-4B dense model with far better answers, 22 GB, and
    it can read images (used to lay out borderless PDF tables). qwen3.5:2b stays as the fast fallback
    selectable in the UI. Not chosen: qwen3.5:27b (dense, ~9x slower per token on CPU);
    qwen3.5:122b-a10b (no vision in Ollama, ~3x slower).

.PARAMETER OutDir
    Output folder (default: offline_bundle).

.PARAMETER AppOnly
    Only the AutoEDA image, for updates (Ollama + models are already on the server). Same as APP_ONLY=1.

.PARAMETER PipTimeout
    pip's network read timeout in seconds for the image build (default 15, pip's own default). Raise it
    on slow links or behind an HTTPS-inspecting proxy, e.g. -PipTimeout 1000 for the large PyTorch
    wheels from download-r2.pytorch.org. Same as PIP_TIMEOUT=<seconds>.

.PARAMETER Models
    Ollama models to bundle, as a list or one space/comma separated string, e.g.
    -Models qwen3.5:35b-a3b,qwen3.5:2b. Only these models are copied from the local Ollama store
    (OLLAMA_MODELS_DIR); the whole model folder is never bundled.

    Default: qwen3.5:35b-a3b, because docker-compose.prod.yml sets OLLAMA_MODEL=qwen3.5:35b-a3b.
    Add qwen3.5:2b to keep the small model selectable in the UI. Same as MODELS="...".
    Ignored with -AppOnly.

.EXAMPLE
    .\scripts\offline_bundle.ps1

.EXAMPLE
    .\scripts\offline_bundle.ps1 -PipTimeout 1000

.EXAMPLE
    .\scripts\offline_bundle.ps1 -Models qwen3.5:35b-a3b,qwen3.5:2b

.EXAMPLE
    $env:MODELS = "qwen3.5:35b-a3b"; .\scripts\offline_bundle.ps1
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$OutDir = "offline_bundle",

    [switch]$AppOnly,

    [ValidateRange(1, 86400)]
    [int]$PipTimeout = 0,

    [string[]]$Models = @()
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# ---------------------------------------------------------------------------------------------
# Configuration (environment variables mirror the Bash script's env overrides)
# ---------------------------------------------------------------------------------------------
function Get-EnvValue([string]$Name, [string]$Default) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrEmpty($v)) { return $Default }
    return $v
}

function Test-Truthy([string]$Value) { return ($Value -eq "1" -or $Value -match '^(?i)(true|yes|on)$') }

function ConvertTo-DockerPath([string]$Path) {
    # Docker Desktop runs the CLI against a Linux VM, so volumes need /d/data-profil style paths
    # (this is what the Bash script's `pwd -W` workaround produces)
    if ($Path -match '^[A-Za-z]:[\\/]') {
        $drive = $Path.Substring(0, 1).ToLowerInvariant()
        $rest = $Path.Substring(2) -replace '\\', '/'
        return "/$drive$rest"
    }
    return ($Path -replace '\\', '/')
}

# Run a native command. docker writes its progress (build steps, pull layers) on stderr, and Windows
# PowerShell 5.1 turns native stderr into error records that abort the script when
# $ErrorActionPreference = 'Stop' - even with `2>file`, because the redirection is implemented as a
# pipeline. So the child's handles are set up directly: stdout is inherited and stderr is read line by
# line and echoed live in grey, which also keeps long pip/apt steps visibly moving.
$script:StderrFile = Join-Path ([System.IO.Path]::GetTempPath()) ("autoeda_stderr_" + $PID + ".log")

function Start-Native([string]$Exe, [string[]]$Arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $lines = New-Object System.Collections.Generic.List[string]
    while ($null -ne ($line = $proc.StandardError.ReadLine())) {
        $lines.Add($line)
        if ($line -notmatch '^\s*$') { Write-Host "    $line" -ForegroundColor DarkGray }
    }
    $proc.WaitForExit()
    [System.IO.File]::WriteAllLines($script:StderrFile, $lines.ToArray(), (New-Object System.Text.UTF8Encoding($false)))
    return $proc.ExitCode
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
    # stdout is inherited (shown live), stderr is echoed live in grey - see Start-Native
    $code = Start-Native $Exe $Arguments
    if ($code -ne 0) { throw "Command failed (exit $code): $Exe $($Arguments -join ' ')" }
}

# Like Invoke-Native, but returns stdout lines (docker inspect etc.). Built on Start-Native for the
# same reason: no PowerShell redirection of native output.
function Invoke-NativeCapture([string]$Exe, [string[]]$Arguments) {
    $file = Join-Path ([System.IO.Path]::GetTempPath()) ("autoeda_out_" + $PID + ".log")
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = ($Arguments | ForEach-Object {
            if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
        }) -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        $proc.WaitForExit()
        if ($proc.ExitCode -ne 0) {
            throw "Command failed (exit $($proc.ExitCode)): $Exe $($Arguments -join ' ')`n$($errTask.Result)"
        }
        return , @($outTask.Result -split "`r?`n" | Where-Object { $_.Trim() -ne "" })
    }
    finally {
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    }
}

function Get-Sha256Hex([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Step([string]$Message) { Write-Host "==> $Message" }

function Format-Size([long]$Length) {
    if ($Length -ge 1GB) { "{0:N1} GB" -f ($Length / 1GB) }
    elseif ($Length -ge 1MB) { "{0:N1} MB" -f ($Length / 1MB) }
    elseif ($Length -ge 1KB) { "{0:N1} KB" -f ($Length / 1KB) }
    else { "$Length B" }
}

# docker save TAG | gzip > file.
# The image is streamed from `docker save` into .NET's GZipStream: raw byte copies only (no text
# re-encoding), and no gzip child process is needed. A gzip.exe found on PATH is a Git-Bash/Cygwin
# shim with a cmd wrapper, which cannot be started directly on Windows (this broke the first version
# with "The system cannot find the file specified").
function Save-DockerImageGzip([string]$Image, [string]$Destination) {
    $dockerExe = (Get-Command docker -CommandType Application | Select-Object -First 1).Source
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $dockerExe
    $psi.Arguments = "save $Image"
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $dockerProc = [System.Diagnostics.Process]::Start($psi)
    # read stderr concurrently: docker save only writes progress/errors there, and a full pipe
    # buffer would deadlock the copy of stdout below
    $errTask = $dockerProc.StandardError.ReadToEndAsync()

    $dest = [System.IO.File]::Create($Destination)
    $saveError = $null
    try {
        $gzs = New-Object System.IO.Compression.GZipStream(
            $dest, [System.IO.Compression.CompressionLevel]::Optimal, $true)
        try {
            $src = $dockerProc.StandardOutput.BaseStream
            $buf = New-Object byte[] (1MB)
            while (($n = $src.Read($buf, 0, $buf.Length)) -gt 0) { $gzs.Write($buf, 0, $n) }
        }
        finally {
            $gzs.Dispose()
        }
    }
    catch {
        $saveError = $_
    }
    finally {
        $dest.Dispose()
        # ExitCode must be read after WaitForExit() but BEFORE Dispose(): otherwise it is empty
        if (-not $dockerProc.HasExited) { $dockerProc.WaitForExit() }
        $exitCode = $dockerProc.ExitCode
        $stderr = $errTask.Result
        $dockerProc.Dispose()
    }
    if ($saveError) {
        # keep any partial file out of the bundle; a later run rewrites it completely
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        throw "docker save $Image failed while writing $Destination`: $($saveError.Exception.Message)"
    }
    if ($exitCode -ne 0) {
        Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
        Write-Host ($stderr.Trim()) -ForegroundColor DarkGray
        throw "docker save $Image failed (exit $exitCode)"
    }
    if (-not [string]::IsNullOrWhiteSpace($stderr)) { Write-Host ($stderr.Trim()) -ForegroundColor DarkGray }
}

# name[:tag] -> manifests/... path (same rule as the Bash script).
function Get-OllamaManifestPath([string]$Model) {
    $name = $Model
    $tag = "latest"
    if ($Model.Contains(":")) {
        $name = $Model.Substring(0, $Model.IndexOf(":"))
        $tag = $Model.Substring($Model.IndexOf(":") + 1)
    }
    if (-not $name.Contains("/")) { $name = "library/$name" }
    return "manifests/registry.ollama.ai/$name/$tag"
}

# The manifest is plain JSON: every "sha256:<hex>" digest in it (config + layers) is a blob file.
# Reading it here gives exactly what the Bash version's grep/sort/sed pipeline produces.
# Every entry is trimmed to a bare relative POSIX path: tar -T needs one LF-terminated name per line
# and fails with "Cannot stat ...\r" on any carriage return or surrounding space.
function Get-OllamaModelFiles([string]$ModelsPath) {
    $files = New-Object System.Collections.Generic.List[string]
    foreach ($m in $ModelList) {
        $man = Get-OllamaManifestPath $m
        $manPath = Join-Path $ModelsPath ($man -replace '/', [System.IO.Path]::DirectorySeparatorChar)
        if (-not (Test-Path -LiteralPath $manPath)) {
            throw "model $m is not in $ModelsPath (run: ollama pull $m)"
        }
        $files.Add($man)
        $text = Get-Content -LiteralPath $manPath -Raw -Encoding utf8
        $digests = [regex]::Matches($text, 'sha256:[0-9a-f]{64}') | ForEach-Object { $_.Value } | Sort-Object -Unique
        foreach ($d in $digests) { $files.Add("blobs/" + $d.Replace("sha256:", "sha256-")) }
    }
    # return only clean, non-empty relative paths
    $clean = foreach ($f in $files) {
        $v = ($f.Trim() -replace "`r", "" -replace "`n", "") -replace '"', ''
        if ($v) { $v }
    }
    return , @($clean)
}

# ---------------------------------------------------------------------------------------------
# Paths and settings
# ---------------------------------------------------------------------------------------------
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$OutPath = (Resolve-Path -LiteralPath $OutDir).ProviderPath
$OutDocker = ConvertTo-DockerPath $OutPath

$TAG            = Get-EnvValue "TAG" "autoeda:poc"
$OllamaImage    = Get-EnvValue "OLLAMA_IMAGE" "ollama/ollama:latest"
$PySparkVersion = Get-EnvValue "PYSPARK_VERSION" "3.5.3"
$WithDocling    = if (Test-Truthy (Get-EnvValue "WITH_DOCLING" "1")) { "1" } else { "0" }
# pip network read timeout for the build: -PipTimeout > $env:PIP_TIMEOUT > pip's default (15 s)
$PipTimeoutValue = if ($PipTimeout -gt 0) { $PipTimeout } else { [int](Get-EnvValue "PIP_TIMEOUT" "15") }
$ModelsDir      = [Environment]::GetEnvironmentVariable("OLLAMA_MODELS_DIR")
$AppOnlyMode    = ($AppOnly -or (Test-Truthy (Get-EnvValue "APP_ONLY" "0")))

# Ollama models to bundle: -Models (list or "a b,c") > $env:MODELS > the production default.
# Only these models are copied from the local Ollama store; the folder itself is never bundled.
#
# The *production* default is qwen3.5:35b-a3b because docker-compose.prod.yml sets
# OLLAMA_MODEL=qwen3.5:35b-a3b: bundling anything else leaves the server without the model the app
# asks for. The installer checks bundle.env against the compose file and warns/refuses on a mismatch.
$DefaultModels = @("qwen3.5:35b-a3b")

# A model name may now carry a registry namespace / tag with dots, e.g. "registry.local:5000/x/y:1.0".
$ModelNameRegex = '^(?:[A-Za-z0-9._-]+(?::[0-9]{1,5})?/)?[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*(?::[A-Za-z0-9._-]+)?$'

function Split-ModelList([string[]]$Items) {
    @($Items | ForEach-Object { $_ -split '[,\s]+' } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

if ($Models.Count -gt 0) {
    # -Models a,b  /  -Models "a,b"  /  -Models a,b,c  all arrive as one or more strings: split each
    $ModelSource = Split-ModelList $Models
    $ModelsOrigin = "-Models"
}
elseif (-not [string]::IsNullOrWhiteSpace((Get-EnvValue "MODELS" ""))) {
    $ModelSource = Split-ModelList @(Get-EnvValue "MODELS" "")
    $ModelsOrigin = '$env:MODELS'
}
else {
    $ModelSource = $DefaultModels
    $ModelsOrigin = "default"
}
$ModelList = @($ModelSource | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
if ($ModelList.Count -eq 0) { throw "No Ollama models selected. Pass -Models name[:tag] or set MODELS." }
foreach ($m in $ModelList) {
    if ($m -notmatch $ModelNameRegex) {
        throw "Invalid Ollama model name '$m' (expected name or name:tag, e.g. qwen3.5:2b)."
    }
}
$Models = $ModelList -join " "   # the single string written to bundle.env and shown to the user

# build for the production servers regardless of the build machine (override with DOCKER_PLATFORM)
$Platform = Get-EnvValue "DOCKER_PLATFORM" "linux/amd64"

if (-not (Get-Command docker -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "docker is not on PATH. Start Docker Desktop and run this from the project root."
}

# ---------------------------------------------------------------------------------------------
# Build and export the images
# ---------------------------------------------------------------------------------------------
# Built for Linux/amd64 (the production servers) whatever the build machine is. A Windows build
# context has no POSIX permission bits, so a tiny generated Dockerfile is used that runs the real
# build and then restores the executable bits on the two shell scripts in the image.
$dockerfile = Join-Path $OutPath ".build.Dockerfile"
$dockerfileLines = @(
    "# generated by scripts/offline_bundle.ps1 - do not edit",
    "FROM autoeda-build:local",
    "USER root",
    "RUN chmod +x /entrypoint.sh /app/scripts/*.sh 2>/dev/null || true",
    "USER app"
)
[System.IO.File]::WriteAllLines($dockerfile, $dockerfileLines, (New-Object System.Text.UTF8Encoding($false)))
# -f is resolved by the docker CLI on Windows, so it needs the native path (D:\...); the /d/...
# form is only for -v volume mounts.
$dockerfileArg = $dockerfile
$isWindowsHost = ($env:OS -eq "Windows_NT")
# one timestamp for the image's /health "build" field and bundle.env BUILT
$BuildStamp = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')

Step "Building $TAG for $Platform (Spark + OCR, Docling=$WithDocling, pip timeout ${PipTimeoutValue}s)"
try {
    # stage 1: the normal build from this project (cached as autoeda-build:local)
    Invoke-Native docker @(
        "buildx", "build",
        "--platform", $Platform,
        "--build-arg", "WITH_SPARK=1",
        "--build-arg", "WITH_OCR=1",
        "--build-arg", "WITH_DOCLING=$WithDocling",
        "--build-arg", "PYSPARK_VERSION=$PySparkVersion",
        "--build-arg", "PIP_TIMEOUT=$PipTimeoutValue",
        "--build-arg", "BUILD=$BuildStamp",
        "--load", "-t", "autoeda-build:local", "."
    )
    if ($isWindowsHost) {
        # stage 2: restore the executable bits NTFS cannot carry, then tag the result as $TAG
        Invoke-Native docker @(
            "buildx", "build",
            "--platform", $Platform,
            "--build-arg", "BUILD=$BuildStamp",
            "--load", "-t", $TAG, "-f", $dockerfileArg, "."
        )
    }
    else {
        # on Linux the build already produced the final image with correct modes
        Invoke-Native docker @("tag", "autoeda-build:local", $TAG)
    }
}
finally {
    Remove-Item -LiteralPath $dockerfile -Force -ErrorAction SilentlyContinue
}

# refuse to ship an image the production servers cannot run
$inspect = Invoke-NativeCapture docker @("image", "inspect", "--format", "{{.Id}}|{{.Os}}|{{.Architecture}}", $TAG)
$imageId, $imageOs, $imageArch = ($inspect[0] -split '\|')
if ($imageOs -ne "linux") {
    throw "The image was built for $imageOs/$imageArch, but the production servers run Linux. Check the docker builder."
}
Step "Built $TAG ($imageOs/$imageArch, id $($imageId.Substring(7, 12)))"

Step "Saving app image"
Save-DockerImageGzip -Image $TAG -Destination (Join-Path $OutPath "app_image.tar.gz")

if (-not $AppOnlyMode) {
    Step "Pulling and saving $OllamaImage ($Platform)"
    Invoke-Native docker @("pull", "--platform", $Platform, $OllamaImage)
    Save-DockerImageGzip -Image $OllamaImage -Destination (Join-Path $OutPath "ollama_image.tar.gz")

    Step "Collecting Ollama models: $Models"
    Write-Host "    models from: $ModelsOrigin" -ForegroundColor DarkGray
    if (-not [string]::IsNullOrEmpty($ModelsDir)) {
        $srcPath = (Resolve-Path -LiteralPath $ModelsDir).ProviderPath
        Write-Host "    source: $srcPath"
        # pull what is missing with the local Ollama (it trusts the OS certificate store, so it also
        # works behind HTTPS inspection), then bundle only the selected models, not the whole folder
        $haveOllama = [bool](Get-Command ollama -CommandType Application -ErrorAction SilentlyContinue)
        foreach ($m in $ModelList) {
            if ($haveOllama) {
                & ollama show $m *> $null
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "    pulling $m (first time only)"
                    Invoke-Native ollama @("pull", $m)
                }
            }
            $manifest = Join-Path $srcPath ((Get-OllamaManifestPath $m) -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $manifest)) {
                throw "model $m is not in $ModelsDir (run: ollama pull $m)"
            }
            Write-Host "    $m -> $(Get-OllamaManifestPath $m)"
        }

        $modelFiles = Get-OllamaModelFiles -ModelsPath $srcPath
        $blobBytes = 0
        foreach ($f in $modelFiles) {
            if ($f -notlike 'blobs/*') { continue }
            $blobPath = Join-Path $srcPath ($f -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            if (Test-Path -LiteralPath $blobPath) { $blobBytes += (Get-Item -LiteralPath $blobPath).Length }
            else { throw "blob for the selected models is missing: $f (run: ollama pull <model>)" }
        }
        Write-Host "    $($ModelList.Count) model(s), $($modelFiles.Count) files, $(Format-Size $blobBytes) of layers"

        $listLocal = Join-Path $OutPath ".model_files"
        # tar -T reads LF-terminated names: write them explicitly (no Environment.NewLine = CRLF)
        $listText = ($modelFiles -join "`n") + "`n"
        [System.IO.File]::WriteAllText($listLocal, $listText, (New-Object System.Text.UTF8Encoding($false)))
        $rawList = [System.IO.File]::ReadAllBytes($listLocal)
        if ($rawList -contains 13) {
            Remove-Item -LiteralPath $listLocal -Force -ErrorAction SilentlyContinue
            throw "Internal error: the model file list contains carriage returns (CR)."
        }
        try {
            Invoke-Native docker @(
                "run", "--rm", "--user", "root", "--entrypoint", "sh",
                "-v", "$(ConvertTo-DockerPath $srcPath):/src:ro",
                "-v", "${OutDocker}:/out",
                $TAG,
                "-c", "cd /src && tar czf /out/ollama_models.tgz --no-recursion -T /out/.model_files"
            )
        }
        finally {
            Remove-Item -LiteralPath $listLocal -Force -ErrorAction SilentlyContinue
        }
        $archive = Join-Path $OutPath "ollama_models.tgz"
        if (-not (Test-Path -LiteralPath $archive) -or (Get-Item -LiteralPath $archive).Length -le 100) {
            throw "ollama_models.tgz was not created properly (empty or missing) - check the model files in $srcPath"
        }
        Write-Host "    ollama_models.tgz: $(Format-Size (Get-Item -LiteralPath $archive).Length) (selected models only)"
    }
    else {
        # No local model folder: pull inside a throw-away Ollama container, then tar its volume
        & docker volume create autoeda_bundle_models *> $null
        & docker rm -f autoeda_bundle_ollama *> $null
        # extra root CAs from certs/ (HTTPS-inspecting proxy / antivirus) for the pull
        $certsDocker = ConvertTo-DockerPath (Resolve-Path -LiteralPath "certs").ProviderPath
        Invoke-Native docker @(
            "run", "-d", "--name", "autoeda_bundle_ollama",
            "-v", "autoeda_bundle_models:/root/.ollama",
            "-v", "${certsDocker}:/extra-certs:ro",
            "-e", "SSL_CERT_DIR=/etc/ssl/certs:/extra-certs",
            $OllamaImage
        )
        Start-Sleep -Seconds 3
        try {
            foreach ($m in $ModelList) {
                Invoke-Native docker @("exec", "autoeda_bundle_ollama", "ollama", "pull", $m)
            }
        }
        finally {
            & docker rm -f autoeda_bundle_ollama *> $null
        }
        Invoke-Native docker @(
            "run", "--rm", "--user", "root", "--entrypoint", "sh",
            "-v", "autoeda_bundle_models:/v:ro",
            "-v", "${OutDocker}:/out",
            $TAG,
            "-c", "tar czf /out/ollama_models.tgz -C /v/models ."
        )
        & docker volume rm autoeda_bundle_models *> $null
    }
}
else {
    foreach ($f in @("ollama_image.tar.gz", "ollama_models.tgz")) {
        $p = Join-Path $OutPath $f
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Force }
    }
    # An app-only bundle cannot start docker-compose.prod.yml on a server that has no Ollama image:
    # compose would try to pull ollama/ollama and the air-gapped server would time out.
    Write-Host "    note: app-only bundle - no Ollama image/models included."
    Write-Host "          Use it only where Ollama and the models are already installed; for a first"
    Write-Host "          install use a full bundle (run without -AppOnly)."
}

# ---------------------------------------------------------------------------------------------
# Bundle metadata (same contents as the Bash version)
# ---------------------------------------------------------------------------------------------
Copy-Item -LiteralPath "docker-compose.yml" -Destination $OutPath -Force
Copy-Item -LiteralPath "docker-compose.prod.yml" -Destination $OutPath -Force
Copy-Item -LiteralPath (Join-Path "scripts" "install_offline.sh") -Destination $OutPath -Force
# picks the compose file the running project was started with, for plain `docker compose`
Copy-Item -LiteralPath (Join-Path "scripts" "docker-compose") -Destination $OutPath -Force

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
# These two files are consumed by bash on Linux (`source ./bundle.env`, `sha256sum -c`), so they must
# use LF line endings. [System.IO.File]::WriteAllLines would write CRLF on Windows (Environment.NewLine),
# which makes sha256sum look for files literally named "./app_image.tar.gz\r".
function Write-UnixTextFile([string]$Path, [string[]]$Lines) {
    $content = if ($Lines.Count -gt 0) { ($Lines -join "`n") + "`n" } else { "" }
    [System.IO.File]::WriteAllText($Path, $content, $utf8NoBom)
}

$envLines = @(
    "TAG=$TAG",
    "OLLAMA_IMAGE=$OllamaImage",
    "MODELS=`"$Models`"",
    "PYSPARK_VERSION=$PySparkVersion",
    "BUILT=$BuildStamp",
    "APP_IMAGE_ID=$imageId"   # what install_offline.sh loaded: compare with /health on the server
)
Write-UnixTextFile -Path (Join-Path $OutPath "bundle.env") -Lines $envLines

# "hash  ./file" lines so `sha256sum -c SHA256SUMS` on the server accepts them
$sums = @(foreach ($f in (Get-ChildItem -LiteralPath $OutPath -File | Sort-Object Name)) {
        if ($f.Name -match '\.(tar\.gz|tgz)$') { "$(Get-Sha256Hex $f.FullName)  ./$($f.Name)" }
    })
Write-UnixTextFile -Path (Join-Path $OutPath "SHA256SUMS") -Lines $sums

Step "Bundle ready in ${OutDir}:"
Get-ChildItem -LiteralPath $OutPath | ForEach-Object {
    $len = if ($_.PSIsContainer) { "-" } else { Format-Size $_.Length }
    [pscustomobject]@{ Name = $_.Name; Size = $len }
} | Format-Table -AutoSize | Out-String | Write-Host
