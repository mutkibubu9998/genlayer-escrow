"""Integration tests for TrustEscrow against GLSim (localnet).

Full consensus: leader + validators run the adjudication with mocked web/LLM
responses (via transaction_context), so no real LLM API key is needed.
Accounts are funded through GLSim's sim_fundAccount RPC.

Requires a running simulator: glsim --port 4000 --validators 5
Run: gltest tests/integration/ -v -s --network localnet
"""

import json
import urllib.request

from gltest import create_accounts, get_contract_factory, get_default_account
from gltest.assertions import tx_execution_failed, tx_execution_succeeded

ONE_GEN = 10**18
FUTURE_DEADLINE = 1893456000  # 2030-01-01T00:00:00Z
EVIDENCE_URL = "https://example.com/delivery/report-1"
MILESTONE = "Write a 500-word blog post about GenLayer consensus"
REQUIREMENTS = "A public URL showing the published post, or the full post text"
GLSIM_URL = "http://127.0.0.1:4000/api"


def _rpc(method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    req = urllib.request.Request(
        GLSIM_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read())
    if "error" in payload:
        raise RuntimeError(f"GLSim RPC {method} failed: {payload['error']}")
    return payload["result"]


def _fund(address, amount):
    _rpc("sim_fundAccount", {"account_address": address, "amount": amount})


def _verdict_context(verdict, with_time=True):
    """Mocks for resolve_escrow: evidence page + LLM verdict."""
    ctx = {
        "validators": [
            {
                "plugin_config": {
                    "mock_web_response": {
                        "nondet_web_request": {
                            EVIDENCE_URL: {
                                "status": 200,
                                "body": "The blog post is published here, 500 words.",
                            }
                        }
                    },
                    "mock_response": {
                        "response": {
                            ".*adjudicating a milestone escrow.*": json.dumps(
                                {
                                    "verdict": verdict,
                                    "reason": "Decisive fact from the evidence.",
                                    "confidence": 95,
                                }
                            )
                        }
                    },
                }
            }
        ],
    }
    if with_time:
        ctx["genvm_datetime"] = "2030-01-02T00:00:00Z"
    return ctx


def _deploy_and_fund():
    factory = get_contract_factory("TrustEscrow")
    contract = factory.deploy(args=[])
    owner = get_default_account()
    alice, bob = create_accounts(2)
    _fund(owner.address, 10 * ONE_GEN)
    _fund(alice.address, 10 * ONE_GEN)
    _fund(bob.address, 10 * ONE_GEN)
    return contract, owner, alice, bob


def _create_escrow(contract, payer, payee):
    receipt = contract.connect(payer).create_escrow(
        args=[payee.address, MILESTONE, REQUIREMENTS, FUTURE_DEADLINE]
    ).transact(value=5 * ONE_GEN)
    assert tx_execution_succeeded(receipt)
    return "esc_0"


def _submit_evidence(contract, payee):
    receipt = contract.connect(payee).submit_evidence(
        args=["esc_0", EVIDENCE_URL, "Full post text here."]
    ).transact()
    assert tx_execution_succeeded(receipt)


def test_full_flow_create_submit_resolve_release():
    contract, owner, alice, bob = _deploy_and_fund()
    escrow_id = _create_escrow(contract, alice, bob)
    _submit_evidence(contract, bob)

    escrow = contract.get_escrow(args=[escrow_id]).call()
    assert escrow["status"] == "EVIDENCE_SUBMITTED"

    # Resolve with mocked evidence + LLM verdict RELEASE (full consensus)
    receipt = contract.resolve_escrow(args=[escrow_id]).transact(
        transaction_context=_verdict_context("RELEASE")
    )
    assert tx_execution_succeeded(receipt)
    escrow = contract.get_escrow(args=[escrow_id]).call()
    assert escrow["status"] == "RESOLVED_RELEASED"
    assert escrow["verdict"] == "RELEASE"


def test_full_flow_resolve_refund():
    contract, owner, alice, bob = _deploy_and_fund()
    escrow_id = _create_escrow(contract, alice, bob)
    _submit_evidence(contract, bob)

    receipt = contract.resolve_escrow(args=[escrow_id]).transact(
        transaction_context=_verdict_context("REFUND")
    )
    assert tx_execution_succeeded(receipt)
    escrow = contract.get_escrow(args=[escrow_id]).call()
    assert escrow["status"] == "RESOLVED_REFUNDED"
    assert escrow["verdict"] == "REFUND"


def test_release_early_and_expiry_refund():
    contract, owner, alice, bob = _deploy_and_fund()

    # Early release by the payer
    escrow_id = _create_escrow(contract, alice, bob)
    receipt = contract.connect(alice).release_early(args=[escrow_id]).transact()
    assert tx_execution_succeeded(receipt)
    escrow = contract.get_escrow(args=[escrow_id]).call()
    assert escrow["status"] == "RESOLVED_RELEASED"
    assert escrow["verdict"] == "EARLY_RELEASE"

    # Expiry refund on a second escrow (no evidence submitted)
    receipt = contract.connect(alice).create_escrow(
        args=[bob.address, MILESTONE, REQUIREMENTS, FUTURE_DEADLINE]
    ).transact(value=2 * ONE_GEN)
    assert tx_execution_succeeded(receipt)
    escrow_id2 = "esc_1"

    # Before the deadline the expiry refund must fail
    receipt = contract.refund_expired(args=[escrow_id2]).transact()
    assert tx_execution_failed(receipt)

    # After the deadline it succeeds
    receipt = contract.refund_expired(args=[escrow_id2]).transact(
        transaction_context={"genvm_datetime": "2030-01-02T00:00:00Z"}
    )
    assert tx_execution_succeeded(receipt)
    escrow = contract.get_escrow(args=[escrow_id2]).call()
    assert escrow["status"] == "EXPIRED_REFUNDED"
    assert escrow["verdict"] == "EXPIRED"


def test_resolve_without_evidence_fails():
    contract, owner, alice, bob = _deploy_and_fund()
    escrow_id = _create_escrow(contract, alice, bob)

    receipt = contract.resolve_escrow(args=[escrow_id]).transact(
        transaction_context=_verdict_context("RELEASE", with_time=False)
    )
    assert tx_execution_failed(receipt)
    escrow = contract.get_escrow(args=[escrow_id]).call()
    assert escrow["status"] == "FUNDED"
