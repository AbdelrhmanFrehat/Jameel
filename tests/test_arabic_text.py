"""The firmware's Arabic shaping/bidi algorithm (Python twin in tools/arabic_text.py)
checked against the arabic_reshaper + python-bidi reference implementations."""

import pytest

arabic_reshaper = pytest.importorskip("arabic_reshaper")
bidi_algorithm = pytest.importorskip("bidi.algorithm")

from tools import arabic_text as A

SENTENCES = [
    "هل تريد نشر النسخة إلى الإنتاج؟",
    "اختر قاعدة البيانات",
    "نعم",
    "لا",
    "استخدم PostgreSQL مع Redis",
    "تم تعديل الملف PatientService.cs بنجاح",
    "الإصدار v2 جاهز",
    "أضف 3 اختبارات جديدة",
    "هل أكمل العمل؟ (المهمة 2 من 5)",
    "كلمة السر: abc123",
    "مرحباً بك في claude code",
    "السلام عليكم ورحمة الله",
    "الآن يمكنك الإجابة بالعربية",
    "إلغاء",
    "B1 نعم",
    "Deploy الآن?",
    "لأنّ الجمعة إجازة",
    "شكراً جزيلاً",
]


# The firmware draws "الله" letter by letter (no special ALLAH ligature glyph);
# everything else follows arabic_reshaper's defaults (Lam-Alef ligatures on,
# harakat removed).
RESHAPER = arabic_reshaper.ArabicReshaper(configuration={"ARABIC LIGATURE ALLAH": False})


def reference_shape(text: str) -> str:
    return RESHAPER.reshape(text)


def reference_visual(text: str, base: str) -> str:
    return bidi_algorithm.get_display(reference_shape(text), base_dir=base)


@pytest.mark.parametrize("text", SENTENCES)
def test_shaping_matches_reference_in_logical_order(text):
    ours = "".join(chr(c) for c in A.shape(text))
    ref = reference_shape(text)
    assert [hex(ord(c)) for c in ours] == [hex(ord(c)) for c in ref]


@pytest.mark.parametrize("text", SENTENCES)
def test_visual_order_matches_reference(text):
    base = A.base_direction(text)
    ours = A.to_visual(text)
    ref = reference_visual(text, base)
    assert [hex(ord(c)) for c in ours] == [hex(ord(c)) for c in ref], (ours, ref)


def test_helpers():
    assert A.has_arabic("نعم") and not A.has_arabic("yes 123")
    assert A.base_direction("B1 نعم") == "L" and A.base_direction("نعم B1") == "R"
    assert A.wrap_logical("اختر قاعدة البيانات المناسبة للمشروع", 12) == ["اختر قاعدة", "البيانات", "المناسبة", "للمشروع"]
    assert A.wrap_logical("abc\n\ndef", 10) == ["abc", "", "def"]
    long_word = "ب" * 30
    assert [len(l) for l in A.wrap_logical(long_word, 10)] == [10, 10, 10]
    assert len(A.shape("لا")) == 1   # ligature counts as one cell
