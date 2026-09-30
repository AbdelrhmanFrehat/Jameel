// ============================================================================
//  Free-text composer driven by the rotary encoder.
//
//  Character sets: lowercase, UPPERCASE, digits, symbols, ARABIC, WORDS
//  (tokens sent by the host per question). Encoder rotation moves the pick
//  index inside the current set, B9 inserts, B6 deletes one character (any
//  UTF-8 length), B7 cycles sets, B8 sends. Append-only UTF-8 buffer with a
//  hard maximum in characters (<= 256) and in bytes.
// ============================================================================
#pragma once

#include "config.h"

constexpr uint8_t COMPOSER_SETS = 6;
enum ComposerSet : uint8_t { SET_LOWER = 0, SET_UPPER, SET_DIGITS, SET_SYMBOLS, SET_ARABIC, SET_WORDS };

struct TextComposerState {
    char text[MAX_TEXT_ANSWER_BYTES];
    uint16_t len;        // bytes used
    uint16_t chars;      // code points
    uint16_t maxChars;
    uint8_t set;
    int16_t index;
    char tokens[MAX_SUGGESTIONS][MAX_SUGGESTION_BYTES];
    uint8_t tokenCount;
};

void composerBegin(TextComposerState &c, uint16_t maxChars,
                   const char tokens[][MAX_SUGGESTION_BYTES], uint8_t tokenCount);
void composerRotate(TextComposerState &c, int32_t delta);
bool composerInsertCurrent(TextComposerState &c);            // false when full
bool composerInsertWord(TextComposerState &c, const char *word);  // adds a separating space when needed
void composerBackspace(TextComposerState &c);
void composerClear(TextComposerState &c);
void composerNextSet(TextComposerState &c);
const char *composerSetName(const TextComposerState &c);
uint16_t composerSetSize(const TextComposerState &c);
bool composerSetIsArabic(const TextComposerState &c);
// Item at `offset` positions from the current pick (wraps), as UTF-8. Space renders as "SPC".
void composerItemAt(const TextComposerState &c, int16_t offset, char *out, size_t outSize);
