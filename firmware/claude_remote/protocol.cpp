#include "protocol.h"

#include <ctype.h>
#include <string.h>

#include "app.h"
#include "arabic.h"

namespace {

ProtocolCallbacks callbacks = {};
ProtocolStats stats = {};
char currentMode[8] = "remote";

char lineBuf[SERIAL_LINE_MAX];
size_t lineLen = 0;
bool lineOverflow = false;

bool connected = false;
bool handshakeDone = false;     // hello_ack received since our last hello
uint32_t lastRxMs = 0;
uint32_t lastHelloMs = 0;
uint32_t txSeq = 0;

void sendDoc(JsonDocument &doc) {
    doc["seq"] = ++txSeq;
    serializeJson(doc, Serial);
    Serial.write('\n');
    stats.txMessages++;
}

// Last answer / decision we sent, kept so a re-delivered request (host
// reconnect after our answer was lost) can be answered again without user
// interaction.
char lastResponseLine[640] = "";
char lastResponseId[MAX_REQUEST_ID + 1] = "";

void sendResponseDoc(JsonDocument &doc) {
    doc["seq"] = ++txSeq;
    size_t n = serializeJson(doc, lastResponseLine, sizeof(lastResponseLine) - 1);
    lastResponseLine[n] = '\0';
    strlcpy(lastResponseId, doc["request_id"] | "", sizeof(lastResponseId));
    Serial.write((const uint8_t *)lastResponseLine, n);
    Serial.write('\n');
    stats.txMessages++;
}

bool validRequestId(const char *id) {
    if (!id) return false;
    size_t n = strlen(id);
    if (n == 0 || n > MAX_REQUEST_ID) return false;
    for (size_t i = 0; i < n; i++) {
        char c = id[i];
        if (!(isalnum((unsigned char)c) || c == '-' || c == '_')) return false;
    }
    return true;
}

void markConnected() {
    lastRxMs = millis();
    if (!connected) {
        connected = true;
        stats.reconnects++;
        protoSendHello();
        if (callbacks.onConnected) callbacks.onConnected();
    }
}

void handleLine() {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, lineBuf, lineLen);
    if (err || !doc.is<JsonObject>()) {
        stats.rxRejected++;
        return;
    }
    const char *type = doc["type"] | "";
    if (!type[0]) {
        stats.rxRejected++;
        return;
    }
    stats.rxLines++;

    // A host sequence number that goes backwards means the bridge process
    // restarted: re-introduce ourselves so the new bridge completes the
    // handshake even though we never saw a gap in heartbeats.
    uint32_t seq = doc["seq"] | 0u;
    if (seq != 0 && seq < stats.lastHostSeq) {
        handshakeDone = false;
    }
    if (seq != 0) stats.lastHostSeq = seq;

    markConnected();

