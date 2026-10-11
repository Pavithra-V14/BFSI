"""A stubbed-model evaluation rig: the real pipeline, runner and scoring over a small copy of the
sample corpus, with a scripted model. Nothing here touches a network, a real key or a real service.

The corpus is the sample documents' own sentences (the anchors the cases look for), split into
clauses, ingested into an in-memory test collection named `documents_eval` with the access role
each document has in the corpus manifest. The "model" answers a question with the case's reference
answer and cites the passage that holds the case's anchor, so a run exercises everything except
the quality of a real model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import fakeredis
from qdrant_client import QdrantClient

from app.cache.store import CacheStore
from app.db.connection import connect
from app.db.migrate import run
from app.eval import checks
from app.eval.cases import Case, load_cases
from app.eval.corpus import load_manifest
from app.eval.runner import Harness
from app.guardrails.domains import curated_domains
from app.ingest.index import IngestContext, ingest_document
from app.ingest.model import DocRequest
from app.pipeline.run import Pipeline
from app.pipeline.state import Deps
from app.vector.documents import ensure_documents_collection
from tests.api_fixtures import MIGRATIONS
from tests.helpers import WordCounter
from tests.ingest_fixtures import START, FakeEmbedder, para, parsed, title, words
from tests.pipeline_rig import FakeGateway, masker_holder

COLLECTION = "documents_eval"

BANK = [
    ("Part C. Operational framework and pricing", None),
    (
        "2. Key Fact Statement and Cooling-Off. Before the agreement is executed the Borrower "
        "receives a Key Fact Statement (KFS) showing the annualised percentage rate (APR). The "
        "Borrower has a cooling-off period of 3 days from disbursement to exit by repaying the "
        "principal and the proportionate APR, with no penalty.",
        None,
    ),
    (
        "5. Penal Charges and Prepayment. Non-compliance with material terms attracts penal "
        "charges of 2.00 per cent per annum on the overdue amount only; penal charges are not "
        "capitalised and carry no further interest.",
        None,
    ),
    ("Table 1. Pricing Grid by Internal Credit Rating Grade", None),
    (
        "CR3 1.00% to 2.00% spread 1.85 processing fee 0.40 minimum security cover 1.25 Chief "
        "Credit Officer. CR4 2.00% to 4.00% spread 2.40 processing fee 0.50 minimum security "
        "cover 1.40 Chief Credit Officer. CR5 4.00% to 8.00% spread 3.10 processing fee 0.60 "
        "minimum security cover 1.50 Credit Committee of the Board.",
        None,
    ),
    ("Table 2. Asset Classification and Provisioning Matrix", None),
    (
        "Doubtful D2 Doubtful for 1 to 3 years secured portion 70.00 unsecured portion 30.00 "
        "secured provision 30.00 unsecured provision 100.00 effective provision 51.00.",
        None,
    ),
    (
        "Figure 1. Gross NPA ratio of scheduled commercial banks. The line falls from 11.2 per "
        "cent in FY2018 to 2.3 per cent in FY2025.",
        None,
    ),
    ("Part E. Statutory covenants", None),
    (
        "6. Financial Covenants. Throughout the tenor the Borrower shall maintain a Debt Service "
        "Coverage Ratio of at least 1.25 times, tested annually on audited accounts.",
        None,
    ),
    (
        "8. Asset Classification. An account becomes a Non-Performing Asset when interest or an "
        "instalment of principal remains overdue for more than 90 days.",
        None,
    ),
    (
        "9. Events of Default. (a) Non-payment of principal or interest within three business "
        "days of the due date; (b) breach of financial or negative covenants.",
        None,
    ),
    (
        "Grievance Redressal. Level 3 Principal Nodal Officer: Vivek Anand Rastogi, final reply "
        "within 30 days of the complaint.",
        None,
    ),
]

MF = [
    ("Part C. Service commitments", None),
    (
        "1. What a Consolidated Account Statement is. A monthly statement is sent for any month "
        "with a financial transaction; otherwise a statement is sent half-yearly, in September "
        "and March.",
        None,
    ),
    (
        "2. Valuation and Masking. Stamp duty of 0.005 per cent applies to purchases; Securities "
        "Transaction Tax applies on redemption of equity-oriented units; tax is deducted at "
        "source on dividend income above INR 5,000 in a financial year.",
        None,
    ),
    ("Table 1. Portfolio Valuation Grid as on 30 September 2026", None),
    (
        "Sindhu Capital Large Cap Fund - Regular Plan - Growth, units 3,180.742, NAV 142.8115, "
        "current market value INR 4,54,246.54, gain 15.00 per cent.",
        None,
    ),
    ("Table 2. Realised Capital Gains Register", None),
    (
        "Bharat Horizon Flexi Cap Fund, equity, purchased 18-May-2023, redeemed 14-Jun-2026, "
        "2,000 units, holding 1123 days, LTCG, gain INR 1,36,158.00.",
        None,
    ),
    ("Table 3. Capital Gains Tax Distribution", None),
    ("Total indicative tax before surcharge and 4% cess INR 13,307.60.", None),
    ("Part E. Statutory disclosures", None),
    (
        "5. Capital Gains Rules Applied in This Statement. For equity-oriented schemes, units "
        "held for more than twelve months give long-term capital gains, taxed at 12.5 per cent "
        "on the gain above INR 1,25,000 in a financial year; units held for twelve months or "
        "less give short-term capital gains, taxed at 20 per cent.",
        None,
    ),
    (
        "8. Folio Integrity, KYC and Nomination. Report any discrepancy to AuraVance or the AMC "
        "or RTA within 30 days of receipt, failing which the statement is taken as correct.",
        None,
    ),
]

INS = [
    ("Part B. Risk management structure", None),
    (
        "3. Standard Fire and Allied Perils Coverage Grid. Storm, cyclone, typhoon, tempest, "
        "flood and inundation (STFI) Yes. Deductible 5% of claim, minimum INR 50,000. "
        "Earthquake (fire and shock) Optional add-on Not opted in this policy period.",
        None,
    ),
    (
        "5. Role of AuraVance as Corporate Agent. It does not underwrite the risk, decide a "
        "claim or hold premium: premium is paid directly to the Insurer through the Insurer's "
        "payment channel.",
        None,
    ),
    ("Table 1. Policy Schedule", None),
    ("Total premium payable including GST INR 9,57,971.20.", None),
    (
        "Figure 1. Historic loss factors. The cold-chain stock class shows the highest loss "
        "factor (0.91 per cent) and furniture and fixtures the lowest (0.18 per cent).",
        None,
    ),
    ("Part D. Claims conditions", None),
    (
        "8. Exclusions. No cover for war, invasion or nuclear risks; wilful act or gross "
        "negligence of the Policyholder; consequential loss or loss of profit.",
        None,
    ),
    (
        "9. Subrogation, Contribution, Fraud and Cancellation. The Policyholder may cancel on "
        "fifteen days' written notice with a pro-rata or short-period refund if no claim has "
        "been made; the Insurer may cancel on seven days' notice for non-disclosure, fraud or "
        "non-payment.",
        None,
    ),
]

CORPUS = {
    "banking-rtl-facility": BANK,
    "mf-ecas-servicing": MF,
    "insurance-flexi-property": INS,
}


def document_of(clauses: list[tuple[str, Any]]):
    elements = []
    for text, _ in clauses:
        if text.startswith(("Part ", "Table ")) and len(text.split()) < 12:
            elements.append(title(text))
        else:
            elements.append(para(text))
    return parsed(*elements)


class StubGateway(FakeGateway):
    """A scripted model. For each question it looks up the case, sets the triage answers the
    case expects (out of domain, vague, an injection attempt), and writes the case's reference
    answer ONLY when the passage that holds the case's anchor reached the prompt (it cites that
    passage), so a role that cannot see a document gets no answer from it."""

    def __init__(self, cases: list[Case], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cases = list(cases)
        self.answer_reply = self._answer
        # the hash embeddings of the rig are not a ranking: rank by words in common, scaled so
        # that the best passage scores high and a passage with no word in common scores low
        self.rerank_kind = "cohere"
        self.rerank_scores = self._lexical

    @staticmethod
    def _lexical(query: str, documents: list[str]) -> list[float]:
        """Share of the question's content words (four letters or more) that a passage holds."""
        import re

        words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 4}
        if not words:
            return [0.1 for _ in documents]
        out = []
        for doc in documents:
            have = set(re.findall(r"[a-z0-9]+", doc.lower()))
            out.append(0.1 + 0.85 * len(words & have) / len(words))
        return out

    def _cases_for(self, text: str) -> list[Case]:
        """Every case whose question (or whose longest sentence, for a question that carries a
        masked identifier) is in the text. Several cases may ask the same question in different
        circumstances (a role, a changed clause)."""
        import re

        low = checks.normalise(text)
        found = []
        for case in self._cases:
            sentences = [
                checks.normalise(x) for x in re.split(r"(?<=[.?!])\s+", case.question) if x
            ]
            sentences = [x for x in sentences if len(x.split()) >= 4]
            if checks.normalise(case.question) in low or any(x in low for x in sentences):
                found.append(case)
        return found

    async def decide(self, questions, state, *, tier="decide", timeout_s=None, max_wait_s=0.0):
        matches = self._cases_for(json.dumps(state, ensure_ascii=False))
        case = matches[0] if matches else None
        self.in_domain, self.vague, self.injection = 0.95, 0.05, 0.02
        self.rewrite_reply = None
        if case is not None:
            if case.behaviour == "refuse" and not case.source_docs:
                self.in_domain = 0.05
            if case.behaviour == "clarify":
                self.vague = 0.95
                self.rewrite_reply = {
                    "standalone_question": "",
                    "clear": False,
                    "understood": "",
                    "clarifying_question": "Which one do you mean?",
                }
            if case.behaviour == "block":
                self.injection = 0.95
        return await super().decide(
            questions, state, tier=tier, timeout_s=timeout_s, max_wait_s=max_wait_s
        )

    def _answer(self, prompt: str) -> dict[str, Any]:
        for case in self._cases_for(prompt):
            if case.behaviour != "answer":
                continue
            labels = _labels_holding(prompt, [s.anchor for s in case.sources])
            if labels:
                return {
                    "answer": case.reference,
                    "citations": [{"chunk_id": label} for label in labels],
                }
        return {"answer": "I could not find that in the documents.", "citations": []}


