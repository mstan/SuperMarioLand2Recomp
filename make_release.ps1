<#
make_release.ps1 — build and package the Super Mario Land 2 Windows x64 release.

Produces exactly one asset:

    release-stage\SuperMarioLand2Recomp-windows-x64-v<Version>.zip

Never a bare Super_Mario_Land_2.exe. The executable is useless on its own: it
needs the mingw-w64 runtime DLLs (libEGL.dll is dlopen'ed by SDL/ANGLE, so it
is invisible to `ldd`), the recomp-ui launcher `assets/` tree, and
sml2dx_v181.bps for the DX colour body.

**No ROM, and nothing derived from one, is ever staged.** The zip carries a
`roms/README.md` telling the player which cart to supply. The staged tree is
scanned for ROM-shaped files and for the supported ROM's CRC32 before the
archive is written, and packaging fails if either turns up.

Unlike the SNES equivalents (snesrecomp's tools/make_release.ps1) this script
DOES build, because the two-body generation step is easy to forget and a zip
built from a stale generated_dx/ silently ships a mismatched DX body. The build
is the same sequence as `Launch.ps1 -Build` — two gbrecomp runs, then cmake +
ninja — with release build directories of its own so a packaging run never
disturbs a developer's `gb-recompiled/build` or `generated/build`.

What the script verifies before it will write a zip:

  * the ROM used for generation is the one supported cart (CRC32 D5EC24E4);
  * the built executable has the expected CRC baked in (little-endian search,
    the check from Tetris/README.md);
  * a copy of the staged tree starts from a CLEAN PATH — only System32, so a
    missing DLL cannot be papered over by msys2/devkitPro being on the
    developer's PATH — runs headless, answers a `ping` on the debug port, and
    then exits 0 under a frame limit;
  * every ZIP entry name is portable ('/'), re-read from the finished archive.

Usage:
    powershell -File make_release.ps1                    # version from VERSION
    powershell -File make_release.ps1 -Version 0.2.0
    powershell -File make_release.ps1 -SkipBuild         # package what is built
#>
param(
    # Defaults to the contents of the VERSION file at the repo root.
    [string]$Version,
    # Skip the build and package whatever is already in -GameBuildDir.
    [switch]$SkipBuild,
    # Skip the clean-PATH headless start check (not recommended; the zip is
    # still written, so only use this when the machine cannot run the game).
    [switch]$SkipVerify,
    [string]$ToolRoot = 'C:\msys64\mingw64\bin',
    # Release build trees, deliberately distinct from Launch.ps1's so a
    # packaging run cannot invalidate an in-progress development build.
    [string]$EngineBuildDir = 'build-release-engine',
    [string]$GameBuildDir = 'generated/build-release',
    [ValidateSet('Release', 'MinSizeRel', 'RelWithDebInfo')][string]$BuildType = 'Release',
    # Optimization level for the generated ROM bodies. The generated project
    # defaults to 1; that is the profile every playtest of this game has used,
    # so it is what ships unless a release deliberately opts up.
    [ValidateSet('0', '1', '2', '3')][string]$GeneratedOptLevel = '1',
    [int]$Jobs = 0
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$exeName = 'Super_Mario_Land_2.exe'
$romName = 'Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb'
# PowerShell 5.1 parses a 32-bit hex literal with the high bit set as a
# NEGATIVE Int32 (0xD5EC24E4 -> -706923292), so build the value explicitly.
$expectedCrc = [Convert]::ToUInt32('D5EC24E4', 16)
$releaseSlug = 'SuperMarioLand2Recomp'

# The list is the engine's (GBRECOMP_STAGE_RUNTIME_DLLS in c_emitter.cpp) and
# README.md's. Kept here as well so packaging fails loudly if the build stopped
# staging one, rather than shipping a zip that dies with "entry point not
# found" on a machine without msys2.
$runtimeDlls = @(
    'SDL2.dll',
    'libEGL.dll',
    'libGLESv2.dll',
    'zlib1.dll',
    'libgcc_s_seh-1.dll',
    'libstdc++-6.dll',
    'libwinpthread-1.dll'
)

function Resolve-Version {
    param([string]$Requested)
    if ($Requested) { return $Requested.TrimStart('v') }
    $versionFile = Join-Path $root 'VERSION'
    if (-not (Test-Path -LiteralPath $versionFile)) {
        throw "No -Version given and no VERSION file at $versionFile"
    }
    $v = (Get-Content -LiteralPath $versionFile -TotalCount 1).Trim().TrimStart('v')
    if (-not $v) { throw "VERSION file is empty: $versionFile" }
    return $v
}

function Get-Crc32 {
    param([string]$Path)
    $crcPoly = [Convert]::ToUInt32('EDB88320', 16)
    $crcInit = [Convert]::ToUInt32('FFFFFFFF', 16)
    $table = New-Object 'uint32[]' 256
    for ($i = 0; $i -lt 256; $i++) {
        $c = [uint32]$i
        for ($j = 0; $j -lt 8; $j++) {
            if ($c -band 1) { $c = [uint32]($crcPoly -bxor ($c -shr 1)) }
            else { $c = [uint32]($c -shr 1) }
        }
        $table[$i] = $c
    }
    $crc = $crcInit
    $stream = [IO.File]::OpenRead($Path)
    try {
        $buffer = New-Object byte[] 65536
        while (($read = $stream.Read($buffer, 0, $buffer.Length)) -gt 0) {
            for ($i = 0; $i -lt $read; $i++) {
                $crc = [uint32]($table[(($crc -bxor $buffer[$i]) -band 0xFF)] -bxor ($crc -shr 8))
            }
        }
    } finally { $stream.Dispose() }
    return [uint32]($crc -bxor $crcInit)
}

function Invoke-Step {
    param([string]$Label, [string]$Exe, [string[]]$Arguments)
    Write-Host "[build] $Label"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

$Version = Resolve-Version $Version
if ($Version -notmatch '^\d+\.\d+\.\d+(-[0-9A-Za-z.\-]+)?$') {
    throw "Version '$Version' is not semver (expected e.g. 0.1.0)"
}
if ($Jobs -le 0) { $Jobs = [Environment]::ProcessorCount }

Write-Host "=== $releaseSlug v$Version (windows-x64) ==="

# ── preflight ────────────────────────────────────────────────────────────────
$engineRoot = Join-Path $root 'gb-recompiled'
$uiRoot = Join-Path $root 'recomp-ui'
if (-not (Test-Path -LiteralPath (Join-Path $engineRoot 'runtime/include/gb_body.h'))) {
    throw 'gb-recompiled is not initialized: git submodule update --init gb-recompiled'
}
if (-not (Test-Path -LiteralPath (Join-Path $uiRoot 'src/recomp_launcher.h'))) {
    throw 'recomp-ui is not initialized: git submodule update --init recomp-ui'
}
$romPath = Join-Path $root "roms/$romName"
if (-not $SkipBuild) {
    if (-not (Test-Path -LiteralPath $romPath -PathType Leaf)) {
        throw "Generation needs the supported cart at roms\$romName (it is never packaged)"
    }
    $romCrc = Get-Crc32 $romPath
    if ($romCrc -ne $expectedCrc) {
        throw ("roms\$romName has CRC32 0x{0:X8}; this project only supports 0x{1:X8}" -f $romCrc, $expectedCrc)
    }
    Write-Host ("[preflight] ROM CRC32 0x{0:X8} OK" -f $romCrc)
}

$engineBuild = Join-Path $root $EngineBuildDir
$gameBuild = Join-Path $root $GameBuildDir

# ── build (same sequence as Launch.ps1 -Build) ───────────────────────────────
if (-not $SkipBuild) {
    $originalPath = $env:PATH
    try {
        # devkitPro ships its own cmake/gcc that wins in PATH but cannot find
        # the mingw64 toolchain; put msys2 first and call its exes directly.
        $env:PATH = "$ToolRoot;$env:SystemRoot\System32;$env:SystemRoot;$originalPath"
        $cmake = Join-Path $ToolRoot 'cmake.exe'
        $ninja = Join-Path $ToolRoot 'ninja.exe'
        foreach ($tool in @($cmake, $ninja)) {
            if (-not (Test-Path -LiteralPath $tool)) { throw "Toolchain missing: $tool" }
        }

        Invoke-Step 'configure recompiler' $cmake @(
            '-G', 'Ninja', '-S', $engineRoot, '-B', $engineBuild,
            "-DCMAKE_C_COMPILER=$ToolRoot/gcc.exe",
            "-DCMAKE_CXX_COMPILER=$ToolRoot/g++.exe",
            "-DCMAKE_MAKE_PROGRAM=$ToolRoot/ninja.exe",
            '-DGBRECOMP_RECOMP_UI=ON', '-DRECOMP_UI_ENABLE_MODS=ON',
            "-DRECOMP_UI_ROOT=$uiRoot")
        Invoke-Step 'build recompiler' $ninja @('-C', $engineBuild, 'gbrecomp')

        $gbrecomp = Join-Path $engineBuild 'bin/gbrecomp.exe'
        Push-Location -LiteralPath $root
        try {
            # Body 1: the faithful V1.0 build; this tree is the CMake project.
            Invoke-Step 'generate faithful body' $gbrecomp @('--config', 'super_mario_land_2.toml')
            # Body 2: V1.0 + recomp/patches/sml2dx_v181.bps, [options] body_only
            # so it links into the same executable. See DX.md.
            Invoke-Step 'generate DX body' $gbrecomp @('--config', 'super_mario_land_2_dx.toml')
        } finally { Pop-Location }

        Invoke-Step 'configure game' $cmake @(
            '-G', 'Ninja', '-S', (Join-Path $root 'generated'), '-B', $gameBuild,
            "-DCMAKE_C_COMPILER=$ToolRoot/gcc.exe",
            "-DCMAKE_CXX_COMPILER=$ToolRoot/g++.exe",
            "-DCMAKE_MAKE_PROGRAM=$ToolRoot/ninja.exe",
            "-DCMAKE_BUILD_TYPE=$BuildType",
            "-DGBRECOMP_GENERATED_OPT_LEVEL=$GeneratedOptLevel",
            '-DGBRECOMP_RECOMP_UI=ON', '-DRECOMP_UI_ENABLE_MODS=ON',
            '-DGBRECOMP_LAUNCHER_CONSOLE=gb',
            "-DRECOMP_UI_ROOT=$uiRoot")
        Invoke-Step 'build game' $ninja @('-C', $gameBuild, "-j$Jobs")
    } finally {
        $env:PATH = $originalPath
    }
}

# ── stage ────────────────────────────────────────────────────────────────────
$exe = Join-Path $gameBuild $exeName
$assets = Join-Path $gameBuild 'assets'
if (-not (Test-Path -LiteralPath $exe)) { throw "Release executable missing: $exe" }
if (-not (Test-Path -LiteralPath $assets)) { throw "recomp-ui launcher assets/ missing: $assets" }

# The CRC gate the launcher enforces is compiled into the binary from
# extras.c:game_get_expected_crc32(). A build generated from a different cart
# would not carry it — refuse to ship one. (Tetris/README.md documents the same
# little-endian search as the release check.)
$crcBytes = [BitConverter]::GetBytes([uint32]$expectedCrc)
$exeBytes = [IO.File]::ReadAllBytes($exe)
$crcFound = $false
for ($i = 0; $i -le $exeBytes.Length - 4; $i++) {
    if ($exeBytes[$i] -eq $crcBytes[0] -and $exeBytes[$i + 1] -eq $crcBytes[1] -and
        $exeBytes[$i + 2] -eq $crcBytes[2] -and $exeBytes[$i + 3] -eq $crcBytes[3]) {
        $crcFound = $true
        Write-Host ("[verify] baked CRC 0x{0:X8} found at exe offset 0x{1:X}" -f $expectedCrc, $i)
        break
    }
}
$exeBytes = $null
if (-not $crcFound) {
    throw ("Executable does not carry the expected ROM CRC 0x{0:X8}; it was not built from this project's cart." -f $expectedCrc)
}

$out = Join-Path $root 'release-stage'
$stageName = "$releaseSlug-windows-x64-v$Version"
$stage = Join-Path $out $stageName
$zip = Join-Path $out "$stageName.zip"

$outFull = [IO.Path]::GetFullPath($out).TrimEnd('\') + '\'
$stageFull = [IO.Path]::GetFullPath($stage)
$zipFull = [IO.Path]::GetFullPath($zip)
if (-not $stageFull.StartsWith($outFull, [StringComparison]::OrdinalIgnoreCase) -or
    -not $zipFull.StartsWith($outFull, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to clean release paths outside release-stage.'
}
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Copy-Item -LiteralPath $exe -Destination $stage
Copy-Item -LiteralPath $assets -Destination $stage -Recurse

foreach ($dll in $runtimeDlls) {
    $source = Join-Path $gameBuild $dll
    if (-not (Test-Path -LiteralPath $source)) { $source = Join-Path $ToolRoot $dll }
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Required runtime DLL missing from the build and from ${ToolRoot}: $dll"
    }
    Copy-Item -LiteralPath $source -Destination $stage
}

# DX colour body payload: the BPS patch is applied to the player's own V1.0
# ROM in memory at boot, so the patch ships and the hack never does.
foreach ($payload in @('sml2dx_v181.bps', 'SML2DX_readme.txt')) {
    $source = Join-Path $gameBuild $payload
    if (-not (Test-Path -LiteralPath $source)) {
        $source = Join-Path $root "recomp/patches/$payload"
    }
    if (-not (Test-Path -LiteralPath $source)) { throw "DX payload missing: $payload" }
    Copy-Item -LiteralPath $source -Destination $stage
}

# Docs the player is pointed at from the launcher and the README.
Copy-Item -LiteralPath (Join-Path $root 'README.md') -Destination $stage
foreach ($doc in @('DX.md', 'ADAPTIVE.md')) {
    $source = Join-Path $root $doc
    if (Test-Path -LiteralPath $source) { Copy-Item -LiteralPath $source -Destination $stage }
}
$license = Get-ChildItem -LiteralPath $root -File |
    Where-Object { $_.Name -match '^(LICENSE|COPYING)(\..*)?$' } | Select-Object -First 1
if ($license) {
    Copy-Item -LiteralPath $license.FullName -Destination $stage
} else {
    Write-Warning 'No LICENSE/COPYING file in the repo root — the zip ships without one.'
}

# roms/ placeholder. This directory exists so the player has an obvious place
# to put the cart; it must never contain one when we build the archive.
New-Item -ItemType Directory -Path (Join-Path $stage 'roms') -Force | Out-Null
$romsReadme = @"
# Put your Super Mario Land 2 ROM here

This release does **not** include a ROM, and never will. Supply your own dump
of the cart you own:

    Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb
    CRC32  D5EC24E4
    size   524288 bytes (512 KiB)

That is the only cart this build accepts. The launcher checks the CRC32 and
refuses anything else, including the V1.2 revision.

You do not need a Super Mario Land 2 DX ROM. Turning **DX colour** on in the
launcher's Mods page applies ``sml2dx_v181.bps`` to your V1.0 ROM in memory at
boot; nothing is written to disk and the patched image is never distributed.

Drop the file in this folder (or anywhere else) and pick it in the launcher —
the chosen path is remembered in ``rom.cfg`` next to the executable.
"@
Set-Content -LiteralPath (Join-Path $stage 'roms/README.md') -Value $romsReadme -Encoding ascii

# ── refuse to ship a ROM ─────────────────────────────────────────────────────
# Both halves matter: the extension check catches an obvious mistake, the CRC
# check catches a ROM renamed to something innocuous.
$romLike = Get-ChildItem -LiteralPath $stage -File -Recurse |
    Where-Object { $_.Extension -in '.gb', '.gbc', '.sgb', '.gbs', '.sfc', '.smc', '.nes', '.z64' }
if ($romLike) {
    throw "ROM-shaped files in the release stage: $(($romLike | ForEach-Object Name) -join ', ')"
}
foreach ($candidate in Get-ChildItem -LiteralPath $stage -File -Recurse) {
    if ($candidate.Length -lt 32KB -or $candidate.Length -gt 8MB) { continue }
    if ((Get-Crc32 $candidate.FullName) -eq $expectedCrc) {
        throw "A copy of the supported ROM is in the release stage: $($candidate.FullName)"
    }
}

# ── clean-PATH headless verification ─────────────────────────────────────────
# Run a COPY of the stage so the archive is built from a pristine tree: the
# game writes rom.cfg, keybinds.ini, runtime_prefs.ini and a .sav next to
# itself, none of which belong in a release.
if (-not $SkipVerify) {
    if (-not (Test-Path -LiteralPath $romPath -PathType Leaf)) {
        throw "Verification needs the supported cart at roms\$romName (use -SkipVerify to skip)"
    }
    $verify = Join-Path ([IO.Path]::GetTempPath()) ("sml2_release_verify_" + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $verify -Force | Out-Null
    try {
        Get-ChildItem -LiteralPath $stage -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $verify -Recurse
        }
        Set-Content -LiteralPath (Join-Path $verify 'rom.cfg') -Value $romPath -Encoding ascii

        # PATH is System32 only. Anything the exe needs must be in the stage.
        $cleanPath = "$env:SystemRoot\System32;$env:SystemRoot"
        $port = 4370 + (Get-Random -Minimum 11 -Maximum 400)

        function Start-Staged {
            param([string]$Arguments, [int]$Port)
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = Join-Path $verify $exeName
            $psi.Arguments = $Arguments
            $psi.WorkingDirectory = $verify
            $psi.UseShellExecute = $false
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.EnvironmentVariables['PATH'] = $cleanPath
            $psi.EnvironmentVariables['GBRECOMP_NO_LAUNCHER'] = '1'
            $psi.EnvironmentVariables['GBRECOMP_DEBUG_PORT'] = "$Port"
            $psi.EnvironmentVariables['SDL_VIDEODRIVER'] = 'dummy'
            $psi.EnvironmentVariables['SDL_AUDIODRIVER'] = 'dummy'
            return [System.Diagnostics.Process]::Start($psi)
        }

        # 1. It starts, and its always-on debug server answers a ping. A frame
        #    number in the reply is proof the recompiled body is executing, not
        #    merely that the process survived loading.
        Write-Host "[verify] clean-PATH headless start + debug ping (port $port)"
        $proc = Start-Staged '--benchmark --limit-frames 2000000' $port
        $stdout = $proc.StandardOutput.ReadToEndAsync()
        $stderr = $proc.StandardError.ReadToEndAsync()
        $pong = $null
        $clock = [Diagnostics.Stopwatch]::StartNew()
        while ($clock.Elapsed.TotalSeconds -lt 60 -and -not $pong -and -not $proc.HasExited) {
            try {
                $client = New-Object System.Net.Sockets.TcpClient
                $client.Connect('127.0.0.1', $port)
                $netStream = $client.GetStream()
                $netStream.ReadTimeout = 5000
                $request = [Text.Encoding]::ASCII.GetBytes("{`"cmd`":`"ping`",`"id`":1}`n")
                $netStream.Write($request, 0, $request.Length)
                $netStream.Flush()
                $reply = New-Object byte[] 512
                $n = $netStream.Read($reply, 0, $reply.Length)
                if ($n -gt 0) { $pong = [Text.Encoding]::ASCII.GetString($reply, 0, $n) }
                $client.Close()
            } catch { Start-Sleep -Milliseconds 100 }
        }
        if (-not $proc.HasExited) { $proc.Kill(); $proc.WaitForExit(15000) | Out-Null }
        if (-not $pong) {
            $tail = ($stderr.Result -split "`n" | Select-Object -Last 20) -join "`n"
            throw "Staged build did not answer a debug ping from a clean PATH.`n$tail"
        }
        if ($pong -notmatch '"ok"\s*:\s*true') { throw "Unexpected debug reply: $pong" }
        if ($pong -notmatch '"frame"\s*:\s*([1-9][0-9]*)') {
            throw "Debug ping reports no frames executed: $pong"
        }
        Write-Host "[verify] ping OK: $($pong.Trim())  (frames advanced)"
        $null = $stdout.Result

        # 2. And it terminates cleanly under a frame limit (exit 0).
        Write-Host '[verify] clean-PATH headless run to frame limit'
        $proc = Start-Staged '--benchmark --limit-frames 600' ($port + 1)
        $stdout = $proc.StandardOutput.ReadToEndAsync()
        $stderr = $proc.StandardError.ReadToEndAsync()
        if (-not $proc.WaitForExit(120000)) {
            $proc.Kill()
            throw 'Staged build did not reach its frame limit within 120s.'
        }
        if ($proc.ExitCode -ne 0) {
            $tail = ($stderr.Result -split "`n" | Select-Object -Last 20) -join "`n"
            throw "Staged build exited $($proc.ExitCode) from a clean PATH.`n$tail"
        }
        if ($stdout.Result -notmatch '\[Launcher\] ROM:') {
            throw 'Staged build never reported resolving a ROM.'
        }
        Write-Host '[verify] headless run exited 0'
    } finally {
        Remove-Item -LiteralPath $verify -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Warning 'Clean-PATH verification skipped (-SkipVerify).'
}

# ── archive ──────────────────────────────────────────────────────────────────
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

# ZIP entry names always use '/', regardless of host OS. Compress-Archive keeps
# Windows backslashes, which POSIX extractors treat as literal filename
# characters — that is what stopped Linux/Steam Deck extractors from rebuilding
# the nested assets/ hierarchy, leaving the launcher with no fonts under Proton.
$stagePrefix = $stageFull.TrimEnd('\') + '\'
$files = @(Get-ChildItem -LiteralPath $stage -File -Recurse | Sort-Object FullName)
$archive = [IO.Compression.ZipFile]::Open($zipFull, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($file in $files) {
        $fileFull = [IO.Path]::GetFullPath($file.FullName)
        if (-not $fileFull.StartsWith($stagePrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to archive a file outside the release stage: $fileFull"
        }
        $entryName = $fileFull.Substring($stagePrefix.Length).Replace('\', '/')
        if ($entryName.StartsWith('/') -or $entryName -match '(^|/)\.\.(/|$)') {
            throw "Unsafe ZIP entry name: $entryName"
        }
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive, $fileFull, $entryName,
            [IO.Compression.CompressionLevel]::Optimal) | Out-Null
    }
} finally { $archive.Dispose() }

# Read the archive back so a regression in the writer cannot ship a zip that
# only extracts correctly on Windows.
$archive = [IO.Compression.ZipFile]::OpenRead($zipFull)
try {
    $bad = @($archive.Entries | Where-Object {
        $_.FullName.Contains('\') -or $_.FullName.StartsWith('/') -or
        $_.FullName -match '(^|/)\.\.(/|$)'
    })
    if ($bad.Count -ne 0) {
        throw "ZIP contains non-portable entry names: $(($bad | ForEach-Object FullName) -join ', ')"
    }
    if ($archive.Entries.Count -ne $files.Count) {
        throw "ZIP entry count mismatch: expected $($files.Count), got $($archive.Entries.Count)"
    }
    $romEntries = @($archive.Entries | Where-Object { $_.FullName -match '\.(gb|gbc|sgb|sfc|smc)$' })
    if ($romEntries.Count -ne 0) {
        throw "ZIP contains a ROM: $(($romEntries | ForEach-Object FullName) -join ', ')"
    }
} finally { $archive.Dispose() }

Write-Host ''
Write-Host "--- $stageName ---"
$archive = [IO.Compression.ZipFile]::OpenRead($zipFull)
try {
    $archive.Entries | Sort-Object FullName |
        Select-Object @{n = 'Entry'; e = { $_.FullName } }, Length |
        Format-Table -AutoSize | Out-Host
} finally { $archive.Dispose() }
Write-Host ("zip: $zipFull  ({0:N1} MiB)" -f ((Get-Item -LiteralPath $zipFull).Length / 1MB))
Get-FileHash -LiteralPath $zipFull -Algorithm SHA256 | Out-Host
