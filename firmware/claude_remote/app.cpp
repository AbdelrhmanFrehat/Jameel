#include "app.h"

#include <string.h>

#include "arabic.h"

#include "composer.h"
#include "input.h"
#include "model.h"
#include "protocol.h"
#include "ui.h"

// ---- model.h helper ---------------------------------------------------------------

static bool displayable(uint16_t cp) {
    if (cp == '\n') return true;
    if (cp >= 0x20 && cp < 0x7F) return true;
    if (cp >= 0x0600 && cp <= 0x06FF) return true;   // Arabic
    if (cp >= 0xFB50 && cp <= 0xFDFF) return true;   // presentation forms A
    if (cp >= 0xFE70 && cp <= 0xFEFF) return true;   // presentation forms B
    if (cp == 0x200C) return true;                   // zero-width non-joiner
    return false;
}

void copyUtf8(char *dst, size_t dstSize, const char *src) {
    if (dstSize == 0) return;
    dst[0] = '\0';
    if (!src) return;
    static uint16_t cps[ARABIC_MAX_CPS * 2];
    uint16_t n = utf8Decode(src, cps, ARABIC_MAX_CPS * 2);
    size_t o = 0;
    for (uint16_t i = 0; i < n; i++) {
        uint16_t cp = cps[i];
        if (cp == '\t') cp = ' ';
        else if (cp < 0x20 && cp != '\n') continue;          // control characters
        else if (!displayable(cp)) cp = '?';
        uint8_t w = utf8Encode(cp, dst + o, dstSize - o);
        if (w == 0) break;                                    // buffer full: stop on a boundary
        o += w;
    }
    dst[o] = '\0';
}

