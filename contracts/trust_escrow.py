# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

"""TrustEscrow - milestone escrow adjudicated by GenLayer consensus.

A payer locks GEN in escrow for a milestone (freelance work, a bounty, a
delivery). The payee submits delivery evidence (a URL and/or text). Anyone
can then call resolve_escrow: validators fetch the evidence page, and an LLM
judges it against the milestone specification and the evidence requirements
the payer wrote at creation time, returning a strict-JSON verdict:
RELEASE (delivered as specified) or REFUND (not delivered / fails the spec).
A custom comparative validator re-fetches the same evidence and re-runs the
same verdict; exact agreement on the verdict field is required, so no single
leader can release or refund funds on its own say-so.

State machine:
    FUNDED -> EVIDENCE_SUBMITTED -> RESOLVED_RELEASED | RESOLVED_REFUNDED
    FUNDED -> RESOLVED_RELEASED        (payer releases early, mutual agreement)
    FUNDED -> EXPIRED_REFUNDED        (payee never submits evidence by deadline)

Funds move only through the contract's own payout logic (emit_transfer to
the payee on release, to the payer on refund). All money is atto-scale u256.
No floats for money.

Why this is a reusable primitive, not a thin LLM wrapper: the escrow, the
verdict, and the payout are all on-chain state transitions gated by
independent validator re-verification of the evidence. The same contract
serves any milestone-based payment - freelance milestones, bug bounties,
content deliveries, staged procurement - because the milestone spec and the
evidence requirements are per-escrow inputs, not hard-coded prompts.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from genlayer import *

# ---------------------------------------------------------------------------
# Error classification (validators compare these prefixes, see _handle_leader_error)
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"  # Business logic (deterministic) - exact match required
ERROR_EXTERNAL = "[EXTERNAL]"  # External API 4xx (deterministic) - exact match required
ERROR_TRANSIENT = "[TRANSIENT]"  # Network/5xx (non-deterministic) - agree if both transient
ERROR_LLM = "[LLM_ERROR]"  # LLM misbehavior - always disagree, force rotation

MAX_TEXT_LEN = 500
MAX_EVIDENCE_LEN = 2000
MAX_ESCROW = u256(1000) * u256(10**18)  # 1000 GEN max per escrow
EVIDENCE_CHARS = 6000


@allow_storage
@dataclass
class Escrow:
    id: str
    payer: Address
    payee: Address
    amount: u256
    milestone: str
    evidence_requirements: str
    evidence_url: str
    evidence_text: str
    status: str  # FUNDED | EVIDENCE_SUBMITTED | RESOLVED_RELEASED | RESOLVED_REFUNDED | EXPIRED_REFUNDED
    verdict: str  # "" | RELEASE | REFUND | EARLY_RELEASE | EXPIRED
    verdict_reason: str
    created_ts: u256
    evidence_deadline_ts: u256


@gl.evm.contract_interface
class _EoaRecipient:
    class View:
        pass

    class Write:
        pass


class TrustEscrow(gl.Contract):
    owner: Address
    escrow_count: u256
    escrows: TreeMap[str, Escrow]
    escrow_ids: DynArray[str]

    def __init__(self):
        self.owner = gl.message.sender_address
        self.escrow_count = u256(0)

    # ------------------------------------------------------------------
    # Helpers (deterministic)
    # ------------------------------------------------------------------
    def _now(self) -> u256:
        return u256(int(datetime.now(timezone.utc).timestamp()))

    def _get_escrow(self, escrow_id: str) -> Escrow:
        if escrow_id not in self.escrows:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Escrow not found: {escrow_id}")
        return self.escrows[escrow_id]

    def _escrow_to_dict(self, e: Escrow) -> dict:
        return {
            "id": e.id,
            "payer": e.payer.as_hex,
            "payee": e.payee.as_hex,
            "amount": int(e.amount),
            "milestone": e.milestone,
            "evidence_requirements": e.evidence_requirements,
            "evidence_url": e.evidence_url,
            "evidence_text": e.evidence_text,
            "status": e.status,
            "verdict": e.verdict,
            "verdict_reason": e.verdict_reason,
            "created_ts": int(e.created_ts),
            "evidence_deadline_ts": int(e.evidence_deadline_ts),
        }

    def _parse_verdict(self, raw) -> dict:
        """Defensively parse the LLM verdict JSON (dict or raw string)."""
        if isinstance(raw, dict):
            data = raw
        elif isinstance(raw, str):
            text = raw
            first = text.find("{")
            last = text.rfind("}")
            if first < 0 or last < 0:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM returned no JSON object")
            text = text[first : last + 1]
            text = re.sub(r",(?!\s*?[\{\[\"\'\w])", "", text)
            try:
                data = json.loads(text)
            except Exception:
                raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON unparsable")
        else:
            raise gl.vm.UserError(f"{ERROR_LLM} LLM returned non-dict: {type(raw)}")

        if not isinstance(data, dict):
            raise gl.vm.UserError(f"{ERROR_LLM} LLM JSON is not an object")

        verdict = data.get("verdict")
        if verdict is None:
            for alt in ("decision", "outcome", "result"):
                if alt in data:
                    verdict = data[alt]
                    break
        if verdict is None:
            raise gl.vm.UserError(f"{ERROR_LLM} Missing 'verdict'. Keys: {list(data.keys())}")

        verdict = str(verdict).strip().upper()
        if verdict not in ("RELEASE", "REFUND"):
            raise gl.vm.UserError(f"{ERROR_LLM} Invalid verdict: {verdict}")

        reason = data.get("reason", "")
        if not isinstance(reason, str):
            reason = str(reason)
        return {"verdict": verdict, "reason": reason[:500]}

    def _handle_leader_error(self, leaders_res, leader_fn) -> bool:
        """Canonical error comparison for validators (see write-contract skill)."""
        leader_msg = leaders_res.message if hasattr(leaders_res, "message") else ""
        try:
            leader_fn()
            return False  # Leader errored, validator succeeded - disagree
        except gl.vm.UserError as e:
            validator_msg = e.message if hasattr(e, "message") else str(e)
            if validator_msg.startswith(ERROR_EXPECTED) or validator_msg.startswith(
                ERROR_EXTERNAL
            ):
                return validator_msg == leader_msg
            if validator_msg.startswith(ERROR_TRANSIENT) and leader_msg.startswith(
                ERROR_TRANSIENT
            ):
                return True
            return False
        except Exception:
            return False

    def _payout(self, to: Address, amount: u256) -> None:
        if amount > u256(0):
            _EoaRecipient(to).emit_transfer(value=amount)

    # ------------------------------------------------------------------
    # Create an escrow, locking GEN for a milestone (payable)
    # ------------------------------------------------------------------
    @gl.public.write.payable
    def create_escrow(
        self,
        payee: str,
        milestone: str,
        evidence_requirements: str,
        evidence_deadline_ts: u256,
    ) -> str:
        milestone = milestone.strip()
        if len(milestone) == 0:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Milestone is empty")
        if len(milestone) > MAX_TEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Milestone too long")
        evidence_requirements = evidence_requirements.strip()
        if len(evidence_requirements) > MAX_TEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence requirements too long")

        payee = payee.strip()
        if not (payee.startswith("0x") and len(payee) == 42):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} payee must be a 0x address")
        payee_addr = Address(payee)
        if payee_addr == gl.message.sender_address:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Payer cannot be the payee")

        amount = gl.message.value
        if amount == u256(0):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Escrow amount must be > 0")
        if amount > MAX_ESCROW:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Escrow exceeds 1000 GEN max")

        now = self._now()
        if evidence_deadline_ts <= now:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Evidence deadline must be in the future"
            )

        escrow_id = f"esc_{int(self.escrow_count)}"
        escrow = Escrow(
            id=escrow_id,
            payer=gl.message.sender_address,
            payee=payee_addr,
            amount=amount,
            milestone=milestone,
            evidence_requirements=evidence_requirements,
            evidence_url="",
            evidence_text="",
            status="FUNDED",
            verdict="",
            verdict_reason="",
            created_ts=now,
            evidence_deadline_ts=evidence_deadline_ts,
        )
        self.escrows[escrow_id] = escrow
        self.escrow_ids.append(escrow_id)
        self.escrow_count = self.escrow_count + u256(1)
        return escrow_id

    # ------------------------------------------------------------------
    # Payee submits delivery evidence (URL and/or text)
    # ------------------------------------------------------------------
    @gl.public.write
    def submit_evidence(
        self, escrow_id: str, evidence_url: str, evidence_text: str
    ) -> None:
        escrow = self._get_escrow(escrow_id)
        if gl.message.sender_address != escrow.payee:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only the payee can submit evidence")
        if escrow.status != "FUNDED":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Escrow is not awaiting evidence: {escrow.status}"
            )
        if self._now() >= escrow.evidence_deadline_ts:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence deadline has passed")

        evidence_url = evidence_url.strip()
        evidence_text = evidence_text.strip()
        if len(evidence_url) == 0 and len(evidence_text) == 0:
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Provide an evidence URL and/or evidence text"
            )
        if len(evidence_url) > MAX_TEXT_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence_url too long")
        if len(evidence_url) > 0 and not (
            evidence_url.startswith("http://")
            or evidence_url.startswith("https://")
        ):
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence_url must be http(s)")
        if len(evidence_text) > MAX_EVIDENCE_LEN:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} evidence_text too long")

        escrow.evidence_url = evidence_url
        escrow.evidence_text = evidence_text
        escrow.status = "EVIDENCE_SUBMITTED"
        self.escrows[escrow_id] = escrow

    # ------------------------------------------------------------------
    # Resolve an escrow by consensus verdict on the submitted evidence
    # ------------------------------------------------------------------
    @gl.public.write
    def resolve_escrow(self, escrow_id: str) -> str:
        escrow = self._get_escrow(escrow_id)
        if escrow.status != "EVIDENCE_SUBMITTED":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Escrow has no submitted evidence to resolve: {escrow.status}"
            )

        milestone = escrow.milestone
        requirements = escrow.evidence_requirements
        url = escrow.evidence_url
        text = escrow.evidence_text

        def leader_fn() -> dict:
            page = ""
            if len(url) > 0:
                res = gl.nondet.web.get(url)
                if res.status == 200:
                    pass
                elif 400 <= res.status < 500:
                    raise gl.vm.UserError(
                        f"{ERROR_EXTERNAL} Evidence URL returned {res.status}"
                    )
                else:
                    raise gl.vm.UserError(
                        f"{ERROR_TRANSIENT} Evidence URL returned {res.status}"
                    )
                try:
                    page = res.body.decode("utf-8", errors="ignore")[:EVIDENCE_CHARS]
                except Exception:
                    raise gl.vm.UserError(f"{ERROR_TRANSIENT} Evidence page undecodable")

            prompt = f"""You are adjudicating a milestone escrow.
