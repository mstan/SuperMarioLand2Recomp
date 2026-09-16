param(
    [ValidateSet('fit', '16:9', '21:9', '32:9', 'off')][string]$Aspect,
    # Seed the DX color toggle for this launch. Omitted = whatever sml2-mods.ini
    # remembers; the Mods page is authoritative either way.
    [switch]$DX,
    [switch]$Faithful,
    # Skip the second gbrecomp run, producing an ordinary single-body build.
    [switch]$NoDX,
    [switch]$Build
)
$ErrorActionPreference = 'Stop'
if ($DX -and $Faithful) { throw 'Pass -DX or -Faithful, not both' }
$gameRoot = $PSScriptRoot
$engineRoot = Join-Path $gameRoot 'gb-recompiled'
$uiRoot = Join-Path $gameRoot 'recomp-ui'
$toolRoot = 'C:\msys64\mingw64\bin'
$originalPath = $env:PATH
$originalAspect = $env:SML2_WIDESCREEN
$originalDX = $env:SML2_DX
try {
    # devkitPro ships its own cmake/gcc that wins in PATH but cannot find the
    # mingw64 toolchain; put msys2 first and call its executables directly.
    $env:PATH = "$toolRoot;C:\Windows\System32;C:\Windows;$originalPath"
    Push-Location -LiteralPath $gameRoot
    if ($Build) {
        if (!(Test-Path -LiteralPath "$engineRoot/runtime/include/gb_body.h")) {
            throw 'Initialize the pinned engine first: git submodule update --init gb-recompiled'
        }
        if (!(Test-Path -LiteralPath "$uiRoot/src/recomp_launcher.h")) {
            throw 'Initialize the pinned UI first: git submodule update --init recomp-ui'
        }
        & "$toolRoot\cmake.exe" -G Ninja -S $engineRoot -B "$engineRoot/build" `
            "-DCMAKE_C_COMPILER=$toolRoot/gcc.exe" "-DCMAKE_CXX_COMPILER=$toolRoot/g++.exe" `
            "-DCMAKE_MAKE_PROGRAM=$toolRoot/ninja.exe" -DGBRECOMP_RECOMP_UI=ON `
            -DRECOMP_UI_ENABLE_MODS=ON "-DRECOMP_UI_ROOT=$uiRoot"
        if ($LASTEXITCODE) { throw 'Engine configuration failed' }
        & "$toolRoot\ninja.exe" -C "$engineRoot/build" gbrecomp
        if ($LASTEXITCODE) { throw 'Recompiler build failed' }
        # Body 1: the faithful V1.0 build. This tree is the CMake project.
        & "$engineRoot/build/bin/gbrecomp.exe" --config super_mario_land_2.toml
        if ($LASTEXITCODE) { throw 'Generation failed (faithful body)' }
        # Body 2: the same ROM plus recomp/patches/sml2dx_v181.bps, recompiled
        # with [options] body_only so it links into the same executable. See DX.md.
        if ($NoDX) {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $gameRoot 'generated_dx')
        } else {
            & "$engineRoot/build/bin/gbrecomp.exe" --config super_mario_land_2_dx.toml
            if ($LASTEXITCODE) { throw 'Generation failed (DX body)' }
        }
        & "$toolRoot\cmake.exe" -G Ninja -S generated -B generated/build `
            "-DCMAKE_C_COMPILER=$toolRoot/gcc.exe" "-DCMAKE_CXX_COMPILER=$toolRoot/g++.exe" `
            "-DCMAKE_MAKE_PROGRAM=$toolRoot/ninja.exe" -DGBRECOMP_RECOMP_UI=ON `
            -DRECOMP_UI_ENABLE_MODS=ON -DGBRECOMP_LAUNCHER_CONSOLE=gb "-DRECOMP_UI_ROOT=$uiRoot"
        if ($LASTEXITCODE) { throw 'Game configuration failed' }
        & "$toolRoot\ninja.exe" -C generated/build
        if ($LASTEXITCODE) { throw 'Game build failed' }
    }
    # An explicit -Aspect / -DX only seeds the launcher controls; an ordinary
    # launch keeps whatever is saved in sml2-mods.ini, and the Mods page wins
    # either way.
    if ($PSBoundParameters.ContainsKey('Aspect')) { $env:SML2_WIDESCREEN = $Aspect }
    if ($DX)       { $env:SML2_DX = '1' }
    if ($Faithful) { $env:SML2_DX = '0' }
    # One supported ROM: (UE) V1.0, CRC32 D5EC24E4. The DX body derives its own
    # image from this file in memory at boot — never ask the user for the hack.
    $romPath = Join-Path $gameRoot 'roms/Super Mario Land 2 - 6 Golden Coins (UE) (V1.0) [!].gb'
    if (!(Test-Path -LiteralPath $romPath -PathType Leaf)) { throw "ROM missing: $romPath" }
    $exe = Join-Path $gameRoot 'generated/build/Super_Mario_Land_2.exe'
    if (!(Test-Path -LiteralPath $exe -PathType Leaf)) { throw 'Build it first with -Build' }
    [System.IO.File]::WriteAllText((Join-Path $gameRoot 'generated/build/rom.cfg'), $romPath)
    & $exe
} finally {
    Pop-Location
    $env:PATH = $originalPath
    $env:SML2_WIDESCREEN = $originalAspect
    $env:SML2_DX = $originalDX
}