namespace {

// ---- state ------------------------------------------------------------------------
ClaudeStatus status = {};
RemoteQuestion question = {};
PermissionRequest permission = {};
StopRequest stopReq = {};
TextComposerState composer = {};

UiMode mode = UiMode::BOOT;
RequestKind activeKind = RequestKind::NONE;
char activeId[MAX_REQUEST_ID + 1] = "";
char lastAnsweredId[MAX_REQUEST_ID + 1] = "";
uint32_t activeArrivedAt = 0;

int16_t scroll = 0;
uint8_t textLines = 0;
uint32_t sentUntil = 0;
uint32_t toastUntil = 0;
bool toastActive = false;
uint32_t bootStarted = 0;
uint32_t lastTimerSecond = 0xFFFFFFFF;
bool composerFromChoice = false;
UiMode composerReturnMode = UiMode::CHOICE;

uint8_t quickCount = 5;
char quick[5][MAX_QUICK_BYTES] = {"YES", "NO", "CONTINUE", "RETRY", "SKIP"};

// shortcuts: fixed instructions from the host config
char shortcutLabels[MAX_SHORTCUTS][MAX_OPTION_BYTES];
uint8_t shortcutCount = 0;
RemoteQuestion shortcutMenu = {};
UiMode shortcutFrom = UiMode::STATUS;       // where B9 (back) returns to
char shortcutStopId[MAX_REQUEST_ID + 1] = "";   // stop request being answered with a shortcut, if any

// session selection made on the device (kept for a moment even if the host
// pushes an older `selected` before it has processed our select_session)
char localSelectedId[9] = "";
uint32_t localSelectAt = 0;

// test mode
bool testMode = false;
char testLastEvent[32] = "-";
int testEncoderPos = 0;
int32_t testEncoderLast = 0;

// ---- helpers -------------------------------------------------------------------------

UiMode homeMode() {
    return protoConnected() ? UiMode::STATUS : UiMode::DISCONNECTED;
}

void render();

void setMode(UiMode m) {
    mode = m;
    scroll = 0;
    render();
}

void showToast(const char *text, const char *level, uint32_t ttl) {
    uiDrawToast(text, level);
    toastActive = true;
    toastUntil = millis() + ttl;
}

bool hasActive() { return activeKind != RequestKind::NONE; }

void clearActive() {
    activeKind = RequestKind::NONE;
    activeId[0] = '\0';
}

void finishRequest(const char *label, const char *value) {
    strlcpy(lastAnsweredId, activeId, sizeof(lastAnsweredId));
    clearActive();
    sentUntil = millis() + SENT_OVERLAY_MS;
    mode = UiMode::SENT;
    uiDrawSent(label, value);
}

// Presses that physically began before (or right as) a request arrived can
// never answer it.
bool isStale(const InputEvent &ev) {
    return hasActive() && (int32_t)(ev.pressedAt - activeArrivedAt) < (int32_t)STALE_PRESS_GRACE_MS;
}

void applySessionEntry(int8_t idx) {
    if (idx < 0 || idx >= (int8_t)status.sessionCount) return;
    const SessionEntry &e = status.sessions[idx];
    strlcpy(status.state, e.state, sizeof(status.state));
    strlcpy(status.title, e.title, sizeof(status.title));
    strlcpy(status.detail, e.detail, sizeof(status.detail));
    strlcpy(status.project, e.project, sizeof(status.project));
    strlcpy(status.session, e.id, sizeof(status.session));
    strlcpy(status.tool, e.tool, sizeof(status.tool));
    status.elapsedBase = e.elapsedBase;
    status.elapsedAt = millis();
    status.selectedIndex = idx;
    status.valid = true;
}

void openShortcuts(UiMode from) {
    if (shortcutCount == 0) {
        showToast("No shortcuts configured (config.yaml)", "warn", 2000);
        return;
    }
    if (!protoConnected()) {
        showToast("Host disconnected", "warn", 1500);
        return;
    }
    shortcutFrom = from;
    shortcutStopId[0] = '\0';
    if (from == UiMode::STOPPED && hasActive() && activeKind == RequestKind::STOP) {
        strlcpy(shortcutStopId, activeId, sizeof(shortcutStopId));
    }
    strlcpy(shortcutMenu.text, from == UiMode::STOPPED ? "Continue with this instruction:"
                                                       : "Send this instruction to Claude:", sizeof(shortcutMenu.text));
    strlcpy(shortcutMenu.header, "SHORTCUTS", sizeof(shortcutMenu.header));
    strlcpy(shortcutMenu.project, status.project, sizeof(shortcutMenu.project));
    shortcutMenu.optionCount = shortcutCount;
    for (uint8_t i = 0; i < shortcutCount; i++) {
        strlcpy(shortcutMenu.options[i], shortcutLabels[i], sizeof(shortcutMenu.options[0]));
        shortcutMenu.selected[i] = false;
    }
    shortcutMenu.allowFreeText = false;
    shortcutMenu.page = 0;
    mode = UiMode::SHORTCUTS;
    uiDrawShortcuts(shortcutMenu);
}

void render() {
    switch (mode) {
        case UiMode::BOOT:         uiDrawBoot(); break;
        case UiMode::DISCONNECTED: uiDrawDisconnected(); break;
        case UiMode::STATUS:       uiDrawStatus(status, protoConnected()); lastTimerSecond = 0xFFFFFFFF; break;
        case UiMode::YES_NO:       uiDrawYesNo(question, scroll, &textLines); break;
        case UiMode::CHOICE:       uiDrawChoice(question, scroll, &textLines); break;
        case UiMode::MULTI:        uiDrawMulti(question, scroll, &textLines); break;
        case UiMode::TEXT_INPUT:   uiDrawTextInput(question, composer, quick, quickCount); break;
        case UiMode::PERMISSION:   uiDrawPermission(permission, scroll, &textLines); break;
        case UiMode::STOPPED:      uiDrawStopped(stopReq, scroll, &textLines); break;
        case UiMode::SENT:         break;   // drawn by finishRequest
        case UiMode::SHORTCUTS:    uiDrawShortcuts(shortcutMenu); break;
        case UiMode::TEST: {
            UiTestInfo t;
            for (uint8_t i = 0; i < NUM_BUTTONS; i++) t.pressed[i] = inputIsPressed(i + 1);
            t.encoderRaw = inputEncoderRaw();
            t.encoderPos = testEncoderPos;
            t.lastEvent = testLastEvent;
            t.hostConnected = protoConnected();
            const ProtocolStats &st = protoStats();
            t.rxLines = st.rxLines;
            t.txMessages = st.txMessages;
            t.rejected = st.rxRejected + st.rxOverflow;
            uiDrawTest(t, true);
            break;
        }
    }
    toastActive = false;
}

uint8_t visibleLinesForMode() {
    switch (mode) {
        case UiMode::YES_NO:     return 6;
        case UiMode::CHOICE:     return question.optionCount > OPTIONS_PER_PAGE ? 3 : 4;
        case UiMode::MULTI:      return 3;
        case UiMode::PERMISSION: return 6;
        case UiMode::STOPPED:    return 5;
        default:                 return 0;
    }
}

void scrollBy(int32_t delta) {
    uint8_t visible = visibleLinesForMode();
    if (visible == 0 || textLines <= visible) return;
    int16_t maxScroll = textLines - visible;
    int16_t s = scroll + (int16_t)delta;
    if (s < 0) s = 0;
    if (s > maxScroll) s = maxScroll;
    if (s == scroll) return;
    scroll = s;
    render();
}

void enterComposer(UiMode from) {
    composerFromChoice = (from == UiMode::CHOICE || from == UiMode::MULTI || from == UiMode::YES_NO);
    composerReturnMode = from;
    composerBegin(composer, question.maxText, question.suggestions, question.suggestionCount);
    setMode(UiMode::TEXT_INPUT);
}

// ---- test mode ----------------------------------------------------------------------

void enterTestMode() {
    testMode = true;
    protoSetMode("test");
    protoSendLog("info", "entering hardware test mode");
    if (hasActive()) {
        protoSendAnswerCancelled(activeId, "test_mode");
        clearActive();
    }
    strlcpy(testLastEvent, "-", sizeof(testLastEvent));
    setMode(UiMode::TEST);
}

void exitTestMode() {
    testMode = false;
    protoSetMode("remote");
    protoSendLog("info", "leaving hardware test mode");
    protoSendHello();
    setMode(homeMode());
}

void handleTestEvent(const InputEvent &ev) {
    const char *action = ev.action == ButtonAction::PRESS ? "press"
                       : ev.action == ButtonAction::RELEASE ? "release" : "long";
    snprintf(testLastEvent, sizeof(testLastEvent), "B%d %s%s", ev.button, action, ev.clean ? "" : " (ghost?)");
    protoSendEvent("button", ev.button, action, 0);
    UiTestInfo t;
    for (uint8_t i = 0; i < NUM_BUTTONS; i++) t.pressed[i] = inputIsPressed(i + 1);
    t.encoderRaw = inputEncoderRaw();
    t.encoderPos = testEncoderPos;
    t.lastEvent = testLastEvent;
    t.hostConnected = protoConnected();
    const ProtocolStats &st = protoStats();
    t.rxLines = st.rxLines;
    t.txMessages = st.txMessages;
    t.rejected = st.rxRejected + st.rxOverflow;
    uiDrawTest(t, false);
}

// ---- answering ------------------------------------------------------------------------

void answerOption(uint8_t idx) {
    if (idx >= question.optionCount) return;
    protoSendAnswerOption(activeId, idx, question.options[idx]);
    finishRequest("SENT", question.options[idx]);
}

void answerBoolean(bool v) {
    protoSendAnswerBoolean(activeId, v);
    finishRequest("SENT", v ? "YES" : "NO");
}

void answerText(const char *text) {
    protoSendAnswerText(activeId, text);
    finishRequest("SENT", text);
}

void answerMulti() {
    uint8_t count = question.optionCount > 7 ? 7 : question.optionCount;
    protoSendAnswerMulti(activeId, question.selected, count, question.options);
    char joined[128] = "";
    for (uint8_t i = 0; i < count; i++) {
        if (!question.selected[i]) continue;
        if (joined[0]) strlcat(joined, ", ", sizeof(joined));
        strlcat(joined, question.options[i], sizeof(joined));
    }
    finishRequest("SENT", joined);
}

void cancelToPc() {
    switch (activeKind) {
        case RequestKind::QUESTION:   protoSendAnswerCancelled(activeId, "user"); break;
        case RequestKind::PERMISSION: protoSendPermissionDecision(activeId, "cancelled"); break;
        case RequestKind::STOP:       protoSendStopDecision(activeId, "cancelled"); break;
        default: return;
    }
    finishRequest("HANDED TO PC", "Answer it in Claude Code on the computer.");
}

void permissionDecision(const char *decision) {
    protoSendPermissionDecision(activeId, decision);
    finishRequest(!strcmp(decision, "allow") ? "ALLOWED" : "DENIED", permission.summary);
}

void stopDecision(const char *decision) {
    protoSendStopDecision(activeId, decision);
    finishRequest(!strcmp(decision, "continue") ? "CONTINUE" : "DONE",
                  !strcmp(decision, "continue") ? "Claude keeps working." : "Claude stops here.");
}

// ---- input dispatch -----------------------------------------------------------------------

void handleYesNo(const InputEvent &ev) {
    if (ev.action == ButtonAction::PRESS) {
        if (!ev.clean) return;
        if (ev.button == 1) answerBoolean(true);
        else if (ev.button == 2) answerBoolean(false);
        else if (ev.button == 7 && question.allowFreeText) enterComposer(UiMode::YES_NO);
        else if (ev.button == 8) cancelToPc();
    } else if (ev.action == ButtonAction::RELEASE && ev.button == 9 && !ev.longFired) {
        cancelToPc();
    }
}

void handlePermission(const InputEvent &ev) {
    if (ev.action == ButtonAction::PRESS) {
        if (!ev.clean) return;
        if (ev.button == 1) permissionDecision("allow");
        else if (ev.button == 2) permissionDecision("deny");
        else if (ev.button == 8) cancelToPc();
    } else if (ev.action == ButtonAction::RELEASE && ev.button == 9 && !ev.longFired) {
        cancelToPc();
    }
}

void handleStopped(const InputEvent &ev) {
    if (ev.action == ButtonAction::PRESS) {
        if (!ev.clean) return;
        if (ev.button == 1) stopDecision("continue");
        else if (ev.button == 2) stopDecision("done");
        else if (ev.button == 3) openShortcuts(UiMode::STOPPED);
        else if (ev.button == 8) cancelToPc();
    } else if (ev.action == ButtonAction::RELEASE && ev.button == 9 && !ev.longFired) {
        cancelToPc();
    }
}

void handleChoice(const InputEvent &ev) {
    bool paginated = question.optionCount > OPTIONS_PER_PAGE;
    uint8_t first = paginated ? question.page * OPTIONS_PER_PAGE : 0;
    uint8_t count = question.optionCount - first;
    if (count > OPTIONS_PER_PAGE) count = OPTIONS_PER_PAGE;

    if (ev.action == ButtonAction::PRESS && ev.button <= 8) {
        if (!ev.clean) return;
        uint8_t k = ev.button - 1;
        if (k < count) {
            answerOption(first + k);
        } else if (!paginated && question.allowFreeText && k == count) {
            enterComposer(UiMode::CHOICE);
        }
    } else if (ev.button == 9) {
        if (ev.action == ButtonAction::RELEASE && !ev.longFired) cancelToPc();
        else if (ev.action == ButtonAction::LONG && question.allowFreeText) enterComposer(UiMode::CHOICE);
    }
}

void handleMulti(const InputEvent &ev) {
    uint8_t count = question.optionCount > 7 ? 7 : question.optionCount;
    if (ev.action == ButtonAction::PRESS && ev.button <= 8) {
        if (!ev.clean) return;
        uint8_t k = ev.button - 1;
        if (k < count) {
            question.selected[k] = !question.selected[k];
            render();
        } else if (ev.button == 8) {
            bool any = false;
            for (uint8_t i = 0; i < count; i++) any |= question.selected[i];
            if (any) answerMulti();
            else showToast("Select at least one option first", "warn", 1500);
        }
    } else if (ev.button == 9 && ev.action == ButtonAction::RELEASE && !ev.longFired) {
        cancelToPc();
    }
}

void handleTextInput(const InputEvent &ev) {
    if (ev.action == ButtonAction::PRESS) {
        if (!ev.clean && ev.button != 9) return;
        if (ev.button >= 1 && ev.button <= 5) {
            uint8_t k = ev.button - 1;
            if (k >= quickCount) return;
            if (composer.len == 0) {
                answerText(quick[k]);
            } else {
                if (!composerInsertWord(composer, quick[k])) showToast("Answer is full", "warn", 1200);
                uiDrawTextField(composer);
                uiDrawQuickHint(composer);
            }
        } else if (ev.button == 6) {
            composerBackspace(composer);
            uiDrawTextField(composer);
            uiDrawQuickHint(composer);
        } else if (ev.button == 7) {
            composerNextSet(composer);
            uiDrawPicker(composer);
        } else if (ev.button == 8) {
            if (composer.len == 0) {
                showToast("Nothing typed yet - B1-B5 send a quick reply", "warn", 1500);
            } else {
                answerText(composer.text);
            }
        }
    } else if (ev.action == ButtonAction::LONG) {
        if (ev.button == 6) {
            composerClear(composer);
            uiDrawTextField(composer);
            uiDrawQuickHint(composer);
        } else if (ev.button == 9) {
            if (composerFromChoice) setMode(composerReturnMode);
            else cancelToPc();
        }
    } else if (ev.action == ButtonAction::RELEASE && ev.button == 9 && !ev.longFired) {
        if (!composerInsertCurrent(composer)) showToast("Answer is full", "warn", 1200);
        uiDrawTextField(composer);
        uiDrawQuickHint(composer);
    }
}

void handleShortcuts(const InputEvent &ev) {
    bool paginated = shortcutMenu.optionCount > OPTIONS_PER_PAGE;
    uint8_t first = paginated ? shortcutMenu.page * OPTIONS_PER_PAGE : 0;
    uint8_t count = shortcutMenu.optionCount - first;
    if (count > OPTIONS_PER_PAGE) count = OPTIONS_PER_PAGE;

    if (ev.action == ButtonAction::PRESS && ev.button <= 8) {
        if (!ev.clean) return;
        uint8_t k = ev.button - 1;
        if (k >= count) return;
        uint8_t idx = first + k;
        if (shortcutStopId[0] && hasActive() && !strcmp(shortcutStopId, activeId)) {
            protoSendStopShortcut(activeId, idx);
            finishRequest("CONTINUE WITH", shortcutLabels[idx]);
        } else {
            protoSendShortcut(idx, shortcutLabels[idx]);
            sentUntil = millis() + SENT_OVERLAY_MS;
            mode = UiMode::SENT;
            uiDrawSent("QUEUED", shortcutLabels[idx]);
        }
    } else if (ev.button == 9 && ev.action == ButtonAction::RELEASE && !ev.longFired) {
        setMode(shortcutFrom == UiMode::STOPPED && hasActive() ? UiMode::STOPPED : homeMode());
    }
}

void handleEvent(const InputEvent &ev) {
    if (mode == UiMode::TEST) {
        handleTestEvent(ev);
        return;
    }
    if (hasActive() && isStale(ev)) return;
    switch (mode) {
        case UiMode::YES_NO:     handleYesNo(ev); break;
        case UiMode::PERMISSION: handlePermission(ev); break;
        case UiMode::STOPPED:    handleStopped(ev); break;
        case UiMode::CHOICE:     handleChoice(ev); break;
        case UiMode::MULTI:      handleMulti(ev); break;
        case UiMode::TEXT_INPUT: handleTextInput(ev); break;
        case UiMode::SHORTCUTS:  handleShortcuts(ev); break;
        case UiMode::STATUS:
            if (ev.button == 1 && ev.action == ButtonAction::PRESS && ev.clean) {
                openShortcuts(UiMode::STATUS);
            } else if (ev.button == 9 && ev.action == ButtonAction::RELEASE && !ev.longFired && status.sessionCount > 1) {
                status.pinned = false;
                localSelectedId[0] = '\0';
                protoSendFollowSessions();
                showToast("Following the latest session", "info", 1500);
            }
            break;
        default: break;   // BOOT / DISCONNECTED / SENT: buttons inert
    }
}

void handleEncoder(int32_t delta) {
    if (delta == 0) return;
    switch (mode) {
        case UiMode::TEXT_INPUT:
            composerRotate(composer, delta);
            uiDrawPicker(composer);
            break;
        case UiMode::CHOICE:
            if (question.optionCount > OPTIONS_PER_PAGE) {
                uint8_t pages = (question.optionCount + OPTIONS_PER_PAGE - 1) / OPTIONS_PER_PAGE;
                int16_t p = (int16_t)question.page + (delta > 0 ? 1 : -1);
                if (p < 0) p = pages - 1;
                if (p >= pages) p = 0;
                question.page = (uint8_t)p;
                scroll = 0;
                render();
            } else {
                scrollBy(delta);
            }
            break;
        case UiMode::YES_NO:
        case UiMode::MULTI:
        case UiMode::PERMISSION:
        case UiMode::STOPPED:
            scrollBy(delta);
            break;
        case UiMode::SHORTCUTS:
            if (shortcutMenu.optionCount > OPTIONS_PER_PAGE) {
                uint8_t pages = (shortcutMenu.optionCount + OPTIONS_PER_PAGE - 1) / OPTIONS_PER_PAGE;
                int16_t p = (int16_t)shortcutMenu.page + (delta > 0 ? 1 : -1);
                if (p < 0) p = pages - 1;
                if (p >= pages) p = 0;
                shortcutMenu.page = (uint8_t)p;
                uiDrawShortcuts(shortcutMenu);
            }
            break;
        case UiMode::STATUS:
            if (status.sessionCount > 1) {
                int16_t idx = status.selectedIndex + (delta > 0 ? 1 : -1);
                if (idx < 0) idx = status.sessionCount - 1;
                if (idx >= status.sessionCount) idx = 0;
                applySessionEntry((int8_t)idx);
                status.pinned = true;
                strlcpy(localSelectedId, status.sessions[idx].id, sizeof(localSelectedId));
                localSelectAt = millis();
                protoSendSelectSession(localSelectedId);
                render();
            }
            break;
        case UiMode::TEST: {
            int32_t raw = inputEncoderRaw();
            int32_t d = raw - testEncoderLast;
            testEncoderLast = raw;
            testEncoderPos += (int)d * 2;
            if (testEncoderPos < 0) testEncoderPos = 0;
            if (testEncoderPos > 100) testEncoderPos = 100;
            snprintf(testLastEvent, sizeof(testLastEvent), "ENC %s %ld", d > 0 ? "cw" : "ccw", (long)raw);
            protoSendEvent("encoder", -1, d > 0 ? "cw" : "ccw", d);
            UiTestInfo t;
            for (uint8_t i = 0; i < NUM_BUTTONS; i++) t.pressed[i] = inputIsPressed(i + 1);
            t.encoderRaw = raw;
            t.encoderPos = testEncoderPos;
            t.lastEvent = testLastEvent;
            t.hostConnected = protoConnected();
            const ProtocolStats &st = protoStats();
            t.rxLines = st.rxLines;
            t.txMessages = st.txMessages;
            t.rejected = st.rxRejected + st.rxOverflow;
            uiDrawTest(t, false);
            break;
        }
        default: break;
    }
}

// ---- protocol callbacks --------------------------------------------------------------------

bool duplicateRequest(const char *rid) {
    if (!strcmp(rid, lastAnsweredId)) {
        // Our answer may have been lost on the wire; the host re-sent the
        // request after a reconnect. Acknowledge only - the host also gets
        // our cached response through protoResendLastResponse.
        protoSendAck(rid);
        protoResendLastResponse(rid);
        return true;
    }
    if (hasActive() && !strcmp(rid, activeId)) {
        protoSendAck(rid);   // already on screen
        return true;
    }
    return false;
}

void beginRequest(RequestKind kind, const char *rid) {
    if (hasActive() && strcmp(rid, activeId) != 0) {
        // The host serialises requests, so this only happens if the host
        // lost track; the newer request wins and the old one is cancelled.
        protoSendAnswerCancelled(activeId, "superseded");
    }
    activeKind = kind;
    strlcpy(activeId, rid, sizeof(activeId));
    activeArrivedAt = millis();
    protoSendAck(rid);
    if (testMode) exitTestMode();
}

void onConnected() {
    if (mode == UiMode::DISCONNECTED) setMode(UiMode::STATUS);
    else if (mode == UiMode::STATUS) render();
}

void onDisconnected() {
    if (hasActive()) clearActive();   // the host re-sends it on reconnect
    if (mode != UiMode::TEST && mode != UiMode::BOOT) setMode(UiMode::DISCONNECTED);
}

void onState(JsonDocument &doc) {
    copyUtf8(status.state, sizeof(status.state), doc["state"] | "IDLE");
    copyUtf8(status.title, sizeof(status.title), doc["title"] | "");
    copyUtf8(status.detail, sizeof(status.detail), doc["detail"] | "");
    copyUtf8(status.project, sizeof(status.project), doc["project"] | "");
    copyUtf8(status.session, sizeof(status.session), doc["session"] | "");
    copyUtf8(status.tool, sizeof(status.tool), doc["tool"] | "");
    status.elapsedBase = doc["elapsed"] | 0u;
    status.elapsedAt = millis();
    status.valid = true;

    // Live session list (max MAX_SESSIONS), most recent first.
    status.sessionCount = 0;
    JsonArray list = doc["sessions"].as<JsonArray>();
    for (JsonObject e : list) {
        if (status.sessionCount >= MAX_SESSIONS) break;
        SessionEntry &se = status.sessions[status.sessionCount];
        copyUtf8(se.id, sizeof(se.id), e["id"] | "");
        copyUtf8(se.project, sizeof(se.project), e["project"] | "");
        copyUtf8(se.state, sizeof(se.state), e["state"] | "IDLE");
        copyUtf8(se.title, sizeof(se.title), e["title"] | "");
        copyUtf8(se.detail, sizeof(se.detail), e["detail"] | "");
        copyUtf8(se.tool, sizeof(se.tool), e["tool"] | "");
        se.elapsedBase = e["elapsed"] | 0u;
        status.sessionCount++;
    }
    const char *selected = doc["selected"] | "";
    status.pinned = doc["pinned"] | false;
    status.selectedIndex = -1;
    for (uint8_t i = 0; i < status.sessionCount; i++) {
        if (!strcmp(status.sessions[i].id, selected)) { status.selectedIndex = i; break; }
    }
    // A selection made here in the last 2 s wins over a host push that has not
    // caught up yet (the host pins it as soon as it sees select_session).
    if (localSelectedId[0] && (millis() - localSelectAt) < 2000 && strcmp(selected, localSelectedId) != 0) {
        for (uint8_t i = 0; i < status.sessionCount; i++) {
            if (!strcmp(status.sessions[i].id, localSelectedId)) {
                applySessionEntry(i);
                status.pinned = true;
                break;
            }
        }
    } else if (localSelectedId[0] && !strcmp(selected, localSelectedId)) {
        localSelectedId[0] = '\0';   // host caught up
    }
    if (mode == UiMode::STATUS) render();
}

void onQuestion(JsonDocument &doc) {
    const char *rid = doc["request_id"] | "";
    if (duplicateRequest(rid)) return;

    copyUtf8(question.requestId, sizeof(question.requestId), rid);
    copyUtf8(question.kind, sizeof(question.kind), doc["kind"] | "choice");
    copyUtf8(question.header, sizeof(question.header), doc["header"] | "");
    copyUtf8(question.project, sizeof(question.project), doc["project"] | "");
    copyUtf8(question.text, sizeof(question.text), doc["question"] | "?");
    question.allowFreeText = doc["allow_free_text"] | false;
    question.maxText = doc["max_text"] | (uint16_t)MAX_TEXT_ANSWER;
    question.page = 0;
    question.optionCount = 0;
    JsonArray opts = doc["options"].as<JsonArray>();
    for (JsonVariant v : opts) {
        if (question.optionCount >= MAX_OPTIONS) break;
        copyUtf8(question.options[question.optionCount], sizeof(question.options[0]), v.as<const char *>());
        question.selected[question.optionCount] = false;
        question.optionCount++;
    }
    question.suggestionCount = 0;
    JsonArray sugg = doc["suggestions"].as<JsonArray>();
    for (JsonVariant v : sugg) {
        if (question.suggestionCount >= MAX_SUGGESTIONS) break;
        copyUtf8(question.suggestions[question.suggestionCount], sizeof(question.suggestions[0]),
                  v.as<const char *>());
        question.suggestionCount++;
    }
    JsonArray q = doc["quick"].as<JsonArray>();
    if (!q.isNull() && q.size() > 0) {
        quickCount = 0;
        for (JsonVariant v : q) {
            if (quickCount >= 5) break;
            copyUtf8(quick[quickCount], sizeof(quick[0]), v.as<const char *>());
            if (quick[quickCount][0]) quickCount++;
        }
        if (quickCount == 0) {
            quickCount = 5;
            const char *defaults[5] = {"YES", "NO", "CONTINUE", "RETRY", "SKIP"};
            for (uint8_t i = 0; i < 5; i++) strlcpy(quick[i], defaults[i], sizeof(quick[0]));
        }
    }
    question.arrivedAt = millis();
    beginRequest(RequestKind::QUESTION, rid);

    if (!strcmp(question.kind, "yes_no")) {
        setMode(UiMode::YES_NO);
    } else if (!strcmp(question.kind, "text") || question.optionCount == 0) {
        enterComposer(UiMode::STATUS);
    } else if (!strcmp(question.kind, "multi")) {
        setMode(UiMode::MULTI);
    } else {
        setMode(UiMode::CHOICE);
    }
}

void onPermission(JsonDocument &doc) {
    const char *rid = doc["request_id"] | "";
    if (duplicateRequest(rid)) return;
    copyUtf8(permission.requestId, sizeof(permission.requestId), rid);
    copyUtf8(permission.tool, sizeof(permission.tool), doc["tool"] | "?");
    copyUtf8(permission.summary, sizeof(permission.summary), doc["summary"] | "");
    copyUtf8(permission.details, sizeof(permission.details), doc["details"] | "");
    copyUtf8(permission.agent, sizeof(permission.agent), doc["agent"] | "");
    copyUtf8(permission.project, sizeof(permission.project), doc["project"] | "");
    permission.arrivedAt = millis();
    beginRequest(RequestKind::PERMISSION, rid);
    setMode(UiMode::PERMISSION);
}

void onStop(JsonDocument &doc) {
    const char *rid = doc["request_id"] | "";
    if (duplicateRequest(rid)) return;
    copyUtf8(stopReq.requestId, sizeof(stopReq.requestId), rid);
    copyUtf8(stopReq.summary, sizeof(stopReq.summary), doc["summary"] | "");
    stopReq.count = doc["count"] | 1;
    stopReq.max = doc["max"] | 20;
    copyUtf8(stopReq.project, sizeof(stopReq.project), doc["project"] | "");
    stopReq.arrivedAt = millis();
    beginRequest(RequestKind::STOP, rid);
    setMode(UiMode::STOPPED);
}

void onCancel(const char *rid, const char *reason) {
    if (!hasActive() || strcmp(rid, activeId) != 0) return;
    clearActive();
    shortcutStopId[0] = '\0';
    setMode(homeMode());
    char msg[64];
    if (!strcmp(reason, "pc")) {
        strlcpy(msg, "Handed to the PC - you are active there", sizeof(msg));
    } else if (!strcmp(reason, "timeout")) {
        strlcpy(msg, "No answer in time - handed to the PC", sizeof(msg));
    } else {
        snprintf(msg, sizeof(msg), "Request withdrawn by host (%s)", reason);
    }
    showToast(msg, "warn", 2500);
}

void onNotify(const char *text, const char *level, uint32_t ttl) {
    if (mode == UiMode::TEST || mode == UiMode::BOOT) return;
    showToast(text, level, ttl);
}

void onShortcuts(JsonDocument &doc) {
    shortcutCount = 0;
    JsonArray items = doc["items"].as<JsonArray>();
    for (JsonVariant v : items) {
        if (shortcutCount >= MAX_SHORTCUTS) break;
        copyUtf8(shortcutLabels[shortcutCount], sizeof(shortcutLabels[0]), v.as<const char *>());
        if (shortcutLabels[shortcutCount][0]) shortcutCount++;
    }
    if (mode == UiMode::SHORTCUTS) openShortcuts(shortcutFrom);
}

void onMode(const char *m) {
    if (!strcmp(m, "test") && !testMode) enterTestMode();
    else if (!strcmp(m, "remote") && testMode) exitTestMode();
}

}  // namespace