def _labels_holding(prompt: str, anchors: list[str]) -> list[str]:
    """The labels (S1, S2 ...) of the prompt's passages that contain an anchor phrase."""
    import re

    found: list[str] = []
    blocks = re.findall(r"<<<DOCUMENT id=(S\d+)[^>]*>>>(.*?)<<<END DOCUMENT>>>", prompt, re.S)
    for label, text in blocks:
        if any(checks.contains(text, a) for a in anchors):
            found.append(label)
    return found


class EvalRig:
    def __init__(self, tmp_path: Path, *, gateway: FakeGateway | None = None) -> None:
        self.tmp_path = tmp_path
        self.version, self.cases = load_cases()
        self.url = f"sqlite:///{tmp_path / 'scratch.db'}"
        run(self.url, MIGRATIONS, log=lambda line: None)
        self.db = connect(self.url)
        self.qdrant = QdrantClient(":memory:")
        ensure_documents_collection(self.qdrant, COLLECTION)
        redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
        self.cache = CacheStore(redis)
        self.embedder = FakeEmbedder()
        self.gateway = gateway or StubGateway(self.cases)
        self.gateway.embedder = self.embedder
        ctx = IngestContext(
            qdrant=self.qdrant,
            cache=self.cache,
            db=self.db,
            embed=self.embedder,
            count=words,
            lock_dir=tmp_path / "locks",
            marker_dir=tmp_path / "pending",
            collection=COLLECTION,
            now=lambda: START,
        )
        manifest = load_manifest()
        for doc_id, clauses in CORPUS.items():
            entry = manifest[doc_id]
            request = DocRequest(
                doc_id=doc_id,
                source=f"{doc_id}.pdf",
                title=entry.title,
                doc_type=entry.doc_type,
                access_role=entry.access_role,
                product_line=entry.product_line,
                effective_date="2026-10-01",
            )
            ingest_document(ctx, request, document_of(clauses))
        self.deps = Deps(
            gateway=self.gateway,
            qdrant=self.qdrant,
            cache=None,
            masker=masker_holder(),
            counter=WordCounter(),
            usage=None,
            curated_domains=curated_domains(),
            collection=COLLECTION,
            now=lambda: START,
        )
        self.pipeline = Pipeline(self.deps)
        self.harness = Harness(self.pipeline, self.db, self.qdrant, COLLECTION, tmp_path / "work")

    def case(self, case_id: str) -> Case:
        return next(c for c in self.cases if c.id == case_id)

    def snapshot(self) -> list[tuple[str, str, str]]:
        """(point id, text, effective_to) of every point: to prove a run left nothing changed."""
        found, offset = [], None
        while True:
            batch, offset = self.qdrant.scroll(
                COLLECTION, limit=100, offset=offset, with_payload=True
            )
            for p in batch:
                found.append(
                    (
                        str(p.id),
                        str(p.payload.get("text")),
                        json.dumps(p.payload.get("effective_to")),
                    )
                )
            if offset is None:
                return sorted(found)

    def close(self) -> None:
        self.db.close()
