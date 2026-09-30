#include "arabic.h"

#include <string.h>

namespace {

// base code point -> isolated, final, initial, medial (0 = form does not exist)
struct Forms { uint16_t base, iso, fin, ini, med; };

const Forms FORMS[] = {
    {0x0621, 0xFE80, 0xFE80, 0, 0},           // ء
    {0x0622, 0xFE81, 0xFE82, 0, 0},           // آ
    {0x0623, 0xFE83, 0xFE84, 0, 0},           // أ
    {0x0624, 0xFE85, 0xFE86, 0, 0},           // ؤ
    {0x0625, 0xFE87, 0xFE88, 0, 0},           // إ
    {0x0626, 0xFE89, 0xFE8A, 0xFE8B, 0xFE8C}, // ئ
    {0x0627, 0xFE8D, 0xFE8E, 0, 0},           // ا
    {0x0628, 0xFE8F, 0xFE90, 0xFE91, 0xFE92}, // ب
    {0x0629, 0xFE93, 0xFE94, 0, 0},           // ة
    {0x062A, 0xFE95, 0xFE96, 0xFE97, 0xFE98}, // ت
    {0x062B, 0xFE99, 0xFE9A, 0xFE9B, 0xFE9C}, // ث
    {0x062C, 0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0}, // ج
    {0x062D, 0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4}, // ح
    {0x062E, 0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8}, // خ
    {0x062F, 0xFEA9, 0xFEAA, 0, 0},           // د
    {0x0630, 0xFEAB, 0xFEAC, 0, 0},           // ذ
    {0x0631, 0xFEAD, 0xFEAE, 0, 0},           // ر
    {0x0632, 0xFEAF, 0xFEB0, 0, 0},           // ز
    {0x0633, 0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4}, // س
    {0x0634, 0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8}, // ش
    {0x0635, 0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC}, // ص
    {0x0636, 0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0}, // ض
    {0x0637, 0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4}, // ط
    {0x0638, 0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8}, // ظ
    {0x0639, 0xFEC9, 0xFECA, 0xFECB, 0xFECC}, // ع
    {0x063A, 0xFECD, 0xFECE, 0xFECF, 0xFED0}, // غ
    {0x0641, 0xFED1, 0xFED2, 0xFED3, 0xFED4}, // ف
    {0x0642, 0xFED5, 0xFED6, 0xFED7, 0xFED8}, // ق
    {0x0643, 0xFED9, 0xFEDA, 0xFEDB, 0xFEDC}, // ك
    {0x0644, 0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0}, // ل
    {0x0645, 0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4}, // م
    {0x0646, 0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8}, // ن
    {0x0647, 0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC}, // ه
    {0x0648, 0xFEED, 0xFEEE, 0, 0},           // و
    {0x0649, 0xFEEF, 0xFEF0, 0, 0},           // ى
    {0x064A, 0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4}, // ي
    {0x067E, 0xFB56, 0xFB57, 0xFB58, 0xFB59}, // پ
    {0x0686, 0xFB7A, 0xFB7B, 0xFB7C, 0xFB7D}, // چ
    {0x0698, 0xFB8A, 0xFB8B, 0, 0},           // ژ
    {0x06A9, 0xFB8E, 0xFB8F, 0xFB90, 0xFB91}, // ک
    {0x06AF, 0xFB92, 0xFB93, 0xFB94, 0xFB95}, // گ
    {0x06CC, 0xFBFC, 0xFBFD, 0xFBFE, 0xFBFF}, // ی
};
constexpr uint8_t FORMS_N = sizeof(FORMS) / sizeof(FORMS[0]);

struct LamAlef { uint16_t alef, iso, fin; };
const LamAlef LAM_ALEF[] = {
    {0x0622, 0xFEF5, 0xFEF6}, {0x0623, 0xFEF7, 0xFEF8}, {0x0625, 0xFEF9, 0xFEFA}, {0x0627, 0xFEFB, 0xFEFC},
};

constexpr uint16_t ZWNJ = 0x200C;

const Forms *findForms(uint16_t cp) {
    if (cp < 0x0621 || cp > 0x06CC) return nullptr;
    for (uint8_t i = 0; i < FORMS_N; i++) {
        if (FORMS[i].base == cp) return &FORMS[i];
    }
    return nullptr;
}

const LamAlef *findLamAlef(uint16_t cp) {
    for (uint8_t i = 0; i < 4; i++) {
        if (LAM_ALEF[i].alef == cp) return &LAM_ALEF[i];
    }
    return nullptr;
}

bool isTashkeel(uint16_t cp) {
    return (cp >= 0x064B && cp <= 0x0652) || cp == 0x0670 || cp == 0x0640;
}

bool isLtr(uint16_t cp) {
    return (cp >= 'A' && cp <= 'Z') || (cp >= 'a' && cp <= 'z') || (cp >= 0x00C0 && cp <= 0x024F) ||
           (cp >= '0' && cp <= '9') || (cp >= 0x0660 && cp <= 0x0669) || (cp >= 0x06F0 && cp <= 0x06F9);
}

uint16_t mirror(uint16_t cp) {
    switch (cp) {
        case '(': return ')'; case ')': return '(';
        case '[': return ']'; case ']': return '[';
        case '{': return '}'; case '}': return '{';
        case '<': return '>'; case '>': return '<';
        default: return cp;
    }
}

}  // namespace