// ---- public ------------------------------------------------------------------------------------

static const char *modeName(UiMode m) {
    switch (m) {
        case UiMode::BOOT: return "BOOT";
        case UiMode::DISCONNECTED: return "DISCONNECTED";
        case UiMode::STATUS: return "STATUS";
        case UiMode::YES_NO: return "YES_NO";
        case UiMode::CHOICE: return "CHOICE";
        case UiMode::MULTI: return "MULTI";
        case UiMode::TEXT_INPUT: return "TEXT_INPUT";
        case UiMode::PERMISSION: return "PERMISSION";
        case UiMode::STOPPED: return "STOPPED";
        case UiMode::SENT: return "SENT";
        case UiMode::TEST: return "TEST";
        case UiMode::SHORTCUTS: return "SHORTCUTS";
    }
    return "?";
}

void appDebugSnapshot(AppDebug &out) {
    out.mode = modeName(mode);
    out.state = status.state;
    out.selectedId = status.session;
    out.sessionCount = status.sessionCount;
    out.selectedIndex = status.selectedIndex;
    out.pinned = status.pinned;
    out.statusValid = status.valid;
    out.shortcutCount = shortcutCount;
}

void appSetup() {
    ProtocolCallbacks cb;
    cb.onConnected = onConnected;
    cb.onDisconnected = onDisconnected;
    cb.onState = onState;
    cb.onQuestion = onQuestion;
    cb.onPermission = onPermission;
    cb.onStop = onStop;
    cb.onCancel = onCancel;
    cb.onNotify = onNotify;
    cb.onMode = onMode;
    cb.onShortcuts = onShortcuts;
    protoSetup(cb, "remote");
    status.valid = false;
    status.sessionCount = 0;
    status.selectedIndex = -1;
    status.pinned = false;
    bootStarted = millis();
    mode = UiMode::BOOT;
    render();
    protoSendHello();
}

