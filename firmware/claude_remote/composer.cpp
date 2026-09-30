#include "composer.h"

#include <string.h>

#include "arabic.h"

namespace {

const char SET_LOWER_CHARS[]   = " abcdefghijklmnopqrstuvwxyz";
const char SET_UPPER_CHARS[]   = " ABCDEFGHIJKLMNOPQRSTUVWXYZ";
const char SET_DIGIT_CHARS[]   = " 0123456789";
const char SET_SYMBOL_CHARS[]  = " -_./\\:@#$%&*+=()[]{}<>,?!'\";|~^`";

// Arabic letters in alphabetical order, then hamza forms and punctuation.
const uint16_t SET_ARABIC_CPS[] = {
    ' ',
    0x0627, 0x0628, 0x062A, 0x062B, 0x062C, 0x062D, 0x062E, 0x062F, 0x0630, 0x0631, 0x0632, 0x0633, 0x0634,
    0x0635, 0x0636, 0x0637, 0x0638, 0x0639, 0x063A, 0x0641, 0x0642, 0x0643, 0x0644, 0x0645, 0x0646, 0x0647,
    0x0648, 0x064A,
    0x0629, 0x0649, 0x0621, 0x0623, 0x0625, 0x0622, 0x0624, 0x0626,
    0x060C, 0x061F, 0x061B,
};
constexpr uint16_t SET_ARABIC_N = sizeof(SET_ARABIC_CPS) / sizeof(SET_ARABIC_CPS[0]);

const char *charSet(uint8_t set) {
    switch (set) {
        case SET_LOWER:   return SET_LOWER_CHARS;
        case SET_UPPER:   return SET_UPPER_CHARS;
        case SET_DIGITS:  return SET_DIGIT_CHARS;
        case SET_SYMBOLS: return SET_SYMBOL_CHARS;
        default:          return "";
    }
}

int16_t wrapIndex(int32_t idx, uint16_t size) {
    if (size == 0) return 0;
    int32_t m = idx % (int32_t)size;
    if (m < 0) m += size;
    return (int16_t)m;
}

// Code point of the item at idx for the single-character sets.
uint16_t itemCp(const TextComposerState &c, int16_t idx) {
    if (c.set == SET_ARABIC) return SET_ARABIC_CPS[idx];
    return (uint16_t)(unsigned char)charSet(c.set)[idx];
}

bool appendCp(TextComposerState &c, uint16_t cp) {
    if (c.chars >= c.maxChars) return false;
    char tmp[4];
    uint8_t w = utf8Encode(cp, tmp, sizeof(tmp));
    if (w == 0 || c.len + w >= sizeof(c.text)) return false;
    memcpy(&c.text[c.len], tmp, w);
    c.len += w;
    c.text[c.len] = '\0';
    c.chars++;
    return true;
}

}  // namespace

void composerBegin(TextComposerState &c, uint16_t maxChars,
                   const char tokens[][MAX_SUGGESTION_BYTES], uint8_t tokenCount) {
    c.text[0] = '\0';
    c.len = 0;
    c.chars = 0;
    c.maxChars = (maxChars == 0 || maxChars > MAX_TEXT_ANSWER) ? MAX_TEXT_ANSWER : maxChars;
    c.set = SET_LOWER;
    c.index = 1;   // start on 'a', not on space
    c.tokenCount = 0;
    for (uint8_t i = 0; i < tokenCount && i < MAX_SUGGESTIONS; i++) {
        if (!tokens[i][0]) continue;
        strlcpy(c.tokens[c.tokenCount], tokens[i], sizeof(c.tokens[0]));
        c.tokenCount++;
    }
}

uint16_t composerSetSize(const TextComposerState &c) {
    if (c.set == SET_WORDS) return c.tokenCount;
    if (c.set == SET_ARABIC) return SET_ARABIC_N;
    return (uint16_t)strlen(charSet(c.set));
}

bool composerSetIsArabic(const TextComposerState &c) { return c.set == SET_ARABIC; }

void composerRotate(TextComposerState &c, int32_t delta) {
    uint16_t size = composerSetSize(c);
    if (size == 0) return;
    c.index = wrapIndex((int32_t)c.index + delta, size);
}

void composerItemAt(const TextComposerState &c, int16_t offset, char *out, size_t outSize) {
    if (outSize == 0) return;
    out[0] = '\0';
    uint16_t size = composerSetSize(c);
    if (size == 0) return;
    int16_t idx = wrapIndex((int32_t)c.index + offset, size);
    if (c.set == SET_WORDS) {
        strlcpy(out, c.tokens[idx], outSize);
        return;
    }
    uint16_t cp = itemCp(c, idx);
    if (cp == ' ') {
        strlcpy(out, "SPC", outSize);
    } else {
        utf8Encode(cp, out, outSize);
    }
}

bool composerInsertCurrent(TextComposerState &c) {
    uint16_t size = composerSetSize(c);
    if (size == 0) return false;
    int16_t idx = wrapIndex(c.index, size);
    if (c.set == SET_WORDS) {
        return composerInsertWord(c, c.tokens[idx]);
    }
    return appendCp(c, itemCp(c, idx));
}

bool composerInsertWord(TextComposerState &c, const char *word) {
    if (!word || !word[0]) return false;
    size_t wl = strlen(word);
    uint16_t wchars = utf8Length(word);
    bool needSpace = (c.len > 0 && c.text[c.len - 1] != ' ');
    size_t needed = wl + (needSpace ? 1 : 0);
    if (c.chars + wchars + (needSpace ? 1 : 0) > c.maxChars) return false;
    if (c.len + needed >= sizeof(c.text)) return false;
    if (needSpace) { c.text[c.len++] = ' '; c.chars++; }
    memcpy(&c.text[c.len], word, wl);
    c.len += wl;
    c.chars += wchars;
    c.text[c.len] = '\0';
    return true;
}

void composerBackspace(TextComposerState &c) {
    if (c.len == 0) return;
    utf8PopLast(c.text);
    c.len = strlen(c.text);
    if (c.chars) c.chars--;
}

void composerClear(TextComposerState &c) {
    c.len = 0;
    c.chars = 0;
    c.text[0] = '\0';
}

void composerNextSet(TextComposerState &c) {
    uint8_t next = c.set;
    for (uint8_t i = 0; i < COMPOSER_SETS; i++) {
        next = (next + 1) % COMPOSER_SETS;
        if (next == SET_WORDS && c.tokenCount == 0) continue;
        break;
    }
    c.set = next;
    c.index = (c.set == SET_WORDS) ? 0 : 1;
}

const char *composerSetName(const TextComposerState &c) {
    switch (c.set) {
        case SET_LOWER:   return "abc";
        case SET_UPPER:   return "ABC";
        case SET_DIGITS:  return "123";
        case SET_SYMBOLS: return "#!?";
        case SET_ARABIC:  return "ARABIC";
        case SET_WORDS:   return "WORDS";
        default:          return "?";
    }
}
