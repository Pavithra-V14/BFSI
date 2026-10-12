"""A withdrawn document must stay withdrawn through a restore (Phase 15 round 3, migration 0010).

The delete command records the id (no text), moves the original PDFs to `originals/_withdrawn/`,
and a restore re-runs the delete for every listed id. An ingest of a withdrawn id is refused.
"""

import json
from datetime import timedelta

import pytest
from qdrant_client import models

from app.ingest import cli, withdrawn
from app.ingest.cli import Services, main
from app.ingest.delete import delete_document
from app.ingest.model import IngestError
from app.vector.documents import COLLECTION
from tests.ingest_fixtures import Rig, parsed, sample_elements


@pytest.fixture
def rig(tmp_path):
    made = Rig(tmp_path)
    yield made
    made.close()


@pytest.fixture(autouse=True)
def originals_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ORIGINALS_DIR", tmp_path / "originals")


def rows(rig):
    return rig.db.query_all("SELECT doc_id, withdrawn_at, reason_code FROM withdrawn_documents")


def audit(rig):
    return rig.db.query_all(
        "SELECT actor, target, details FROM audit_log WHERE action = 'delete' ORDER BY created_at"
    )


# ---- the delete writes the record ------------------------------------------------------------
def test_a_delete_records_the_id_the_time_and_the_reason_and_no_text(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a", "compliance")
    found = rows(rig)
    assert [(r[0], r[2]) for r in found] == [("doc-a", "compliance")]
    assert found[0][1].startswith("2026-10-06T12:0")  # the time of the run, in UTC
    assert len(found[0]) == 3, "the record holds three columns: no text"


def test_the_default_reason_is_other_and_a_free_text_reason_is_refused(rig):
    rig.ingest(parsed(*sample_elements()))
    with pytest.raises(IngestError):
        delete_document(rig.ctx, "doc-a", "the customer complained about page 3")
    assert rows(rig) == [] and len(rig.active()) == 3, "a refused reason changes nothing"
    delete_document(rig.ctx, "doc-a")
    assert [r[2] for r in rows(rig)] == ["other"]


def test_a_second_delete_keeps_the_first_time_and_the_first_reason(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a", "outdated")
    first = rows(rig)
    rig.moment += timedelta(days=3)
    delete_document(rig.ctx, "doc-a", "incorrect")
    assert rows(rig) == first
    assert len(audit(rig)) == 1, "a repeat writes no second audit row"


def test_the_audit_row_names_the_reason_and_the_id_only(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a", "replaced")
    ((actor, target, details),) = audit(rig)
    assert target == "doc-a" and json.loads(details)["reason"] == "replaced"


def test_an_id_that_holds_nothing_is_not_recorded_unless_the_owner_says_so(rig):
    """A typo must not burn an id: nothing stored means nothing recorded, and no audit row."""
    result = delete_document(rig.ctx, "old-polcy", "outdated")
    assert result.chunks == 0 and not result.recorded and not result.already_recorded
    assert rows(rig) == [] and audit(rig) == []


def test_a_document_withdrawn_before_the_record_existed_is_entered_with_the_flag(rig):
    result = delete_document(rig.ctx, "old-policy", "outdated", even_if_not_stored=True)
    assert result.chunks == 0 and result.recorded
    assert [r[0] for r in rows(rig)] == ["old-policy"]
    assert len(audit(rig)) == 1
    again = delete_document(rig.ctx, "old-policy", "other", even_if_not_stored=True)
    assert not again.recorded and again.already_recorded and len(audit(rig)) == 1


def test_a_delete_that_finishes_an_unfinished_run_records_the_id(rig):
    """A marker left by an interrupted delete counts as something held: the id is recorded."""
    rig.ingest(parsed(*sample_elements()))
    from app.ingest import pending

    pending.mark(rig.ctx.marker_dir, "doc-a", rig.moment, "delete")
    rig.qdrant.delete(
        COLLECTION,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key="doc_id", match=models.MatchValue(value="doc-a"))]
            )
        ),
    )
    result = delete_document(rig.ctx, "doc-a", "outdated")
    assert result.chunks == 0 and result.recorded