    if (strcmp(type, "hello_ack") == 0) {
        handshakeDone = true;
    } else if (strcmp(type, "heartbeat") == 0) {
        stats.heartbeats++;
        JsonDocument ack;
        ack["type"] = "heartbeat_ack";
        sendDoc(ack);
        if (!handshakeDone) protoSendHello();
    } else if (strcmp(type, "state") == 0) {
        if (callbacks.onState) callbacks.onState(doc);
    } else if (strcmp(type, "question") == 0) {
        const char *rid = doc["request_id"] | "";
        if (!validRequestId(rid)) { stats.rxRejected++; return; }
        if (callbacks.onQuestion) callbacks.onQuestion(doc);
    } else if (strcmp(type, "permission") == 0) {
        const char *rid = doc["request_id"] | "";
        if (!validRequestId(rid)) { stats.rxRejected++; return; }
        if (callbacks.onPermission) callbacks.onPermission(doc);
    } else if (strcmp(type, "stop") == 0) {
        const char *rid = doc["request_id"] | "";
        if (!validRequestId(rid)) { stats.rxRejected++; return; }
        if (callbacks.onStop) callbacks.onStop(doc);
    } else if (strcmp(type, "cancel") == 0) {
        const char *rid = doc["request_id"] | "";
        if (!validRequestId(rid)) { stats.rxRejected++; return; }
        protoSendAck(rid);
        if (callbacks.onCancel) callbacks.onCancel(rid, doc["reason"] | "host_cancel");
    } else if (strcmp(type, "notify") == 0) {
        uint32_t ttl = doc["ttl"] | TOAST_DEFAULT_MS;
        if (callbacks.onNotify) callbacks.onNotify(doc["text"] | "", doc["level"] | "info", ttl);
    } else if (strcmp(type, "mode") == 0) {
        if (callbacks.onMode) callbacks.onMode(doc["mode"] | "remote");
    } else if (strcmp(type, "shortcuts") == 0) {
        if (callbacks.onShortcuts) callbacks.onShortcuts(doc);
    } else if (strcmp(type, "debug") == 0) {
        // Diagnostic: what the device currently holds (mode, session list, heap).
        AppDebug d;
        appDebugSnapshot(d);
        JsonDocument reply;
        reply["type"] = "debug_result";
        reply["mode"] = d.mode;
        reply["state"] = d.state;
        reply["selected"] = d.selectedId;
        reply["session_count"] = d.sessionCount;
        reply["selected_index"] = d.selectedIndex;
        reply["pinned"] = d.pinned;
        reply["status_valid"] = d.statusValid;
        reply["shortcuts"] = d.shortcutCount;
        reply["firmware"] = FIRMWARE_VERSION;
        reply["heap"] = (uint32_t)ESP.getFreeHeap();
        reply["uptime"] = millis() / 1000;
        reply["rx"] = stats.rxLines;
        reply["rejected"] = stats.rxRejected;
        reply["overflow"] = stats.rxOverflow;
        sendDoc(reply);
    } else if (strcmp(type, "shape_test") == 0) {
        // Diagnostic: return the visual (shaped + reordered) form of a line so the
        // host can verify the on-device Arabic engine against its reference.
        static char visual[1024];
        const char *text = doc["text"] | "";
        char base = textBaseDirection(text);
        uint16_t cells = arabicToVisual(text, base, visual, sizeof(visual));
        JsonDocument reply;
        reply["type"] = "shape_result";
        reply["visual"] = visual;
        reply["cells"] = cells;
        reply["base"] = base == 'R' ? "R" : "L";
        reply["arabic"] = textHasArabic(text);
        sendDoc(reply);
    } else {
        // Unknown but well-formed message: ignore (forward compatible).
    }
}

}  // namespace

void protoSetup(const ProtocolCallbacks &cb, const char *mode) {
    callbacks = cb;
    protoSetMode(mode);
    lineLen = 0;
    lineOverflow = false;
    connected = false;
    handshakeDone = false;
    lastRxMs = millis();
    lastHelloMs = 0;
}

void protoSetMode(const char *mode) {
    strlcpy(currentMode, mode ? mode : "remote", sizeof(currentMode));
}

bool protoConnected() { return connected; }

const ProtocolStats &protoStats() { return stats; }

void protoPoll() {
    // Drain whatever is available, but bound the work per loop iteration so
    // a flood can never starve input scanning or rendering.
    uint16_t budget = 1024;
    while (budget-- && Serial.available()) {
        int ci = Serial.read();
        if (ci < 0) break;
        char c = (char)ci;
        if (c == '\n') {
            if (!lineOverflow && lineLen > 0) {
                lineBuf[lineLen] = '\0';
                handleLine();
            } else if (lineOverflow) {
                stats.rxOverflow++;
            }
            lineLen = 0;
            lineOverflow = false;
        } else if (c != '\r') {
            if (lineLen < SERIAL_LINE_MAX - 1) {
                lineBuf[lineLen++] = c;
            } else {
                lineOverflow = true;   // discard until the next newline
            }
        }
    }

    uint32_t now = millis();
    if (connected && (now - lastRxMs) > HEARTBEAT_TIMEOUT_MS) {
        connected = false;
        handshakeDone = false;
        if (callbacks.onDisconnected) callbacks.onDisconnected();
    }
    if (!connected && (now - lastHelloMs) > HELLO_RETRY_MS) {
        protoSendHello();
    }
}

// ---- outgoing -----------------------------------------------------------------

void protoSendHello() {
    lastHelloMs = millis();
    JsonDocument doc;
    doc["type"] = "hello";
    doc["device"] = DEVICE_NAME;
    doc["protocol"] = PROTOCOL_VERSION;
    doc["firmware"] = FIRMWARE_VERSION;
    doc["mode"] = currentMode;
    doc["boot"] = millis();
    sendDoc(doc);
}