void appLoop() {
    uint32_t now = millis();
    inputPoll();
    protoPoll();

    // Hold B1 on the boot splash / disconnected screen -> hardware test mode.
    if ((mode == UiMode::BOOT || mode == UiMode::DISCONNECTED) && inputHeldFor(1) >= TESTMODE_HOLD_MS &&
        inputPressedCount() == 1) {
        enterTestMode();
    }
    // Hold B8 in test mode -> back to remote mode.
    if (mode == UiMode::TEST && inputHeldFor(8) >= TESTMODE_EXIT_MS && inputPressedCount() == 1) {
        exitTestMode();
    }

    if (mode == UiMode::BOOT && (now - bootStarted) >= BOOT_SPLASH_MS) {
        setMode(homeMode());
    }

    InputEvent ev;
    while (inputPop(ev)) {
        // Drain events generated while on the boot screen without acting on them.
        if (mode != UiMode::BOOT) handleEvent(ev);
    }
    handleEncoder(inputConsumeEncoderSteps());

    if (mode == UiMode::SENT && (int32_t)(now - sentUntil) >= 0) {
        setMode(homeMode());
    }
    if (toastActive && (int32_t)(now - toastUntil) >= 0) {
        toastActive = false;
        uiClearToast();
        if (mode == UiMode::STATUS) lastTimerSecond = 0xFFFFFFFF;
    }
    if (mode == UiMode::STATUS && status.valid) {
        uint32_t elapsed = status.elapsedBase + (now - status.elapsedAt) / 1000;
        if (elapsed != lastTimerSecond) {
            lastTimerSecond = elapsed;
            uiDrawTimer(elapsed);
        }
    }
}
