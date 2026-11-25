import pytest

from atompulse.brands import brands_in, find_spans


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Atomberg Renesa review", ["Atomberg"]),
        ("ATOMBERG is great", ["Atomberg"]),
        ("atom   berg fan", ["Atomberg"]),
        ("Efficio vs Energion", ["Atomberg", "Crompton"]),
        ("Orient Electric Aeroquiet", ["Orient"]),
        ("एटमबर्ग का फैन", ["Atomberg"]),
        ("havels fan", ["Havells"]),
        ("Superfan and Versa Drives", ["Superfan"]),
    ],
)
def test_aliases_resolve_to_brand(text, expected):
    assert brands_in(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Orientation of the blades matters",  # 'orient' inside a word
        "I am a super fan of your channel",  # 'super fan' is not the Superfan brand
        "The ushas of dawn",  # 'usha' inside a word
        "bajajfinserv emi",  # no boundary
        "",
    ],
)
def test_no_false_brand(text):
    assert brands_in(text) == []


def test_longest_alias_wins_and_spans_do_not_overlap():
    spans = find_spans("Orient Electric fan")
    assert len(spans) == 1
    assert spans[0].alias == "orient electric"


def test_first_appearance_order():
    assert brands_in("Havells vs Atomberg vs Havells") == ["Havells", "Atomberg"]