void protoSendAck(const char *requestId) {
    JsonDocument doc;
    doc["type"] = "ack";
    doc["request_id"] = requestId;
    sendDoc(doc);
}

void protoSendAnswerOption(const char *requestId, uint8_t index, const char *value) {
    JsonDocument doc;
    doc["type"] = "answer";
    doc["request_id"] = requestId;
    doc["answer_type"] = "option";
    doc["option_index"] = index;
    doc["value"] = value;
    sendResponseDoc(doc);
}

void protoSendAnswerBoolean(const char *requestId, bool value) {
    JsonDocument doc;
    doc["type"] = "answer";
    doc["request_id"] = requestId;
    doc["answer_type"] = "boolean";
    doc["value"] = value;
    sendResponseDoc(doc);
}

void protoSendAnswerText(const char *requestId, const char *value) {
    JsonDocument doc;
    doc["type"] = "answer";
    doc["request_id"] = requestId;
    doc["answer_type"] = "text";
    doc["value"] = value;
    sendResponseDoc(doc);
}

void protoSendAnswerMulti(const char *requestId, const bool *selected, uint8_t count,
                          const char options[][MAX_OPTION_BYTES]) {
    JsonDocument doc;
    doc["type"] = "answer";
    doc["request_id"] = requestId;
    doc["answer_type"] = "multi";
    JsonArray idx = doc["option_indexes"].to<JsonArray>();
    char joined[MAX_OPTIONS * (MAX_OPTION_BYTES + 2)];
    joined[0] = '\0';
    for (uint8_t i = 0; i < count; i++) {
        if (!selected[i]) continue;
        idx.add(i);
        if (joined[0]) strlcat(joined, ", ", sizeof(joined));
        strlcat(joined, options[i], sizeof(joined));
    }
    doc["value"] = joined;
    sendResponseDoc(doc);
}

void protoSendAnswerCancelled(const char *requestId, const char *reason) {
    JsonDocument doc;
    doc["type"] = "answer";
    doc["request_id"] = requestId;
    doc["answer_type"] = "cancelled";
    doc["reason"] = reason;
    sendResponseDoc(doc);
}

void protoSendPermissionDecision(const char *requestId, const char *decision) {
    JsonDocument doc;
    doc["type"] = "permission_decision";
    doc["request_id"] = requestId;
    doc["decision"] = decision;
    sendResponseDoc(doc);
}

void protoSendStopDecision(const char *requestId, const char *decision) {
    JsonDocument doc;
    doc["type"] = "stop_decision";
    doc["request_id"] = requestId;
    doc["decision"] = decision;
    sendResponseDoc(doc);
}

void protoSendEvent(const char *name, int button, const char *action, int32_t steps) {
    JsonDocument doc;
    doc["type"] = "event";
    doc["name"] = name;
    if (button >= 0) doc["button"] = button;
    if (action) doc["action"] = action;
    if (steps != 0) doc["steps"] = steps;
    sendDoc(doc);
}

void protoSendShortcut(uint8_t index, const char *label) {
    JsonDocument doc;
    doc["type"] = "shortcut";
    doc["index"] = index;
    doc["label"] = label;
    sendDoc(doc);
}

void protoSendStopShortcut(const char *requestId, uint8_t index) {
    JsonDocument doc;
    doc["type"] = "stop_decision";
    doc["request_id"] = requestId;
    doc["decision"] = "shortcut";
    doc["index"] = index;
    sendResponseDoc(doc);
}

void protoSendSelectSession(const char *shortId) {
    JsonDocument doc;
    doc["type"] = "select_session";
    doc["id"] = shortId;
    sendDoc(doc);
}

void protoSendFollowSessions() {
    JsonDocument doc;
    doc["type"] = "follow_sessions";
    sendDoc(doc);
}

void protoSendLog(const char *level, const char *msg) {
    JsonDocument doc;
    doc["type"] = "log";
    doc["level"] = level;
    doc["msg"] = msg;
    sendDoc(doc);
}

void protoResendLastResponse(const char *requestId) {
    if (!lastResponseLine[0] || strcmp(requestId, lastResponseId) != 0) return;
    Serial.write((const uint8_t *)lastResponseLine, strlen(lastResponseLine));
    Serial.write('\n');
    stats.txMessages++;
}
