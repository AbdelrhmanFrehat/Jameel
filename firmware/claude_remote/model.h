// ============================================================================
//  Application model: what the device knows about Claude and the current
//  request. Plain structs with fixed-size buffers (no String, no heap churn).
// ============================================================================
#pragma once

#include "config.h"

enum class UiMode : uint8_t {
    BOOT,
    DISCONNECTED,
    STATUS,        // idle / working / tool running / completed ... (from `state`)
    YES_NO,
    CHOICE,        // <=8 options direct, >8 paginated
    MULTI,         // toggle several options then confirm
    TEXT_INPUT,    // encoder composer
    PERMISSION,
    STOPPED,
    SENT,          // short confirmation overlay after answering
    SHORTCUTS,     // menu of fixed instructions to send to Claude
    TEST,          // hardware diagnostics
};

enum class RequestKind : uint8_t { NONE, QUESTION, PERMISSION, STOP };

// One live Claude Code session as reported by the bridge (`state.sessions`).
struct SessionEntry {
    char id[9];                         // first 8 chars of the session id
    char project[MAX_PROJECT_BYTES];
    char state[20];
    char title[MAX_TITLE_BYTES];
    char detail[MAX_DETAIL_BYTES];
    char tool[MAX_TOOL_BYTES];
    uint32_t elapsedBase;
};

struct ClaudeStatus {
    char state[20];
    char title[MAX_TITLE_BYTES];
    char detail[MAX_DETAIL_BYTES];
    char project[MAX_PROJECT_BYTES];
    char session[9];
    char tool[MAX_TOOL_BYTES];
    uint32_t elapsedBase;   // seconds reported by the host
    uint32_t elapsedAt;     // millis() when elapsedBase was received
    bool valid;
    SessionEntry sessions[MAX_SESSIONS];
    uint8_t sessionCount;
    int8_t selectedIndex;   // index into sessions[] shown on the status screen (-1 = none)
    bool pinned;            // chosen with the encoder (host stops auto-following)
};

struct RemoteQuestion {
    char requestId[MAX_REQUEST_ID + 1];
    char kind[8];                       // choice | yes_no | text | multi
    char header[MAX_HEADER_BYTES];
    char project[MAX_PROJECT_BYTES];    // which Claude Code session is asking
    char text[MAX_QUESTION_BYTES];
    char options[MAX_OPTIONS][MAX_OPTION_BYTES];
    uint8_t optionCount;
    bool allowFreeText;
    char suggestions[MAX_SUGGESTIONS][MAX_SUGGESTION_BYTES];
    uint8_t suggestionCount;
    uint16_t maxText;
    uint32_t arrivedAt;
    bool selected[MAX_OPTIONS];         // MULTI mode
    uint8_t page;                       // paginated CHOICE
};

struct PermissionRequest {
    char requestId[MAX_REQUEST_ID + 1];
    char tool[MAX_TOOL_BYTES];
    char summary[MAX_SUMMARY_BYTES];
    char details[MAX_DETAILS_BYTES];
    char agent[21];
    char project[MAX_PROJECT_BYTES];
    uint32_t arrivedAt;
};

struct StopRequest {
    char requestId[MAX_REQUEST_ID + 1];
    char summary[MAX_DETAILS_BYTES];
    uint16_t count;
    uint16_t max;
    char project[MAX_PROJECT_BYTES];
    uint32_t arrivedAt;
};

struct TextComposerState;   // composer.h

// Copies a UTF-8 string into a fixed buffer: keeps printable ASCII, newline
// and the Arabic ranges the display font can show, replaces other scripts
// with '?', drops control characters, never cuts a multi-byte sequence, and
// always NUL-terminates.
void copyUtf8(char *dst, size_t dstSize, const char *src);
