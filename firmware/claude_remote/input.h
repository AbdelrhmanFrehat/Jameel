// ============================================================================
//  Hardware input: 2x4 button matrix (B1-B8), encoder switch (B9), encoder
//  rotation (interrupt driven quadrature decoder).
//
//  Produces a queue of InputEvents; never blocks; ~30 ms debounce; exactly one
//  PRESS and one RELEASE per physical press; LONG once when held past
//  LONG_PRESS_MS. `clean` is true when no other button was down at the moment
//  of the press - decision screens require clean events (matrix ghosting).
// ============================================================================
#pragma once

#include "config.h"

enum class ButtonAction : uint8_t { PRESS, RELEASE, LONG };

struct InputEvent {
    uint8_t button;        // 1..9
    ButtonAction action;
    uint32_t t;            // millis() of the event
    uint32_t pressedAt;    // millis() when the physical press began
    bool clean;            // no other button held when the press began
    bool longFired;        // for RELEASE: a LONG event already fired for this press
};

void inputSetup();
void inputPoll();                          // call every loop iteration
bool inputPop(InputEvent &ev);             // dequeue one event, false if empty
int32_t inputConsumeEncoderSteps();        // detents since last call (+cw / -ccw)
int32_t inputEncoderRaw();                 // cumulative raw steps (diagnostics)
bool inputIsPressed(uint8_t button);       // debounced state, 1..9
uint8_t inputPressedCount();
uint32_t inputHeldFor(uint8_t button);     // ms the button has been held, 0 if not pressed