Milestone to deliver: {milestone}
Evidence the payer requires: {requirements}
Evidence submitted by the payee (text): {text}
Evidence page fetched from {url if len(url) > 0 else "(no URL provided)"}:
{page}

Judge the delivery against the milestone and the evidence requirements using
ONLY the evidence above. Do not assume facts that are not in the evidence.
Respond with JSON only, nothing else:
{{"verdict": "RELEASE" | "REFUND", "reason": "<one sentence quoting the decisive fact>", "confidence": <0-100>}}
Use "RELEASE" only if the evidence shows the milestone was delivered as
specified. Use "REFUND" if the milestone was not delivered, is incomplete,
or the evidence does not prove delivery."""
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            return self._parse_verdict(raw)

        def validator_fn(leaders_res: gl.vm.Result) -> bool:
            if not isinstance(leaders_res, gl.vm.Return):
                return self._handle_leader_error(leaders_res, leader_fn)
            try:
                mine = leader_fn()
            except gl.vm.UserError:
                return False
            except Exception:
                return False
            leader_data = leaders_res.calldata
            if not isinstance(leader_data, dict):
                return False
            # Exact agreement on the decision field; free-text reason may differ.
            return leader_data.get("verdict") == mine.get("verdict")

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
        verdict = result["verdict"]
        reason = result.get("reason", "")

        escrow.verdict = verdict
        escrow.verdict_reason = reason
        if verdict == "RELEASE":
            escrow.status = "RESOLVED_RELEASED"
        else:
            escrow.status = "RESOLVED_REFUNDED"
        self.escrows[escrow_id] = escrow

        if verdict == "RELEASE":
            self._payout(escrow.payee, escrow.amount)
        else:
            self._payout(escrow.payer, escrow.amount)
        return verdict

    # ------------------------------------------------------------------
    # Payer releases the funds early (mutual agreement path)
    # ------------------------------------------------------------------
    @gl.public.write
    def release_early(self, escrow_id: str) -> None:
        escrow = self._get_escrow(escrow_id)
        if gl.message.sender_address != escrow.payer:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Only the payer can release early")
        if escrow.status not in ("FUNDED", "EVIDENCE_SUBMITTED"):
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Escrow cannot be released early: {escrow.status}"
            )

        escrow.status = "RESOLVED_RELEASED"
        escrow.verdict = "EARLY_RELEASE"
        escrow.verdict_reason = "Released early by the payer"
        self.escrows[escrow_id] = escrow
        self._payout(escrow.payee, escrow.amount)

    # ------------------------------------------------------------------
    # Refund the payer when the payee never submitted evidence by deadline
    # ------------------------------------------------------------------
    @gl.public.write
    def refund_expired(self, escrow_id: str) -> None:
        escrow = self._get_escrow(escrow_id)
        if escrow.status != "FUNDED":
            raise gl.vm.UserError(
                f"{ERROR_EXPECTED} Escrow cannot expire-refund: {escrow.status}"
            )
        if self._now() < escrow.evidence_deadline_ts:
            raise gl.vm.UserError(f"{ERROR_EXPECTED} Evidence deadline has not passed")

        escrow.status = "EXPIRED_REFUNDED"
        escrow.verdict = "EXPIRED"
        escrow.verdict_reason = "No evidence submitted by the deadline"
        self.escrows[escrow_id] = escrow
        self._payout(escrow.payer, escrow.amount)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------
    @gl.public.view
    def get_escrow(self, escrow_id: str) -> dict:
        return self._escrow_to_dict(self._get_escrow(escrow_id))

    @gl.public.view
    def list_escrows(self) -> dict:
        return {
            "escrows": [self._escrow_to_dict(self.escrows[eid]) for eid in self.escrow_ids]
        }

    @gl.public.view
    def get_escrow_count(self) -> u256:
        return self.escrow_count

    @gl.public.view
    def get_pool_balance(self) -> u256:
        return self.balance
