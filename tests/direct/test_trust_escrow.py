"""Direct-mode tests for TrustEscrow.

Fast, in-memory (~30ms). Leader function only - validator logic is NOT
exercised here (see tests/integration for full consensus). All web/LLM
calls are mocked.
"""

import json

ONE_GEN = 10**18
FUTURE_DEADLINE = 1893456000  # 2030-01-01T00:00:00Z
PAST_DEADLINE = 1000000000  # 2001-09-09T01:46:40Z
EVIDENCE_URL = "https://example.com/delivery/report-1"
MILESTONE = "Write a 500-word blog post about GenLayer consensus"
REQUIREMENTS = "A public URL showing the published post, or the full post text"


def _hex(addr) -> str:
    """Raw 20-byte test address -> 0x-prefixed hex (contract returns checksummed)."""
    return "0x" + bytes(addr).hex()


def _deal(direct_vm, *accounts):
    for acc in accounts:
        direct_vm.deal(acc, 100 * ONE_GEN)


def _create_escrow(contract, direct_vm, payer, payee):
    direct_vm.sender = payer
    direct_vm.value = 5 * ONE_GEN
    return contract.create_escrow(_hex(payee), MILESTONE, REQUIREMENTS, FUTURE_DEADLINE)


def _mock_verdict(direct_vm, verdict, page_text="The blog post is published here."):
    direct_vm.mock_web(
        r".*example\.com/delivery/report-1.*",
        {"status": 200, "body": page_text},
    )
    direct_vm.mock_llm(
        r".*adjudicating a milestone escrow.*",
        json.dumps(
            {"verdict": verdict, "reason": "Decisive fact from the evidence.", "confidence": 95}
        ),
    )


def test_create_escrow(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)

    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)
    assert escrow_id == "esc_0"
    assert contract.get_escrow_count() == 1

    e = contract.get_escrow(escrow_id)
    assert e["payer"].lower() == _hex(direct_alice).lower()
    assert e["payee"].lower() == _hex(direct_bob).lower()
    assert e["amount"] == 5 * ONE_GEN
    assert e["milestone"] == MILESTONE
    assert e["status"] == "FUNDED"
    assert e["verdict"] == ""
    assert e["evidence_deadline_ts"] == FUTURE_DEADLINE

    listed = contract.list_escrows()
    assert len(listed["escrows"]) == 1
    assert listed["escrows"][0]["id"] == "esc_0"


