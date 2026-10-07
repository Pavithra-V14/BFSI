"""Entity extraction rules (spec 6.8): candidates, variants, normalisation, grouping."""

import pytest

from app.ingest.entities import (
    DEFAULT_COMPANY_NAMES,
    Candidate,
    extract_candidates,
    normalise_entity,
    person_variants,
    variants_of,
)
from app.ingest.model import DocRequest
from app.ingest.review import Row, build_groups

REQUEST = DocRequest("doc-a", "a.pdf", None, "policy", "customer", None, "2026-10-01")


def found(text, ner=lambda t: [], request=REQUEST, headings=()):
    return {
        (c.text, c.kind)
        for c in extract_candidates([(text, tuple(headings))], request, ner, DEFAULT_COMPANY_NAMES)
    }


def test_acts_and_regulations_are_found_without_lead_in_words_or_the_line_above():
    text = (
        "Section 1. Law\nUnder the Insurance Act, 1938 and the IRDAI (Protection of "
        "Policyholders) Regulations, 2017 apply. See the Act."
    )
    got = found(text)
    assert ("Insurance Act, 1938", "law") in got
    assert any(t.endswith("Regulations, 2017") and k == "law" for t, k in got)
    assert not any("Law Under" in t or t == "Act" for t, _ in got)


def test_a_sentence_boundary_stops_the_name_of_a_law():
    got = found("He agreed. The Consumer Protection Act, 2019 applies.")
    assert ("Consumer Protection Act, 2019", "law") in got


@pytest.mark.parametrize(
    ("text", "term", "kind"),
    [
        ("Write to care@auravance.aura today", "care@auravance.aura", "helpline"),
        ("Visit https://www.irdai.gov.in/page now", "https://www.irdai.gov.in/page", "helpline"),
        ("Call 1800-425-4000 any day", "1800-425-4000", "helpline"),
        ("Call +91 9876543210 for help", "+91 9876543210", "helpline"),
        ("IFSC code HDFC0001234 applies", "HDFC0001234", "product"),
        ("CIN U65110MH2026PTC384152 is listed", "U65110MH2026PTC384152", "product"),
        ("Plan code HLT-2026 is covered", "HLT-2026", "product"),
    ],
)
def test_pattern_extractors(text, term, kind):
    assert (term, kind) in found(text)


def test_seeded_regulators_and_the_company_name_are_found_only_when_present():
    got = found(
        "Regulated by IRDAI. AuraVance Digital FinTech Private Limited is the corporate agent."
    )
    assert ("IRDAI", "regulator") in got
    assert ("AuraVance Digital FinTech Private Limited", "organisation") in got
    assert ("SEBI", "regulator") not in got  # not in this text


def test_a_name_next_to_a_designation_is_a_person_without_the_lead_in_word():
    got = found("Contact Rahul Sharma, Chief Compliance Officer, for grievances.")
    assert ("Rahul Sharma", "person_in_policy") in got
    got = found("Dr. Anita Rao - Head of Claims leads the team.")
    assert ("Dr. Anita Rao", "person_in_policy") in got


def test_a_role_without_a_name_is_not_a_person():
    assert not any(k == "person_in_policy" for _, k in found("Contact Officer, Grievance Cell."))


def test_ner_labels_map_to_kinds_and_unknown_labels_are_ignored():

    def ner(text):
        return [
            ("Mumbai", "GPE"),
            ("Acme Ltd", "ORG"),
            ("12 March", "DATE"),
            ("Asha Rao", "PERSON"),
        ]

    got = found("Mumbai Acme Ltd 12 March Asha Rao", ner)
    assert ("Mumbai", "place") in got and ("Acme Ltd", "organisation") in got
    assert ("Asha Rao", "person_in_policy") in got and not any(t == "12 March" for t, _ in got)


def test_the_document_title_and_product_headings_are_product_candidates():
    request = DocRequest("doc-a", "a.pdf", "Flexi Property Cover", "policy", "customer", None, "d")
    got = found(
        "Some text.",
        request=request,
        headings=("Section 2. Gold Health Plan", "Section 3. Exclusions"),
    )
    assert ("Flexi Property Cover", "product") in got
    assert ("Gold Health Plan", "product") in got
    assert not any(t == "Exclusions" for t, _ in got)


def test_person_variants_follow_the_fixed_rules():
    assert [(v.text, v.single_word) for v in person_variants("Rahul Sharma")] == [
        ("Rahul Sharma", False),
        ("Mr. Sharma", False),
        ("R. Sharma", False),
        ("Sharma", True),
    ]
    titled = [v.text for v in person_variants("Dr. Anita Rao")]
    assert titled == ["Anita Rao", "Dr. Anita Rao", "Dr. Rao", "A. Rao", "Rao"]
    assert [v.text for v in person_variants("Madonna")] == ["Madonna"]
    assert person_variants("") == []


def test_only_people_get_variants():
    assert [v.text for v in variants_of(Candidate("IRDAI", "regulator", "", "seed"))] == ["IRDAI"]


def test_normalisation_is_exact_not_fuzzy():
    assert normalise_entity('  "Mr.  SHARMA", ') == "mr. sharma"
    assert normalise_entity("Ｉrdai") == "irdai"  # NFKC folds the full-width letter
    assert normalise_entity("Sharma") != normalise_entity("Sharmaa")  # one letter off: no match
    assert normalise_entity("R. Sharma") != normalise_entity("R Sharma")


def test_pending_rows_are_grouped_by_the_person_they_belong_to():
    rows = [
        Row("1", "Rahul Sharma", "person_in_policy"),
        Row("2", "Mr. Sharma", "person_in_policy"),
        Row("3", "Sharma", "person_in_policy"),
        Row("4", "HLT-2026", "product"),
        Row("5", "Dr. Rao", "person_in_policy"),
    ]
    groups = build_groups(rows)
    heads = {g.head: [r.id for r in g.rows] for g in groups}
    assert heads["Rahul Sharma"] == ["1", "2", "3"]
    assert heads["HLT-2026"] == ["4"] and heads["Dr. Rao"] == ["5"]
    sharma = next(g for g in groups if g.head == "Rahul Sharma")
    assert [sharma.single_word(r) for r in sharma.rows] == [False, False, True]


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("the Reserve Bank of India", "organisation", "Reserve Bank of India"),
        ("an Event of Default", "organisation", "Event of Default"),
        ("Associates | Chartered Accountants", "organisation", None),
        ("a current ratio of at least 1.20", "organisation", None),
        ("passu", "organisation", None),
        ("1.20", "organisation", None),
        ("Borrower", "person_in_policy", None),
        ("Rahul Sharma", "person_in_policy", "Rahul Sharma"),
        ("Mumbai", "place", "Mumbai"),
        ("w1 w2 w3 w4 w5 w6 w7 w8 w9", "organisation", None),
        ("  Acme   Ltd ", "organisation", "Acme Ltd"),
    ],
)
def test_ner_hits_that_are_clearly_not_names_are_dropped(text, kind, expected):
    from app.ingest.entities import clean_ner_text

    assert clean_ner_text(text, kind) == expected
