// ============================================================================
//  Application controller: protocol events + input events -> model + UI mode
//  -> responses. Owns the stale-press / duplicate-request protection.
// ============================================================================
#pragma once

#include "config.h"

void appSetup();
void appLoop();

// Diagnostic snapshot for the host's {"type":"debug"} query.
struct AppDebug {
    const char *mode;
    const char *state;
    const char *selectedId;
    uint8_t sessionCount;
    int8_t selectedIndex;
    bool pinned;
    bool statusValid;
    uint8_t shortcutCount;
};
void appDebugSnapshot(AppDebug &out);
