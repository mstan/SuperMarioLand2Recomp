target_sources(${GBRECOMP_GAME_TARGET} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/sml2_adaptive.c")
target_sources(${GBRECOMP_GAME_TARGET} PRIVATE "${CMAKE_CURRENT_LIST_DIR}/sml2_mods.c")
target_include_directories(${GBRECOMP_GAME_TARGET} PRIVATE "${RECOMP_UI_ROOT}/src")
if(GBRECOMP_RECOMP_UI AND NOT RECOMP_UI_ENABLE_MODS)
    message(FATAL_ERROR "Super Mario Land 2 requires its Mods UI: configure with -DRECOMP_UI_ENABLE_MODS=ON")
endif()

# ── Second recompiled body: Super Mario Land 2 DX v1.8.1 ─────────────────────
#
# generated/ (the project being configured) is the faithful V1.0 build and owns
# main(); generated_dx/ is the same cart plus recomp/patches/sml2dx_v181.bps,
# recompiled with [options] body_only so every one of its globals is namespaced
# behind "Super_Mario_Land_2_DX__". Its generated *_body.cmake is a plain source
# list, so the DX body compiles with exactly the toolchain, defines and include
# path the faithful body does. The launcher's DX color toggle picks between them
# at boot (extras.c game_select_body). See DX.md.
#
# Absent generated_dx/, this whole block is skipped and the executable is the
# ordinary single-body build — so `gbrecomp --config super_mario_land_2.toml`
# alone still produces a working game.
set(SML2_DX_BODY "${CMAKE_CURRENT_LIST_DIR}/generated_dx/Super_Mario_Land_2_DX_body.cmake")
if(EXISTS "${SML2_DX_BODY}")
    include("${SML2_DX_BODY}")
    target_sources(${GBRECOMP_GAME_TARGET} PRIVATE ${Super_Mario_Land_2_DX_BODY_SOURCES})
    target_include_directories(${GBRECOMP_GAME_TARGET} PRIVATE
        ${Super_Mario_Land_2_DX_BODY_INCLUDE_DIR})
    # Same optimization profile the primary body's generated sources get. The
    # generated CMakeLists sets that cache entry AFTER including this file, so
    # fall back to its default rather than emitting a bare "-O".
    set(_sml2_opt "${GBRECOMP_GENERATED_OPT_LEVEL}")
    if(NOT _sml2_opt)
        set(_sml2_opt "1")
    endif()
    set_source_files_properties(${Super_Mario_Land_2_DX_BODY_SOURCES} PROPERTIES
        COMPILE_OPTIONS "-O${_sml2_opt}")
    target_compile_definitions(${GBRECOMP_GAME_TARGET} PRIVATE SML2_HAVE_DX_BODY=1)
    message(STATUS "${GBRECOMP_GAME_TARGET}: linking the DX colour body from generated_dx/")
else()
    message(STATUS "${GBRECOMP_GAME_TARGET}: no generated_dx/ — faithful body only "
                   "(run gbrecomp --config super_mario_land_2_dx.toml to add DX colour)")
endif()

# The DX body derives its image from the player's ROM at boot, so the patch has
# to sit next to the executable. Staged, never embedded: this repo ships the
# patch, never the ROM and never the hack.
set(SML2_DX_PATCH "${CMAKE_CURRENT_LIST_DIR}/recomp/patches/sml2dx_v181.bps")
if(EXISTS "${SML2_DX_PATCH}")
    add_custom_command(TARGET ${GBRECOMP_GAME_TARGET} POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different
                "${SML2_DX_PATCH}" "$<TARGET_FILE_DIR:${GBRECOMP_GAME_TARGET}>/sml2dx_v181.bps"
        COMMENT "Staging sml2dx_v181.bps next to the executable"
        VERBATIM)
    set(SML2_DX_README "${CMAKE_CURRENT_LIST_DIR}/recomp/patches/SML2DX_readme.txt")
    if(EXISTS "${SML2_DX_README}")
        add_custom_command(TARGET ${GBRECOMP_GAME_TARGET} POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
                    "${SML2_DX_README}" "$<TARGET_FILE_DIR:${GBRECOMP_GAME_TARGET}>/SML2DX_readme.txt"
            VERBATIM)
    endif()
elseif(EXISTS "${SML2_DX_BODY}")
    message(WARNING "DX body is linked but recomp/patches/sml2dx_v181.bps is missing: "
                    "the launcher will report DX colour as unavailable")
endif()