// ---- UTF-8 -----------------------------------------------------------------------

uint16_t utf8Decode(const char *s, uint16_t *out, uint16_t maxOut) {
    uint16_t n = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (*p && n < maxOut) {
        unsigned char c = *p;
        uint32_t cp;
        uint8_t extra;
        if (c < 0x80) { cp = c; extra = 0; }
        else if ((c & 0xE0) == 0xC0) { cp = c & 0x1F; extra = 1; }
        else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; extra = 2; }
        else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; extra = 3; }
        else { p++; continue; }                       // stray continuation byte
        bool ok = true;
        for (uint8_t i = 0; i < extra; i++) {
            if ((p[i + 1] & 0xC0) != 0x80) { ok = false; break; }
            cp = (cp << 6) | (p[i + 1] & 0x3F);
        }
        if (!ok) { p++; continue; }
        p += extra + 1;
        if (cp > 0xFFFF) cp = '?';                   // outside the BMP: not in our font
        out[n++] = (uint16_t)cp;
    }
    return n;
}

uint8_t utf8Encode(uint16_t cp, char *dst, size_t dstSize) {
    uint8_t need = cp < 0x80 ? 1 : cp < 0x800 ? 2 : 3;
    if (dstSize < (size_t)need + 1) return 0;
    if (need == 1) {
        dst[0] = (char)cp;
    } else if (need == 2) {
        dst[0] = (char)(0xC0 | (cp >> 6));
        dst[1] = (char)(0x80 | (cp & 0x3F));
    } else {
        dst[0] = (char)(0xE0 | (cp >> 12));
        dst[1] = (char)(0x80 | ((cp >> 6) & 0x3F));
        dst[2] = (char)(0x80 | (cp & 0x3F));
    }
    dst[need] = '\0';
    return need;
}

size_t utf8EncodeAll(const uint16_t *cps, uint16_t n, char *dst, size_t dstSize) {
    size_t o = 0;
    if (dstSize == 0) return 0;
    for (uint16_t i = 0; i < n; i++) {
        uint8_t w = utf8Encode(cps[i], dst + o, dstSize - o);
        if (w == 0) break;
        o += w;
    }
    dst[o] = '\0';
    return o;
}

uint16_t utf8Length(const char *s) {
    uint16_t n = 0;
    for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
        if ((*p & 0xC0) != 0x80) n++;
    }
    return n;
}

size_t utf8ByteLen(const char *s, uint16_t n) {
    size_t o = 0;
    uint16_t seen = 0;
    const unsigned char *p = (const unsigned char *)s;
    while (p[o]) {
        if ((p[o] & 0xC0) != 0x80) {
            if (seen == n) break;
            seen++;
        }
        o++;
    }
    return o;
}

void utf8PopLast(char *s) {
    size_t len = strlen(s);
    if (len == 0) return;
    size_t i = len - 1;
    while (i > 0 && (((unsigned char)s[i]) & 0xC0) == 0x80) i--;
    s[i] = '\0';
}

// ---- Classification ----------------------------------------------------------------

bool arabicIsLetter(uint16_t cp) { return findForms(cp) != nullptr; }

bool arabicIsRtl(uint16_t cp) {
    return (cp >= 0x0621 && cp <= 0x064A) || (cp >= 0x066E && cp <= 0x06D3) ||
           (cp >= 0xFB50 && cp <= 0xFDFF) || (cp >= 0xFE70 && cp <= 0xFEFC) || findForms(cp) != nullptr;
}

bool textHasArabic(const char *utf8) {
    uint16_t buf[64];
    const char *p = utf8;
    while (*p) {
        uint16_t n = utf8Decode(p, buf, 64);
        if (n == 0) break;
        for (uint16_t i = 0; i < n; i++) if (arabicIsRtl(buf[i])) return true;
        p += utf8ByteLen(p, n);
    }
    return false;
}

char textBaseDirection(const char *utf8) {
    uint16_t buf[64];
    const char *p = utf8;
    while (*p) {
        uint16_t n = utf8Decode(p, buf, 64);
        if (n == 0) break;
        for (uint16_t i = 0; i < n; i++) {
            if (arabicIsRtl(buf[i])) return 'R';
            if (isLtr(buf[i])) return 'L';
        }
        p += utf8ByteLen(p, n);
    }
    return 'L';
}

