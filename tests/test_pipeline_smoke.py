"""The pipeline smoke script is off by default, reads .env only with --run and prints names, timings
and
counts: never the question, the answer, a citation's text or a key."""

import secrets

from scripts import pipeline_smoke
from tests.pipeline_rig import PipelineRig, waiting_period_document

QUESTION = "What is the waiting period for illness claims?"


def secret(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


def test_without_run_nothing_is_called_and_no_file_is_read(capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not run without --run")

    monkeypatch.setattr(pipeline_smoke, "load_env_file", forbidden)
    monkeypatch.setattr(pipeline_smoke, "load_owner_env", forbidden)
    monkeypatch.setattr(pipeline_smoke, "real_setup", forbidden)
    assert pipeline_smoke.main([]) == 0
    out = capsys.readouterr().out
    assert "Nothing was called and no file was read" in out and "--run" in out


def test_with_run_it_prints_stage_names_and_counts_and_never_text_or_values(tmp_path, capsys):
    values = {
        "JEV_OPENROUTER_API_KEY": secret("jev"),
        "QDRANT_API_KEY": secret("qd"),
        "DATABASE_URL": "postgresql://user:" + secret("pw") + "@db.remote.test/db",
    }
    env = tmp_path / "sample.env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), "utf-8")
    environ: dict[str, str] = {}
    holder = {}

    def setup(environ_, workdir, with_cache, role):
        (tmp_path / "rig").mkdir()
        rig = PipelineRig(tmp_path / "rig")
        holder["rig"] = rig
        return rig.deps, rig.db, rig.new_user(role)

    code = pipeline_smoke.main(
        ["--run", "--load-env", "--question", QUESTION],
        env_path=env,
        environ=environ,
        make_setup=setup,
    )
    out = capsys.readouterr().out
    assert code == 0
    for stage in (
        "authenticate",
        "local_scan",
        "triage",
        "embed",
        "retrieve",
        "rerank",
        "generate",
    ):
        assert stage in out
    assert "RESULT" in out and "ANSWER" in out and "citations=1" in out and "key=fp456" in out
    assert (
        "loaded 2 variable(s) from .env" in out
    )  # the gateway key and the qdrant key; not DATABASE_URL
    assert "DATABASE_URL" not in environ and "DATABASE_URL" not in out
    for value in values.values():
        assert value not in out  # values are never shown
    assert "waiting period" not in out.lower()  # neither the question nor the answer
    assert "30 days" not in out and "clause" not in out.lower()
    holder["rig"].close()


def test_without_load_env_the_env_file_is_never_read(tmp_path, capsys, monkeypatch):
    """The .env file is read only with the opt-in flag; otherwise only the shell is used."""

    def forbidden(*args, **kwargs):
        raise AssertionError("the .env file must not be read without --load-env")

    monkeypatch.setattr(pipeline_smoke, "load_owner_env", forbidden)
    monkeypatch.setattr(pipeline_smoke, "load_env_file", forbidden)
    environ: dict[str, str] = {}
    holder = {}

    def setup(environ_, workdir, with_cache, role):
        (tmp_path / "rig").mkdir()
        rig = PipelineRig(tmp_path / "rig")
        holder["rig"] = rig
        return rig.deps, rig.db, rig.new_user(role)

    code = pipeline_smoke.main(
        ["--run", "--question", QUESTION],
        env_path=tmp_path / "sample.env",
        environ=environ,
        make_setup=setup,
    )
    out = capsys.readouterr().out
    assert code == 0 and "already set in this shell" in out and "--load-env" in out
    holder["rig"].close()


def test_the_documentation_says_what_it_reads_and_that_qdrant_is_never_written():
    doc = " ".join((pipeline_smoke.__doc__ or "").split())
    assert "never writes to Qdrant" in doc and "READ" in doc
    assert "--load-env" in doc and "not touched unless you add --with-cache" in doc


def test_the_line_printer_hides_anything_that_looks_like_text(capsys):
    pipeline_smoke.line("stage", "OK", ms=3, model="m-1", sentence="What is my claim status today?")
    out = capsys.readouterr().out
    assert "ms=3" in out and "model=m-1" in out and "<hidden>" in out and "claim status" not in out


# ---- the cited document ids and the review row count ---------------------------------------
def smoke_run(tmp_path, capsys, *, role="customer", documents=None, configure=None):
    """One smoke run over a local rig; returns (exit code, output, the rig's chunk strings)."""
    holder = {}
    tmp_path.mkdir(parents=True, exist_ok=True)

    def setup(environ_, workdir, with_cache, role_):
        (tmp_path / "rig").mkdir()
        rig = PipelineRig(tmp_path / "rig", documents=documents)
        if configure:
            configure(rig)
        holder["rig"] = rig
        return rig.deps, rig.db, rig.new_user(role_)

    code = pipeline_smoke.main(
        ["--run", "--role", role, "--question", QUESTION], environ={}, make_setup=setup
    )
    out = capsys.readouterr().out
    rig = holder["rig"]
    texts = []
    if documents is None:
        for point in rig.rig.points("doc-a").values():
            payload = point["payload"]
            texts += [str(payload.get(k)) for k in ("text", "source", "summary")]
    rig.close()
    return code, out, [t for t in texts if t and t != "None"]


def never_text(out, chunk_texts):
    lowered = out.lower()
    for fragment in ("waiting period", "illness claims", "30 days", "what is the", "applies to"):
        assert fragment not in lowered, fragment
    for text in chunk_texts:
        assert text not in out and text.lower() not in lowered


def test_an_answer_prints_the_cited_document_ids_and_no_review_row(tmp_path, capsys):
    code, out, chunk_texts = smoke_run(tmp_path, capsys)
    assert code == 0 and "ANSWER" in out
    assert "cited doc ids: doc-a\n" in out
    assert "review rows: 0\n" in out
    assert chunk_texts, "the rig had chunk text to look for: the test proves something"
    never_text(out, chunk_texts)


def test_a_no_reliable_context_refusal_prints_no_ids_and_one_review_row(tmp_path, capsys):
    def low_scores(rig):
        rig.gateway.rerank_scores = [0.05] * 10  # nothing above the relevance floor

    code, out, chunk_texts = smoke_run(tmp_path, capsys, configure=low_scores)
    assert code == 1 and "REFUSAL" in out and "reason=no_context" in out
    assert "cited doc ids: none\n" in out
    assert "review rows: 1\n" in out
    never_text(out, chunk_texts)


def test_a_staff_only_document_is_cited_for_staff_and_never_for_a_customer(tmp_path, capsys):
    documents = [("ecas-staff-only", "staff", waiting_period_document())]
    code, out, _ = smoke_run(tmp_path / "c", capsys, role="customer", documents=documents)
    # the customer cannot see the document: refused, nothing cited, one queued review row
    assert code == 1 and "reason=no_context" in out
    assert "cited doc ids: none\n" in out and "ecas-staff-only" not in out
    assert "review rows: 1\n" in out
    code, out, _ = smoke_run(tmp_path / "s", capsys, role="staff", documents=documents)
    assert code == 0 and "cited doc ids: ecas-staff-only\n" in out
    assert "review rows: 0\n" in out
    never_text(out, [])


def test_the_ids_line_holds_ids_only_and_hides_anything_that_is_not_an_id():
    ids = pipeline_smoke.cited_doc_ids
    assert ids([]) == "none"
    assert ids([{"doc_id": "b-doc"}, {"doc_id": "a-doc"}, {"doc_id": "a-doc"}]) == "a-doc,b-doc"
    # only the id is read: a title, a source name, a clause or a page is never looked at
    cited = {"doc_id": "a-doc", "source": "Waiting period policy", "clause": "2.1", "page": 3}
    assert ids([cited]) == "a-doc"
    assert ids([{"doc_id": "The waiting period for illness claims is 30 days"}]) == "<hidden>"
    assert ids(["not a dict", {"clause": "2.1"}]) == "none"


def test_the_review_count_is_a_number_or_unknown_and_never_a_row():
    class Db:
        def query_one(self, sql, params=()):
            return (3,)

    class Broken:
        def query_one(self, sql, params=()):
            raise RuntimeError("no such table: review_queue with some text")

    assert pipeline_smoke.review_row_count(Db()) == "3"
    assert pipeline_smoke.review_row_count(Broken()) == "unknown"
