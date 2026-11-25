import pytest

from fanvaaz.brands import brands_in, find_spans


@pytest.mark.parametrize(
    "text, expected",
    [
        ("OnePlus Nord review", ["OnePlus"]),
        ("ONEPLUS is great", ["OnePlus"]),
        ("one   plus phone", ["OnePlus"]),
        ("Nord CE vs Redmi", ["OnePlus", "Xiaomi"]),
        ("Realme Narzo 70 Turbo", ["Realme"]),
        ("वनप्लस का फैन", ["OnePlus"]),
        ("galaxy phone", ["Samsung"]),
        ("Pixel and Moto Edge", ["Pixel", "Motorola"]),
    ],
)
def test_aliases_resolve_to_brand(text, expected):
    assert brands_in(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Pixelated video at night",  # 'pixel' inside a word
        "Applesauce recipe",  # 'apple' inside a word
        "The lavatory was clean",  # 'lava' inside a word
        "oppofinance emi",  # no boundary
        "",
    ],
)
def test_no_false_brand(text):
    assert brands_in(text) == []


def test_longest_alias_wins_and_spans_do_not_overlap():
    spans = find_spans("Realme Narzo phone")
    assert len(spans) == 1
    assert spans[0].alias == "realme narzo"


def test_first_appearance_order():
    assert brands_in("Samsung vs OnePlus vs Samsung") == ["Samsung", "OnePlus"]