// ---- Shaping --------------------------------------------------------------------------

uint16_t arabicShape(const uint16_t *in, uint16_t n, uint16_t *out, uint16_t maxOut) {
    // 1. drop tashkeel into a compact logical buffer (works in place)
    static uint16_t cps[ARABIC_MAX_CPS];
    uint16_t m = 0;
    for (uint16_t i = 0; i < n && m < ARABIC_MAX_CPS; i++) {
        if (!isTashkeel(in[i])) cps[m++] = in[i];
    }
    uint16_t o = 0;
    for (uint16_t i = 0; i < m && o < maxOut; i++) {
        uint16_t cp = cps[i];
        const Forms *f = findForms(cp);
        if (!f) { out[o++] = cp; continue; }

        // does the previous letter connect forward to us?
        bool prevJoins = false;
        if (i > 0) {
            uint16_t p = cps[i - 1];
            const Forms *pf = (p == ZWNJ) ? nullptr : findForms(p);
            prevJoins = pf && pf->ini != 0;
        }
        // Lam-Alef ligature
        if (cp == 0x0644 && i + 1 < m) {
            const LamAlef *la = findLamAlef(cps[i + 1]);
            if (la) {
                out[o++] = prevJoins ? la->fin : la->iso;
                i++;   // consume the Alef
                continue;
            }
        }
        bool nextJoins = (i + 1 < m) && findForms(cps[i + 1]) != nullptr;
        bool dual = f->ini != 0;
        if (prevJoins && nextJoins && dual) out[o++] = f->med;
        else if (prevJoins) out[o++] = f->fin;
        else if (nextJoins && dual) out[o++] = f->ini;
        else out[o++] = f->iso;
    }
    return o;
}

// ---- Bidi (one line) -------------------------------------------------------------------

uint16_t arabicReorder(const uint16_t *in, uint16_t n, uint16_t *out, uint16_t maxOut, char base) {
    if (n == 0) return 0;
    if (n > ARABIC_MAX_CPS) n = ARABIC_MAX_CPS;
    static char types[ARABIC_MAX_CPS];
    for (uint16_t i = 0; i < n; i++) {
        types[i] = arabicIsRtl(in[i]) ? 'R' : (isLtr(in[i]) ? 'L' : 'N');
    }
    if (base != 'R' && base != 'L') {
        base = 'L';
        for (uint16_t i = 0; i < n; i++) if (types[i] != 'N') { base = types[i]; break; }
    }
    // resolve neutrals: same type as both neighbours, else base direction
    uint16_t i = 0;
    while (i < n) {
        if (types[i] != 'N') { i++; continue; }
        uint16_t j = i;
        while (j < n && types[j] == 'N') j++;
        char left = i > 0 ? types[i - 1] : base;
        char right = j < n ? types[j] : base;
        char t = (left == right) ? left : base;
        for (uint16_t k = i; k < j; k++) types[k] = t;
        i = j;
    }
    // emit runs
    uint16_t o = 0;
    if (base == 'R') {
        // walk runs from the end
        uint16_t end = n;
        while (end > 0 && o < maxOut) {
            uint16_t start = end;
            while (start > 0 && types[start - 1] == types[end - 1]) start--;
            if (types[end - 1] == 'R') {
                for (uint16_t k = end; k > start && o < maxOut; k--) out[o++] = mirror(in[k - 1]);
            } else {
                for (uint16_t k = start; k < end && o < maxOut; k++) out[o++] = in[k];
            }
            end = start;
        }
    } else {
        uint16_t start = 0;
        while (start < n && o < maxOut) {
            uint16_t end = start;
            while (end < n && types[end] == types[start]) end++;
            if (types[start] == 'R') {
                for (uint16_t k = end; k > start && o < maxOut; k--) out[o++] = mirror(in[k - 1]);
            } else {
                for (uint16_t k = start; k < end && o < maxOut; k++) out[o++] = in[k];
            }
            start = end;
        }
    }
    return o;
}

uint16_t arabicToVisual(const char *utf8, char base, char *dst, size_t dstSize) {
    static uint16_t a[ARABIC_MAX_CPS];
    static uint16_t b[ARABIC_MAX_CPS];
    uint16_t n = utf8Decode(utf8, a, ARABIC_MAX_CPS);
    n = arabicShape(a, n, b, ARABIC_MAX_CPS);
    n = arabicReorder(b, n, a, ARABIC_MAX_CPS, base);
    utf8EncodeAll(a, n, dst, dstSize);
    return n;
}

uint16_t arabicCellCount(const char *utf8) {
    static uint16_t a[ARABIC_MAX_CPS];
    static uint16_t b[ARABIC_MAX_CPS];
    uint16_t n = utf8Decode(utf8, a, ARABIC_MAX_CPS);
    return arabicShape(a, n, b, ARABIC_MAX_CPS);
}