def test_the_record_and_the_database_changes_are_one_transaction(rig, monkeypatch):
    rig.ingest(parsed(*sample_elements()))
    from app.ingest import delete as module

    def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(module, "clear_memory_for_document", boom)
    with pytest.raises(RuntimeError):
        delete_document(rig.ctx, "doc-a")
    assert rows(rig) == [], "a delete that did not finish the database part is not recorded"


# ---- an ingest of a withdrawn id is refused ---------------------------------------------------
def test_a_withdrawn_id_cannot_be_ingested_again_and_nothing_is_written(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    version = rig.corpus_version()
    with pytest.raises(IngestError, match="was withdrawn"):
        rig.ingest(parsed(*sample_elements()))
    assert rig.points() == {} and rig.corpus_version() == version


def test_another_id_is_not_affected(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    result = rig.ingest(parsed(*sample_elements()), doc_id="doc-b")
    assert result.status == "ok" and len(rig.active("doc-b")) == 3


# ---- the record is permanent ------------------------------------------------------------------
def test_the_retention_purge_never_touches_the_record(rig):
    from app.memory import retention

    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    far = rig.moment + timedelta(days=400)
    retention.purge_old_rows(rig.db, now=far, actor="cli:test")
    retention.sweep(rig.db, now=far, actor="cli:test")
    assert [r[0] for r in rows(rig)] == ["doc-a"]


# ---- the originals are moved out of the way ---------------------------------------------------
def make_original(folder, doc_id, name="a" * 64 + ".pdf", data=b"%PDF"):
    target = folder / doc_id
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_bytes(data)
    return target / name


def test_the_originals_move_to_the_withdrawn_folder_and_only_that_documents(tmp_path):
    keep = make_original(tmp_path, "doc-b", "b" * 64 + ".pdf")
    first = make_original(tmp_path, "doc-a", "a" * 64 + ".pdf")
    second = make_original(tmp_path, "doc-a", "c" * 64 + ".pdf")
    assert withdrawn.move_originals(tmp_path, "doc-a") == 2
    assert not first.exists() and not second.exists() and keep.exists()
    assert (tmp_path / "_withdrawn" / "doc-a" / first.name).is_file()
    assert not (tmp_path / "doc-a").exists(), "the empty folder goes"


def test_moving_twice_is_safe_and_a_document_without_originals_moves_nothing(tmp_path):
    make_original(tmp_path, "doc-a")
    assert withdrawn.move_originals(tmp_path, "doc-a") == 1
    assert withdrawn.move_originals(tmp_path, "doc-a") == 0
    assert withdrawn.move_originals(tmp_path, "never-there") == 0


def test_something_else_in_the_folder_is_left_alone(tmp_path):
    make_original(tmp_path, "doc-a")
    (tmp_path / "doc-a" / "notes.txt").write_text("keep me", "utf-8")
    assert withdrawn.move_originals(tmp_path, "doc-a") == 1
    assert (tmp_path / "doc-a" / "notes.txt").read_text("utf-8") == "keep me"


def test_a_re_ingest_from_originals_cannot_see_a_withdrawn_pdf(tmp_path):
    """The restore path B walks originals/<id>/: a withdrawn id has no such folder any more."""
    make_original(tmp_path, "doc-a")
    make_original(tmp_path, "doc-b", "b" * 64 + ".pdf")
    withdrawn.move_originals(tmp_path, "doc-a")
    walked = sorted(
        p.name for p in tmp_path.iterdir() if p.is_dir() and p.name != withdrawn.WITHDRAWN_FOLDER
    )
    assert walked == ["doc-b"]


def test_the_withdrawn_folder_name_can_never_be_a_document_id():
    from app.ingest.model import check_doc_id

    with pytest.raises(IngestError):
        check_doc_id(withdrawn.WITHDRAWN_FOLDER)


# ---- the command line -------------------------------------------------------------------------
class Cli:
    def __init__(self, rig):
        self.rig, self.out, self.err = rig, [], []

    def go(self, *args, confirm=lambda prompt: ""):
        def factory(env, needs="models"):
            return Services(self.rig.ctx)

        return main(
            list(args),
            env={},
            services_factory=factory,
            confirm=confirm,
            out=self.out.append,
            err=self.err.append,
        )

    @property
    def text(self):
        return "\n".join(self.out)


def test_the_delete_command_records_moves_and_says_so(rig, tmp_path):
    rig.ingest(parsed(*sample_elements()))
    make_original(tmp_path / "originals", "doc-a")
    cli_run = Cli(rig)
    code = cli_run.go("delete", "--doc-id", "doc-a", "--reason", "outdated",
                      confirm=lambda prompt: "doc-a")  # fmt: skip
    assert code == 0
    assert "recorded as withdrawn (reason: outdated); 1 original PDF(s) moved" in cli_run.text
    assert [(r[0], r[2]) for r in rows(rig)] == [("doc-a", "outdated")]
    assert not (tmp_path / "originals" / "doc-a").exists()
    assert list((tmp_path / "originals" / "_withdrawn" / "doc-a").glob("*.pdf"))


def test_an_unconfirmed_delete_records_nothing_and_moves_nothing(rig, tmp_path):
    rig.ingest(parsed(*sample_elements()))
    original = make_original(tmp_path / "originals", "doc-a")
    cli_run = Cli(rig)
    assert cli_run.go("delete", "--doc-id", "doc-a", confirm=lambda prompt: "no") == 1
    assert rows(rig) == [] and original.exists() and len(rig.active()) == 3


def test_the_delete_command_says_when_nothing_was_stored_and_records_nothing(rig, tmp_path):
    original = make_original(tmp_path / "originals", "typo-id")
    cli_run = Cli(rig)
    code = cli_run.go("delete", "--doc-id", "typo-id", confirm=lambda prompt: "typo-id")
    assert code == 0 and "id not stored, nothing recorded" in cli_run.text
    assert rows(rig) == [] and original.exists(), "no record, so no PDF is moved"


def test_the_flag_records_an_id_that_holds_nothing_and_moves_its_pdf(rig, tmp_path):
    make_original(tmp_path / "originals", "old-policy")
    cli_run = Cli(rig)
    code = cli_run.go("delete", "--doc-id", "old-policy", "--even-if-not-stored",
                      "--reason", "outdated", confirm=lambda prompt: "old-policy")  # fmt: skip
    assert (
        code == 0 and "recorded as withdrawn (reason: outdated); 1 original PDF(s)" in cli_run.text
    )
    assert [r[0] for r in rows(rig)] == ["old-policy"]
    assert (tmp_path / "originals" / "_withdrawn" / "old-policy").is_dir()


def test_a_reason_outside_the_list_is_refused_by_the_command(rig, capsys):
    cli_run = Cli(rig)
    with pytest.raises(SystemExit):
        cli_run.go("delete", "--doc-id", "doc-a", "--reason", "because I said so")
    assert rows(rig) == []


def test_the_withdrawn_command_lists_ids_and_is_clean_when_every_one_is_gone(rig):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a", "outdated")
    cli_run = Cli(rig)
    assert cli_run.go("withdrawn") == 0
    assert "1 withdrawn document id(s)" in cli_run.text and "reason outdated  gone" in cli_run.text


def snapshot_points(rig):
    found, _ = rig.qdrant.scroll(COLLECTION, limit=100, with_vectors=True, with_payload=True)
    return [models.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in found]


def test_a_snapshot_restore_that_brings_the_document_back_is_found_and_fixed(rig):
    """The restore of a snapshot taken BEFORE the withdrawal: the vectors come back, the record
    stays, the command names the id, and running delete again finishes the job."""
    rig.ingest(parsed(*sample_elements()))
    before = snapshot_points(rig)
    delete_document(rig.ctx, "doc-a", "compliance")
    rig.qdrant.upsert(COLLECTION, points=before, wait=True)  # "restore the snapshot"
    assert len(rig.points()) == 3

    cli_run = Cli(rig)
    assert cli_run.go("withdrawn") == 1
    assert "STILL STORED: 3 chunk(s)" in cli_run.text and "not restored yet" in cli_run.text

    delete_document(rig.ctx, "doc-a", "compliance")  # the runbook's step
    assert rig.points() == {}
    again = Cli(rig)
    assert again.go("withdrawn") == 0 and "still stored" not in again.text
    assert [r[0] for r in rows(rig)] == ["doc-a"], "and the original record is unchanged"


def test_the_withdrawn_command_changes_nothing(rig):
    rig.ingest(parsed(*sample_elements()))
    before = snapshot_points(rig)
    delete_document(rig.ctx, "doc-a")
    rig.qdrant.upsert(COLLECTION, points=before, wait=True)
    corpus = rig.corpus_version()
    Cli(rig).go("withdrawn")
    assert len(rig.points()) == 3 and rig.corpus_version() == corpus


# ---- the module's own rules (a second line of defence behind the command) ---------------------
def test_the_record_function_refuses_a_reason_outside_the_list_and_a_bad_id(rig):
    with pytest.raises(IngestError):
        withdrawn.record(rig.db, "doc-a", "free text", rig.moment)
    with pytest.raises(IngestError):
        withdrawn.record(rig.db, "../etc", "other", rig.moment)
    assert rows(rig) == []


def test_the_list_is_in_the_order_of_the_withdrawals(rig):
    for name, days in (("doc-late", 5), ("doc-first", 1), ("doc-middle", 3)):
        with rig.db.transaction(write=True):
            withdrawn.record(rig.db, name, "other", rig.moment + timedelta(days=days))
    assert [r[0] for r in withdrawn.list_withdrawn(rig.db)] == [
        "doc-first",
        "doc-middle",
        "doc-late",
    ]
    assert withdrawn.is_withdrawn(rig.db, "doc-middle") and not withdrawn.is_withdrawn(
        rig.db, "doc-none"
    )


def test_a_refused_ingest_leaves_no_original_behind(rig, tmp_path):
    """The command checks the record BEFORE it copies the PDF into originals/."""
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a")
    pdf = tmp_path / "policy.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    errors: list[str] = []
    code = main(
        ["ingest", str(pdf), "--doc-id", "doc-a", "--doc-type", "policy",
         "--access-role", "customer"],
        env={},
        services_factory=lambda env, needs="models": Services(rig.ctx),
        parse=lambda path: parsed(*sample_elements()),
        out=lambda text: None,
        err=errors.append,
    )  # fmt: skip
    assert code == 1 and any("was withdrawn" in line for line in errors)
    assert not (tmp_path / "originals" / "doc-a").exists()
    assert rig.points() == {}


# ---- --reinstate ID --confirm-id ID: bringing an id back on purpose ---------------------------
def run_ingest(rig, tmp_path, *extra, env=None):
    pdf = tmp_path / "policy.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    out: list[str] = []
    errors: list[str] = []
    code = main(
        ["ingest", str(pdf), "--doc-id", "doc-a", "--doc-type", "policy",
         "--access-role", "customer", *extra],
        env=env or {},
        services_factory=lambda env, needs="models": Services(rig.ctx),
        parse=lambda path: parsed(*sample_elements()),
        out=out.append,
        err=errors.append,
    )  # fmt: skip
    return code, out, errors


BOTH = ("--reinstate", "doc-a", "--confirm-id", "doc-a")


def withdrawn_doc(rig, reason="compliance"):
    rig.ingest(parsed(*sample_elements()))
    delete_document(rig.ctx, "doc-a", reason)


def test_reinstate_removes_the_record_writes_one_audit_row_and_the_ingest_goes_on(rig, tmp_path):
    withdrawn_doc(rig)
    code, out, _errors = run_ingest(rig, tmp_path, *BOTH)
    assert code == 0 and any("--reinstate" in line for line in out)
    assert rows(rig) == [] and len(rig.active()) == 3
    audit_rows = rig.db.query_all(
        "SELECT target, details FROM audit_log WHERE action = 'reinstate'"
    )
    assert [r[0] for r in audit_rows] == ["doc-a"]
    assert json.loads(audit_rows[0][1]) == {"reason": "compliance"}


@pytest.mark.parametrize(
    "flags",
    [
        ("--reinstate", "doc-a"),  # no confirmation
        ("--confirm-id", "doc-a"),  # no --reinstate: the id is not asked for
        ("--reinstate", "doc-a", "--confirm-id", "doc-b"),  # the second id differs
        ("--reinstate", "doc-b", "--confirm-id", "doc-b"),  # both differ from --doc-id
        ("--reinstate", "DOC-A", "--confirm-id", "DOC-A"),  # not the same text
    ],
)
def test_reinstate_needs_the_id_typed_twice_and_both_equal_to_doc_id(rig, tmp_path, flags):
    withdrawn_doc(rig)
    code, _out, errors = run_ingest(rig, tmp_path, *flags)
    assert code == 1
    assert [r[0] for r in rows(rig)] == ["doc-a"], "the record stays"
    assert rig.points() == {}, "and nothing was ingested"
    assert rig.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'reinstate'")[0] == 0
    assert not (tmp_path / "originals" / "doc-a").exists()


def test_reinstate_is_refused_on_render_even_with_both_ids(rig, tmp_path):
    withdrawn_doc(rig)
    code, _out, errors = run_ingest(rig, tmp_path, *BOTH, env={"RENDER": "true"})
    assert code == 1 and any("never on Render" in line for line in errors)
    assert [r[0] for r in rows(rig)] == ["doc-a"]


def test_the_reinstate_function_refuses_on_render_by_itself(rig):
    withdrawn_doc(rig)
    with pytest.raises(IngestError, match="hosted service"):
        withdrawn.reinstate(
            rig.db, "doc-a", asked_id="doc-a", confirm_id="doc-a", actor="cli:t",
            now=rig.moment, env={"RENDER": "true"},
        )  # fmt: skip
    assert [r[0] for r in rows(rig)] == ["doc-a"]
    assert withdrawn.reinstate(
        rig.db, "doc-a", asked_id="doc-a", confirm_id="doc-a", actor="cli:t",
        now=rig.moment, env={},
    )  # fmt: skip


def test_without_the_flag_the_refusal_names_the_flags_and_nothing_changes(rig, tmp_path):
    withdrawn_doc(rig, "other")
    code, _out, errors = run_ingest(rig, tmp_path)
    assert code == 1 and any("--reinstate ID --confirm-id ID" in line for line in errors)
    assert [r[0] for r in rows(rig)] == ["doc-a"] and rig.points() == {}


def test_the_flags_on_an_id_that_was_never_withdrawn_do_nothing_extra(rig, tmp_path):
    code, out, _errors = run_ingest(rig, tmp_path, *BOTH)
    assert code == 0 and not any("was withdrawn" in line for line in out)
    assert rig.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'reinstate'")[0] == 0


def test_a_reinstated_id_can_be_withdrawn_again_with_a_new_time_and_the_list_follows(rig, tmp_path):
    withdrawn_doc(rig, "outdated")
    run_ingest(rig, tmp_path, *BOTH)
    assert withdrawn.list_withdrawn(rig.db) == []
    rig.moment += timedelta(days=2)
    delete_document(rig.ctx, "doc-a", "incorrect")
    [(doc_id, when, reason)] = withdrawn.list_withdrawn(rig.db)
    assert (doc_id, reason) == ("doc-a", "incorrect") and when.startswith("2026-10-08")


def test_a_restore_never_uses_reinstate(rig):
    """The restore commands (`withdrawn`, `delete`) never remove a record."""
    withdrawn_doc(rig)
    Cli(rig).go("withdrawn")
    delete_document(rig.ctx, "doc-a")
    assert [r[0] for r in rows(rig)] == ["doc-a"]
    assert rig.db.query_one("SELECT COUNT(*) FROM audit_log WHERE action = 'reinstate'")[0] == 0


def test_reinstate_is_all_or_nothing(rig, monkeypatch):
    withdrawn_doc(rig, "other")

    def boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    monkeypatch.setattr(withdrawn, "write_audit", boom)
    with pytest.raises(RuntimeError):
        withdrawn.reinstate(
            rig.db, "doc-a", asked_id="doc-a", confirm_id="doc-a", actor="cli:t", now=rig.moment,
            env={},
        )  # fmt: skip
    assert [r[0] for r in rows(rig)] == ["doc-a"], "the record is not removed without its audit row"
