"""The ingestion command line, with fake parsing and fake services (nothing real is touched)."""

import pytest

from app.ingest import cli
from app.ingest.cli import Services, main
from tests.ingest_fixtures import Rig, parsed, sample_elements


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.close()


@pytest.fixture(autouse=True)
def originals_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ORIGINALS_DIR", tmp_path / "originals")


class Run:
    def __init__(self, rig, tmp_path, document=None):
        self.rig, self.out, self.err = rig, [], []
        self.pdf = tmp_path / "policy.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.document = document or parsed(*sample_elements())
        self.closed = 0

    def go(self, *args, confirm=lambda prompt: "", env=None):
        def factory(env, needs="models"):
            return Services(self.rig.ctx, close=lambda: setattr(self, "closed", self.closed + 1))

        code = main(
            list(args),
            env=env or {},
            services_factory=factory,
            parse=lambda path: self.document,
            confirm=confirm,
            out=self.out.append,
            err=self.err.append,
        )
        return code

    def ingest_args(self, *extra):
        return (
            "ingest", str(self.pdf), "--doc-id", "doc-a", "--doc-type", "policy",
            "--access-role", "customer", "--product-line", "insurance", *extra,
        )  # fmt: skip


def test_ingest_prints_counts_only_and_exits_zero(rig, tmp_path):
    run = Run(rig, tmp_path)
    assert run.go(*run.ingest_args()) == 0
    text = "\n".join(run.out)
    assert "new 3" in text and "version 1" in text and "waiting period" not in text
    assert run.closed == 1 and len(rig.active()) == 3


