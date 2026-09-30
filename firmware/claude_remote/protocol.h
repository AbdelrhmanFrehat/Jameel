// ============================================================================
//  NDJSON serial protocol (docs/PROTOCOL.md) over the native USB CDC.
//
//  Non-blocking line reader with a bounded buffer, strict per-message field
//  validation, heartbeat-based liveness, and typed send helpers. The
//  application never touches Serial directly.
// ============================================================================
#pragma once

#include <ArduinoJson.h>
#include "config.h"

struct ProtocolCallbacks {
    void (*onConnected)();
    void (*onDisconnected)();
    void (*onState)(JsonDocument &doc);
    void (*onQuestion)(JsonDocument &doc);
    void (*onPermission)(JsonDocument &doc);
    void (*onStop)(JsonDocument &doc);
    void (*onCancel)(const char *requestId, const char *reason);
    void (*onNotify)(const char *text, const char *level, uint32_t ttlMs);
    void (*onMode)(const char *mode);
    void (*onShortcuts)(JsonDocument &doc);
};

struct ProtocolStats {
    uint32_t rxLines;
    uint32_t rxRejected;
    uint32_t rxOverflow;
    uint32_t txMessages;
    uint32_t heartbeats;
    uint32_t reconnects;
    uint32_t lastHostSeq;
};

void protoSetup(const ProtocolCallbacks &cb, const char *mode);
void protoPoll();
bool protoConnected();
const ProtocolStats &protoStats();
void protoSetMode(const char *mode);      // "remote" | "test" (reported in hello)

// ---- outgoing ----------------------------------------------------------------
void protoSendHello();
void protoSendAck(const char *requestId);
void protoSendAnswerOption(const char *requestId, uint8_t index, const char *value);
void protoSendAnswerBoolean(const char *requestId, bool value);
void protoSendAnswerText(const char *requestId, const char *value);
void protoSendAnswerMulti(const char *requestId, const bool *selected, uint8_t count,
                          const char options[][MAX_OPTION_BYTES]);
void protoSendAnswerCancelled(const char *requestId, const char *reason);
void protoSendPermissionDecision(const char *requestId, const char *decision);
void protoSendStopDecision(const char *requestId, const char *decision);
void protoSendEvent(const char *name, int button, const char *action, int32_t steps);
void protoSendLog(const char *level, const char *msg);
void protoSendShortcut(uint8_t index, const char *label);            // send a fixed instruction (status screen)
void protoSendStopShortcut(const char *requestId, uint8_t index);   // answer a stop request with a shortcut
void protoSendSelectSession(const char *shortId);   // encoder picked a session
void protoSendFollowSessions();                     // back to auto-follow
// Re-emits the last answer/decision line (used when the host re-delivers an
// already-answered request after a reconnect, i.e. our answer was lost).
void protoResendLastResponse(const char *requestId);