def test_create_escrow_validation(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    direct_vm.sender = direct_alice

    with direct_vm.expect_revert("[EXPECTED] Milestone is empty"):
        contract.create_escrow(_hex(direct_bob), "   ", REQUIREMENTS, FUTURE_DEADLINE)
    with direct_vm.expect_revert("[EXPECTED] payee must be a 0x address"):
        contract.create_escrow("bob", MILESTONE, REQUIREMENTS, FUTURE_DEADLINE)
    with direct_vm.expect_revert("[EXPECTED] Payer cannot be the payee"):
        contract.create_escrow(_hex(direct_alice), MILESTONE, REQUIREMENTS, FUTURE_DEADLINE)
    with direct_vm.expect_revert("[EXPECTED] Escrow amount must be > 0"):
        direct_vm.value = 0
        contract.create_escrow(_hex(direct_bob), MILESTONE, REQUIREMENTS, FUTURE_DEADLINE)
    with direct_vm.expect_revert("[EXPECTED] Evidence deadline must be in the future"):
        direct_vm.value = ONE_GEN
        contract.create_escrow(_hex(direct_bob), MILESTONE, REQUIREMENTS, PAST_DEADLINE)
    with direct_vm.expect_revert("[EXPECTED] Escrow not found: esc_99"):
        contract.get_escrow("esc_99")


def test_submit_evidence(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    # Only the payee can submit
    direct_vm.sender = direct_alice
    with direct_vm.expect_revert("[EXPECTED] Only the payee can submit evidence"):
        contract.submit_evidence(escrow_id, EVIDENCE_URL, "post text")

    # Empty evidence reverts
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] Provide an evidence URL and/or evidence text"):
        contract.submit_evidence(escrow_id, "", "   ")

    # Bad URL reverts
    with direct_vm.expect_revert("[EXPECTED] evidence_url must be http(s)"):
        contract.submit_evidence(escrow_id, "ftp://example.com/x", "")

    contract.submit_evidence(escrow_id, EVIDENCE_URL, "Full post text here.")
    e = contract.get_escrow(escrow_id)
    assert e["status"] == "EVIDENCE_SUBMITTED"
    assert e["evidence_url"] == EVIDENCE_URL
    assert e["evidence_text"] == "Full post text here."

    # Second submission reverts
    with direct_vm.expect_revert("[EXPECTED] Escrow is not awaiting evidence"):
        contract.submit_evidence(escrow_id, EVIDENCE_URL, "more")


def test_resolve_release(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    # Cannot resolve before evidence is submitted
    with direct_vm.expect_revert("[EXPECTED] Escrow has no submitted evidence"):
        contract.resolve_escrow(escrow_id)

    direct_vm.sender = direct_bob
    contract.submit_evidence(escrow_id, EVIDENCE_URL, "Full post text here.")
    _mock_verdict(direct_vm, "RELEASE")

    verdict = contract.resolve_escrow(escrow_id)
    assert verdict == "RELEASE"
    e = contract.get_escrow(escrow_id)
    assert e["status"] == "RESOLVED_RELEASED"
    assert e["verdict"] == "RELEASE"
    assert e["verdict_reason"] == "Decisive fact from the evidence."

    # Resolving twice reverts
    with direct_vm.expect_revert("[EXPECTED] Escrow has no submitted evidence"):
        contract.resolve_escrow(escrow_id)


def test_resolve_refund(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.submit_evidence(escrow_id, EVIDENCE_URL, "Only a draft, not published.")
    _mock_verdict(direct_vm, "REFUND", page_text="Page not found - no post here.")

    verdict = contract.resolve_escrow(escrow_id)
    assert verdict == "REFUND"
    e = contract.get_escrow(escrow_id)
    assert e["status"] == "RESOLVED_REFUNDED"
    assert e["verdict"] == "REFUND"


def test_resolve_text_only_evidence(direct_vm, direct_deploy, direct_alice, direct_bob):
    """Evidence with no URL: verdict comes from the submitted text alone."""
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.sender = direct_bob
    contract.submit_evidence(escrow_id, "", "Full 500-word post text here.")
    direct_vm.mock_llm(
        r".*adjudicating a milestone escrow.*",
        json.dumps({"verdict": "RELEASE", "reason": "Text proves delivery.", "confidence": 90}),
    )

    verdict = contract.resolve_escrow(escrow_id)
    assert verdict == "RELEASE"
    assert contract.get_escrow(escrow_id)["status"] == "RESOLVED_RELEASED"


def test_release_early(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    # Only the payer can release early
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] Only the payer can release early"):
        contract.release_early(escrow_id)

    direct_vm.sender = direct_alice
    contract.release_early(escrow_id)
    e = contract.get_escrow(escrow_id)
    assert e["status"] == "RESOLVED_RELEASED"
    assert e["verdict"] == "EARLY_RELEASE"

    # Cannot release twice
    with direct_vm.expect_revert("[EXPECTED] Escrow cannot be released early"):
        contract.release_early(escrow_id)


def test_refund_expired(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    # Before the deadline, expiry refund reverts
    with direct_vm.expect_revert("[EXPECTED] Evidence deadline has not passed"):
        contract.refund_expired(escrow_id)

    direct_vm.warp("2030-01-02T00:00:00Z")
    contract.refund_expired(escrow_id)
    e = contract.get_escrow(escrow_id)
    assert e["status"] == "EXPIRED_REFUNDED"
    assert e["verdict"] == "EXPIRED"

    # Cannot expire-refund twice
    with direct_vm.expect_revert("[EXPECTED] Escrow cannot expire-refund"):
        contract.refund_expired(escrow_id)


def test_submit_after_deadline_reverts(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/trust_escrow.py")
    _deal(direct_vm, direct_alice, direct_bob)
    escrow_id = _create_escrow(contract, direct_vm, direct_alice, direct_bob)

    direct_vm.warp("2030-01-02T00:00:00Z")
    direct_vm.sender = direct_bob
    with direct_vm.expect_revert("[EXPECTED] Evidence deadline has passed"):
        contract.submit_evidence(escrow_id, EVIDENCE_URL, "too late")
