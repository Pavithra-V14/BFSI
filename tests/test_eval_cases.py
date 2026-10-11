"""The evaluation set and its rules (spec 15.2, 20; owner additions 5 and c)."""

import json
from collections import Counter

import pytest

from app.eval import cases as cs
from app.eval import constants as c
from app.eval.cases import Case, Op, Source
from app.eval.corpus import load_manifest, sha256_of, verify_files


@pytest.fixture(scope="module")
def loaded():
    return cs.load_cases()


def make(**overrides):
    values = {
        "id": "x01-sample",
        "category": "factual",
        "role": "customer",
        "turns": ("What stamp duty applies when I buy units?",),
        "behaviour": "answer",
        "sources": (Source("mf-ecas-servicing", "2", "Stamp duty of 0.005 per cent"),),
        "facts": ("0.005 per cent",),
        "reference": "Stamp duty of 0.005 per cent applies to purchases.",
        "source_docs": ("mf-ecas-servicing",),
    }
    values.update(overrides)
    return Case(**values)


# ================================================================================ the set ====
def test_the_set_is_sixty_cases_twenty_active_and_every_category_is_active(loaded):
    _version, found = loaded
    assert cs.set_problems(found) == []
    assert len(found) == 60 and sum(1 for x in found if x.active) == 20
    active = {x.category for x in found if x.active}
    assert active == set(c.CATEGORIES)  # at least one active case in every category


def test_every_category_of_the_spec_has_cases(loaded):
    _version, found = loaded
    counts = Counter(x.category for x in found)
    assert set(counts) == set(c.CATEGORIES)
    assert all(n >= 2 for n in counts.values())


def test_case_ids_are_unique_and_stable_looking(loaded):
    _version, found = loaded
    ids = [x.id for x in found]
    assert len(ids) == len(set(ids))
    assert all(cs.ID_PATTERN.match(i) for i in ids)


def test_an_answer_case_names_its_source_by_clause_reference_and_anchor(loaded):
    _version, found = loaded
    for case in found:
        if case.behaviour == "answer":
            assert case.sources and all(s.clause_ref and s.anchor for s in case.sources), case.id
            assert case.facts and case.reference, case.id


def test_expected_sources_are_never_chunk_ids(loaded):
    _version, found = loaded
    for case in found:
        for source in case.sources:
            assert "-" not in source.clause_ref or source.clause_ref.startswith(("Table", "Fig"))
            assert not any(len(part) >= 32 and part.isalnum() for part in source.clause_ref.split())


def test_the_catalogue_of_active_cases_is_what_the_owner_must_review(loaded):
    _version, found = loaded
    assert {x.id for x in found if x.review} == {x.id for x in found if x.active}


# ==================================================================== owner addition (c) ====
def test_every_source_document_is_in_the_sample_corpus_manifest(loaded):
    _version, found = loaded
    manifest = set(load_manifest())
    for case in found:
        named = (
            set(case.source_docs)
            | {s.doc_id for s in case.sources}
            | {o.doc_id for o in case.setup}
        )
        assert named <= manifest, case.id


def test_a_case_naming_a_document_outside_the_manifest_is_refused():
    bad = make(
        source_docs=("a-customer-contract",), sources=(Source("a-customer-contract", "1", "x"),)
    )
    problems = cs.case_problems(bad)
    assert any("not in the sample corpus manifest" in p for p in problems)


def test_a_setup_operation_may_not_touch_a_document_outside_the_manifest():
    bad = make(setup=(Op("withdraw_document", "some-live-document"),))
    assert any("manifest" in p for p in cs.case_problems(bad))


def test_a_review_candidate_may_name_a_live_document_but_cannot_join_the_set():
    candidate = make(
        id="rq-abc", status="candidate", behaviour="answer", sources=(), facts=(), reference="",
        source_docs=("a-live-document",),
    )  # fmt: skip
    assert cs.case_problems(candidate) == []
    promoted = make(
        id="rq-abc", status="set", behaviour="refuse", sources=(), facts=(), reference="",
        source_docs=("a-live-document",),
    )  # fmt: skip
    assert any("manifest" in p for p in cs.case_problems(promoted))


def test_the_manifest_files_are_the_listed_files():
    assert verify_files() == []


def test_a_changed_file_is_noticed(tmp_path):
    manifest = load_manifest()
    doc = next(iter(manifest.values()))
    (tmp_path / "spec" / "samples" / "company").mkdir(parents=True)
    fake = tmp_path / doc.file
    fake.write_bytes(b"not the sample document")
    assert doc.doc_id in verify_files(manifest, root=tmp_path)
    assert sha256_of(fake) != doc.sha256


# ================================================================ no personal data (5) ====
def test_no_question_outside_the_pii_category_looks_like_personal_data(loaded):
    _version, found = loaded
    for case in found:
        if case.category != "pii":
            assert not any(cs.scan_text(turn) for turn in case.turns), case.id