def test_the_original_is_kept_under_the_doc_id(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    assert list((tmp_path / "originals" / "doc-a").glob("*.pdf"))


def test_a_failed_page_exits_two_and_says_the_document_was_not_activated(rig, tmp_path):
    run = Run(rig, tmp_path, parsed(*sample_elements(), failed=(2,)))
    assert run.go(*run.ingest_args()) == 2
    text = "\n".join(run.out)
    assert "NOT activated" in text and "page 2" in text and rig.points() == {}


def test_a_failed_version_bump_exits_nonzero_and_says_ingest_incomplete(rig, tmp_path):
    rig.cache.bump_corpus_version = lambda: None
    run = Run(rig, tmp_path)
    assert run.go(*run.ingest_args()) == 1
    assert any("ingest incomplete" in line for line in run.err)


def test_a_failed_invalidation_also_exits_nonzero(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    rig.cache.invalidate_doc = lambda doc_id: None
    run.document = parsed(*sample_elements(), *parsed_extra())
    assert run.go(*run.ingest_args()) == 1
    assert any("ingest incomplete" in line for line in run.err)


def parsed_extra():
    from tests.ingest_fixtures import para, title

    return [title("Section 4. New"), para("A fresh clause is added.")]


def test_a_doc_id_taken_from_a_path_is_refused(rig, tmp_path):
    run = Run(rig, tmp_path)
    code = run.go("ingest", str(run.pdf), "--doc-id", "C:/docs/p.pdf", "--doc-type", "policy",
                  "--access-role", "customer")  # fmt: skip
    assert code == 1 and any("doc_id" in line for line in run.err)


def test_doc_type_and_access_role_are_required():
    with pytest.raises(SystemExit):
        main(["ingest", "x.pdf", "--doc-id", "a", "--access-role", "customer"])
    with pytest.raises(SystemExit):
        main(["ingest", "x.pdf", "--doc-id", "a", "--doc-type", "policy"])
    with pytest.raises(SystemExit):
        main(
            ["ingest", "x.pdf", "--doc-id", "a", "--doc-type", "memo", "--access-role", "customer"]
        )


def test_only_pdf_files_are_accepted(rig, tmp_path):
    run = Run(rig, tmp_path)
    (tmp_path / "notes.txt").write_text("x")
    code = run.go("ingest", str(tmp_path / "notes.txt"), "--doc-id", "a", "--doc-type", "policy",
                  "--access-role", "customer")  # fmt: skip
    assert code == 1 and any(".pdf" in line for line in run.err)


def test_a_dry_run_calls_no_service_and_prints_the_estimate(rig, tmp_path):
    def forbidden(env, needs="models"):
        raise AssertionError("a dry run must not build any service")

    def fake_ner():
        return lambda text: [("Rahul Sharma", "PERSON"), ("Mumbai", "GPE")]

    run = Run(rig, tmp_path)
    code = main(
        list(run.ingest_args("--dry-run")), env={}, services_factory=forbidden,
        parse=lambda p: run.document, ner_factory=fake_ner, out=run.out.append,
        err=run.err.append,
    )  # fmt: skip
    text = "\n".join(run.out)
    assert code == 0 and "chunks: 3" in text and "embedding calls 1" in text
    assert "clause label calls: 3" in text and "image caption calls: 0" in text
    assert "entity questions: 1 of 2 candidates" in text  # the person is asked, the place is not
    assert "no service was called" in text and rig.points() == {}


def test_a_dry_run_without_ner_says_the_entity_count_is_missing(rig, tmp_path):
    run = Run(rig, tmp_path)
    main(
        list(run.ingest_args("--dry-run")), env={}, parse=lambda p: run.document,
        ner_factory=None, out=run.out.append, err=run.err.append,
    )  # fmt: skip
    assert any("not counted" in line for line in run.out)


def test_delete_asks_for_the_doc_id_and_does_nothing_otherwise(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    assert run.go("delete", "--doc-id", "doc-a", confirm=lambda prompt: "yes") == 1
    assert len(rig.active()) == 3 and any("not confirmed" in line for line in run.out)
    assert run.go("delete", "--doc-id", "doc-a", confirm=lambda prompt: "doc-a") == 0
    assert rig.points() == {}


def test_delete_with_confirm_id_needs_no_prompt_and_a_wrong_id_deletes_nothing(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())

    def never(prompt):
        raise AssertionError("the page has no keyboard: the prompt must not be asked")

    assert run.go("delete", "--doc-id", "doc-a", "--confirm-id", "doc-b", confirm=never) == 1
    assert len(rig.active()) == 3 and any("not confirmed" in line for line in run.out)
    assert run.go("delete", "--doc-id", "doc-a", "--confirm-id", " doc-a ", confirm=never) == 0
    assert rig.points() == {}


def test_a_delete_with_a_failed_bump_exits_nonzero(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    rig.cache.bump_corpus_version = lambda: None
    assert run.go("delete", "--doc-id", "doc-a", confirm=lambda prompt: "doc-a") == 1
    assert any("delete incomplete" in line for line in run.err)


def test_an_unexpected_error_prints_a_class_name_not_its_message(rig, tmp_path):
    run = Run(rig, tmp_path)
    rig.embedder.fail = True
    assert run.go(*run.ingest_args()) == 1
    assert any("RuntimeError" in line for line in run.err)
    assert not any("embedding service down" in line for line in run.err)


def test_the_real_services_need_a_database_url(tmp_path):
    from app.ingest.model import IngestError

    with pytest.raises(IngestError, match="DATABASE_URL"):
        cli.build_services({})


# ---- review commands and the review queue -----------------------------------------------------
def test_a_failed_page_puts_a_row_in_the_review_queue_and_a_later_success_closes_it(rig, tmp_path):
    run = Run(rig, tmp_path, parsed(*sample_elements(), failed=(2,)))
    assert run.go(*run.ingest_args()) == 2
    rows = rig.db.query_all("SELECT kind, status, doc_id FROM review_queue")
    assert rows == [("failed_ingest_page", "open", "doc-a")]
    assert any("review queue" in line for line in run.out)
    run.document = parsed(*sample_elements())
    assert run.go(*run.ingest_args()) == 0
    assert rig.db.query_all("SELECT status FROM review_queue") == [("dismissed",)]


def test_a_customer_specific_run_is_refused_with_a_clear_message(rig, tmp_path):
    run = Run(rig, tmp_path)
    code = run.go(
        "ingest", str(run.pdf), "--doc-id", "doc-a", "--doc-type", "customer-specific",
        "--access-role", "staff",
    )  # fmt: skip
    assert code == 1 and any("not accepted yet" in line for line in run.err)
    assert rig.points() == {}


def test_a_normal_run_warns_that_text_and_images_go_to_model_providers(rig, tmp_path):
    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    assert any("sent to model providers" in line for line in run.out)


def test_review_entities_through_the_command(rig, tmp_path):
    import uuid

    rig.db.execute(
        "INSERT INTO allowlist_entities (id, doc_id, text, entity_type, status, access_role) "
        "VALUES (?, 'doc-a', 'HLT-2026', 'product', 'pending', 'customer')",
        (str(uuid.uuid4()),),
    )
    run = Run(rig, tmp_path)
    assert run.go("review", "entities", "--doc-id", "doc-a", confirm=lambda prompt: "a") == 0
    assert rig.db.query_all("SELECT status FROM allowlist_entities") == [("approved",)]
    assert any("approved 1" in line for line in run.out) and run.closed == 1


def test_review_chunks_through_the_command_and_an_incomplete_bump_exits_nonzero(rig, tmp_path):
    from tests.ingest_fixtures import para, title

    run = Run(rig, tmp_path)
    run.go(*run.ingest_args())
    point = next(iter(rig.points()))
    rig.qdrant.set_payload(
        "documents", payload={"quarantine": "pending", "quarantine_reason": "x",
                              "effective_to": "2026-10-06T12:00:00+00:00"},
        points=[point],
    )  # fmt: skip
    rig.cache.bump_corpus_version = lambda: None
    code = run.go("review", "chunks", "--doc-id", "doc-a", confirm=lambda prompt: "a")
    assert code == 1 and any("review incomplete" in line for line in run.err)
    assert title and para  # imported for symmetry with the other tests


def test_review_needs_a_valid_doc_id_and_a_known_target():
    with pytest.raises(SystemExit):
        main(["review", "nothing", "--doc-id", "a"])
    assert main(["review", "entities", "--doc-id", "../x"], env={}, err=lambda text: None) == 1
