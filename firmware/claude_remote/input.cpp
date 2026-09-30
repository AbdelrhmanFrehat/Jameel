#include "input.h"

namespace {

struct Button {
    bool stable;
    bool lastRaw;
    uint32_t lastChange;
    uint32_t pressStart;
    bool longFired;
    bool clean;
};

Button buttons[NUM_BUTTONS];

constexpr uint8_t QUEUE_SIZE = 16;
InputEvent queueBuf[QUEUE_SIZE];
uint8_t qHead = 0, qTail = 0, qCount = 0;

void push(const InputEvent &ev) {
    if (qCount >= QUEUE_SIZE) {           // drop the oldest rather than block
        qHead = (qHead + 1) % QUEUE_SIZE;
        qCount--;
    }
    queueBuf[qTail] = ev;
    qTail = (qTail + 1) % QUEUE_SIZE;
    qCount++;
}

uint8_t pressedCountExcluding(uint8_t idx) {
    uint8_t n = 0;
    for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
        if (i != idx && buttons[i].stable) n++;
    }
    return n;
}

void debounce(uint8_t idx, bool raw, uint32_t now) {
    Button &b = buttons[idx];
    if (raw != b.lastRaw) {
        b.lastChange = now;
        b.lastRaw = raw;
    }
    if ((now - b.lastChange) >= DEBOUNCE_MS && raw != b.stable) {
        b.stable = raw;
        InputEvent ev;
        ev.button = idx + 1;
        ev.t = now;
        if (b.stable) {
            b.pressStart = now;
            b.longFired = false;
            b.clean = (pressedCountExcluding(idx) == 0);
            ev.action = ButtonAction::PRESS;
            ev.pressedAt = now;
            ev.clean = b.clean;
            ev.longFired = false;
        } else {
            ev.action = ButtonAction::RELEASE;
            ev.pressedAt = b.pressStart;
            ev.clean = b.clean;
            ev.longFired = b.longFired;
        }
        push(ev);
    } else if (b.stable && !b.longFired && (now - b.pressStart) >= LONG_PRESS_MS) {
        b.longFired = true;
        InputEvent ev;
        ev.button = idx + 1;
        ev.action = ButtonAction::LONG;
        ev.t = now;
        ev.pressedAt = b.pressStart;
        ev.clean = b.clean;
        ev.longFired = true;
        push(ev);
    }
}

// ---- Quadrature decoder (Ben Buxton's state table): one step per detent,
// bounce-immune because a step is only emitted after a full valid cycle.
constexpr uint8_t R_START = 0x0, R_CW_FINAL = 0x1, R_CW_BEGIN = 0x2, R_CW_NEXT = 0x3;
constexpr uint8_t R_CCW_BEGIN = 0x4, R_CCW_FINAL = 0x5, R_CCW_NEXT = 0x6;
constexpr uint8_t DIR_CW = 0x10, DIR_CCW = 0x20;

const uint8_t ENCODER_TTABLE[7][4] = {
    {R_START,    R_CW_BEGIN,  R_CCW_BEGIN, R_START},
    {R_CW_NEXT,  R_START,     R_CW_FINAL,  R_START | DIR_CW},
    {R_CW_NEXT,  R_CW_BEGIN,  R_START,     R_START},
    {R_CW_NEXT,  R_CW_BEGIN,  R_CW_FINAL,  R_START},
    {R_CCW_NEXT, R_START,     R_CCW_BEGIN, R_START},
    {R_CCW_NEXT, R_CCW_FINAL, R_START,     R_START | DIR_CCW},
    {R_CCW_NEXT, R_CCW_FINAL, R_CCW_BEGIN, R_START},
};

volatile uint8_t encoderState = R_START;
volatile int32_t encoderRawSteps = 0;
int32_t encoderConsumed = 0;

void IRAM_ATTR onEncoderChange() {
    uint8_t pins = (digitalRead(ENCODER_CLK_PIN) << 1) | digitalRead(ENCODER_DT_PIN);
    encoderState = ENCODER_TTABLE[encoderState & 0x0F][pins];
    uint8_t result = encoderState & 0x30;
    if (result == DIR_CW) encoderRawSteps++;
    else if (result == DIR_CCW) encoderRawSteps--;
}

}  // namespace

void inputSetup() {
    for (uint8_t r = 0; r < NUM_ROWS; r++) {
        pinMode(ROW_PINS[r], OUTPUT);
        digitalWrite(ROW_PINS[r], HIGH);
    }
    for (uint8_t c = 0; c < NUM_COLS; c++) {
        pinMode(COL_PINS[c], INPUT_PULLUP);
    }
    pinMode(ENCODER_SW_PIN, INPUT_PULLUP);
    pinMode(ENCODER_CLK_PIN, INPUT_PULLUP);
    pinMode(ENCODER_DT_PIN, INPUT_PULLUP);
    attachInterrupt(digitalPinToInterrupt(ENCODER_CLK_PIN), onEncoderChange, CHANGE);
    attachInterrupt(digitalPinToInterrupt(ENCODER_DT_PIN), onEncoderChange, CHANGE);

    for (uint8_t i = 0; i < NUM_BUTTONS; i++) {
        buttons[i] = {false, false, 0, 0, false, true};
    }
    qHead = qTail = qCount = 0;
}

void inputPoll() {
    uint32_t now = millis();
    for (uint8_t r = 0; r < NUM_ROWS; r++) {
        digitalWrite(ROW_PINS[r], LOW);
        delayMicroseconds(50);
        for (uint8_t c = 0; c < NUM_COLS; c++) {
            uint8_t idx = BUTTON_MAP[r][c] - 1;
            bool raw = (digitalRead(COL_PINS[c]) == LOW);
            debounce(idx, raw, now);
        }
        digitalWrite(ROW_PINS[r], HIGH);
    }
    bool sw = (digitalRead(ENCODER_SW_PIN) == LOW);
    debounce(ENCODER_SW_INDEX, sw, now);
}

bool inputPop(InputEvent &ev) {
    if (qCount == 0) return false;
    ev = queueBuf[qHead];
    qHead = (qHead + 1) % QUEUE_SIZE;
    qCount--;
    return true;
}

int32_t inputConsumeEncoderSteps() {
    noInterrupts();
    int32_t raw = encoderRawSteps;
    interrupts();
    int32_t delta = raw - encoderConsumed;
    encoderConsumed = raw;
    return delta;
}

int32_t inputEncoderRaw() {
    noInterrupts();
    int32_t raw = encoderRawSteps;
    interrupts();
    return raw;
}

bool inputIsPressed(uint8_t button) {
    if (button < 1 || button > NUM_BUTTONS) return false;
    return buttons[button - 1].stable;
}

uint8_t inputPressedCount() {
    uint8_t n = 0;
    for (uint8_t i = 0; i < NUM_BUTTONS; i++) if (buttons[i].stable) n++;
    return n;
}

uint32_t inputHeldFor(uint8_t button) {
    if (!inputIsPressed(button)) return 0;
    return millis() - buttons[button - 1].pressStart;
}
