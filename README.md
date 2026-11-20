# TrustEscrow — Milestone Escrow Adjudicated by GenLayer Consensus

A standalone GenLayer Intelligent Contract that holds GEN in escrow for a
milestone and releases or refunds it based on a consensus verdict about the
delivery evidence. Any milestone-based payment — freelance milestones, bug
bounties, content deliveries, staged procurement — can use it: the milestone
spec and the evidence requirements are per-escrow inputs, not hard-coded
prompts.

**Deployed on StudioNet (gasless):** `0xb4041c30b398F135218a045C780aF5797D4c1a50` — see
[explorer](https://explorer-studio.genlayer.com/address/0xb4041c30b398F135218a045C780aF5797D4c1a50)

Deploy transaction: `0xda106cd3e9c6037664be3038ed16eaa4d003287d2691f7b924828f468a60d3b3` (ACCEPTED, 5/5 validators agreed).

## How it works

1. **Payer creates an escrow** (`create_escrow`, payable) naming the payee,
   the GEN amount, a milestone description, the evidence requirements, and
   an evidence deadline.
2. **Payee submits delivery evidence** (`submit_evidence`): a URL and/or
   text proving the milestone was delivered.
3. **Anyone resolves the escrow** (`resolve_escrow`). The leader fetches the
   evidence page and an LLM judges it against the milestone spec and the
   payer's evidence requirements, returning a strict-JSON verdict:
   `RELEASE` (delivered as specified) or `REFUND` (not delivered / fails
   the spec).
4. **Validators independently re-fetch the same evidence and re-run the
   same verdict.** The custom comparative validator requires exact agreement
   on the `verdict` field — no single leader can release or refund funds
   on its own say-so.
5. **Payout is on-chain:** `RELEASE` pays the payee, `REFUND` pays the
   payer back, via the contract's own `emit_transfer` logic.

Two non-consensus paths, for when no adjudication is needed:

- **Early release** (`release_early`, payer only): the payer releases the
  funds to the payee before/after evidence, e.g. mutual agreement.
- **Expiry refund** (`refund_expired`, anyone): if the payee never submits
  evidence by the deadline, the payer is refunded.

## State machine

```
FUNDED ──submit_evidence──▶ EVIDENCE_SUBMITTED ──resolve (RELEASE)──▶ RESOLVED_RELEASED
   │                              │              └─resolve (REFUND)──▶ RESOLVED_REFUNDED
   ├──release_early──────────────▶ RESOLVED_RELEASED
   └──refund_expired (past deadline)──▶ EXPIRED_REFUNDED
```

## Why this is a reusable primitive, not a thin LLM wrapper

- The escrow, the verdict, and the payout are all on-chain state
  transitions gated by independent validator re-verification of the
  evidence — the LLM never decides alone.
- Equivalence is strict on the decision field (`verdict` must match
  exactly); free-text reasons may differ.
- Error classification follows the GenLayer convention
  (`[EXPECTED]` / `[EXTERNAL]` / `[TRANSIENT]` / `[LLM_ERROR]`) so
  validators compare failure paths correctly.
- All money is atto-scale `u256`; no floats for money.
- The contract is generic: one deployment serves any milestone escrow.

## Contract methods

| Method | Type | Description |
|---|---|---|
| `create_escrow(payee, milestone, evidence_requirements, evidence_deadline_ts)` | payable write | Lock GEN for a milestone (max 1000 GEN) |
| `submit_evidence(escrow_id, evidence_url, evidence_text)` | write | Payee submits delivery evidence |
| `resolve_escrow(escrow_id)` | write | Consensus verdict → release or refund |
| `release_early(escrow_id)` | write | Payer releases funds early |
| `refund_expired(escrow_id)` | write | Refund payer after evidence deadline passes |
| `get_escrow(escrow_id)` | view | Full escrow record |
| `list_escrows()` | view | All escrows |
| `get_escrow_count()` | view | Number of escrows |
| `get_pool_balance()` | view | GEN currently held by the contract |

## Tests

```bash
# Lint (genvm-lint 0.11.0)
genvm-lint check contracts/trust_escrow.py

# Direct mode (fast, in-memory; leader only, mocked web/LLM)
pytest tests/direct/ -v

# Integration (GLSim, full leader + validator consensus)
glsim --port 4000 --validators 5
gltest tests/integration/ -v -s --network localnet
```

- Direct: 9 tests — creation + validation, evidence submission,
  resolve RELEASE / REFUND / text-only evidence, early release, expiry
  refund, deadline reverts, invalid transitions.
- Integration: 4 tests — full create → submit → resolve RELEASE flow,
  resolve REFUND flow, early release + expiry refund, resolve without
  evidence fails.

## Deploy (StudioNet, gasless)

```bash
genlayer network set studionet
genlayer deploy --contract contracts/trust_escrow.py
```

> Note: `genlayer deploy` (CLI 0.39.2) hangs after printing the deployment
> parameters when run under a PTY on this machine (spinner loop, no tx
> submitted). The same deploy works in seconds via
> [`deploy_direct.cjs`](deploy_direct.cjs), which calls `genlayer-js`
> directly with the `genlayer-builder` keystore (password read from
> `~/workspace/genlayer/.builder-account-password` at runtime, never
> stored in the repo):
>
> ```bash
> node deploy_direct.cjs
> ```

## License

MIT — see [LICENSE](LICENSE).