def test_a_question_with_a_pan_is_refused_outside_the_pii_category():
    bad = make(turns=("My PAN is ABCDE1234F, what is the stamp duty?",))
    assert any("personal data" in p for p in cs.case_problems(bad))
    ok = make(category="pii", turns=("My PAN is ABCDE1234F, what is the stamp duty?",))
    assert cs.case_problems(ok) == []


def test_pii_cases_hold_made_up_values_that_the_answer_must_not_repeat(loaded):
    _version, found = loaded
    pii = [x for x in found if x.category == "pii"]
    assert len(pii) >= 5
    for case in pii:
        assert case.forbidden, case.id
        assert all(f in " ".join(case.turns) for f in case.forbidden), case.id


# ============================================================================ the rules ====
@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"category": "nonsense"}, "category"),
        ({"role": "admin"}, "role"),
        ({"behaviour": "shrug"}, "behaviour"),
        ({"turns": ()}, "no question"),
        ({"turns": ("x" * 501,)}, "too long"),
        ({"sources": ()}, "expected source"),
        ({"facts": ()}, "facts"),
        ({"reference": ""}, "reference"),
        ({"sources": (Source("mf-ecas-servicing", "2", ""),)}, "anchor"),
        ({"id": "Bad Id"}, "id"),
        ({"category": "multi_turn"}, "two turns"),
    ],
)
def test_each_rule_of_a_case_is_enforced(overrides, needle):
    problems = cs.case_problems(make(**overrides))
    assert any(needle in p for p in problems), problems


def test_a_set_of_the_wrong_size_or_with_a_category_missing_is_refused(loaded):
    _version, found = loaded
    assert any("not 60" in p for p in cs.set_problems(found[:-1]))
    no_injection = [x for x in found if x.category != "injection"]
    assert any("injection" in p for p in cs.set_problems(no_injection))
    inactive = [
        Case(**{**x.__dict__, "active": False}) if x.category == "greeting" else x for x in found
    ]
    assert any("no active case for: greeting" in p for p in cs.set_problems(inactive))


def test_a_duplicate_id_is_refused(loaded):
    _version, found = loaded
    doubled = [*found[:-1], Case(**{**found[-1].__dict__, "id": found[0].id})]
    assert any("used twice" in p for p in cs.set_problems(doubled))


def test_a_setup_operation_must_name_a_turn_the_case_has():
    bad = make(setup=(Op("withdraw_document", "mf-ecas-servicing", before_turn=3),))
    assert any("turn" in p for p in cs.case_problems(bad))
    bad = make(setup=(Op("edit_text", "mf-ecas-servicing"),))
    assert any("edit needs" in p for p in cs.case_problems(bad))


def test_a_case_round_trips_through_its_dict_form(loaded):
    _version, found = loaded
    for case in found:
        again = cs.case_from_dict(cs.case_to_dict(case), case.dataset_version)
        assert again == case


def test_a_malformed_case_is_refused_without_echoing_it():
    with pytest.raises(c.EvalConfigError) as caught:
        cs.case_from_dict(
            {"id": "x", "turns": "not a list", "category": "factual", "role": "customer"}
        )
    assert "not a list" not in str(caught.value)


# ============================================================================ calibration ====
def test_thirty_calibration_items_and_ten_for_the_expert_review():
    items = cs.load_calibration()
    assert cs.calibration_problems(items) == []
    assert len(items) == 30 and sum(1 for i in items if i.review) == 10


def test_every_calibration_metric_has_both_labels():
    items = cs.load_calibration()
    for metric in cs.CALIBRATION_METRICS:
        mine = [i for i in items if i.metric == metric]
        assert any(i.label for i in mine) and any(not i.label for i in mine), metric
        assert len(mine) == 6


def test_a_calibration_set_of_the_wrong_size_or_without_a_no_label_is_refused():
    items = cs.load_calibration()
    assert any("not 30" in p for p in cs.calibration_problems(items[:-1]))
    one_sided = [
        cs.CalibrationItem(**{**i.__dict__, "label": True}) if i.metric == "claim_supported" else i
        for i in items
    ]
    assert any("both yes and no" in p for p in cs.calibration_problems(one_sided))
    wrong_sample = [cs.CalibrationItem(**{**i.__dict__, "review": False}) for i in items]
    assert any("expert-review sample" in p for p in cs.calibration_problems(wrong_sample))


def test_the_case_file_is_plain_json_with_a_dataset_version():
    data = json.loads(cs.CASES_PATH.read_text("utf-8"))
    assert data["dataset_version"] and isinstance(data["cases"], list)


def test_every_made_up_identifier_in_a_pii_case_is_one_the_masker_really_masks(loaded):
    """A PAN with a letter the masker does not know as an entity type would never be masked: the
    case would test nothing. (Names need the NER model; they are checked in the owner's run.)"""
    from tests.pipeline_rig import masker_holder

    masker = masker_holder().masker
    _version, found = loaded
    checked = 0
    for case in found:
        if case.category != "pii":
            continue
        masked = masker.mask(case.question, []).text
        for value in case.forbidden:
            if " " in value:  # a person's name
                continue
            assert value not in masked, (case.id, "an identifier the masker does not mask")
            checked += 1
    assert checked >= 4
