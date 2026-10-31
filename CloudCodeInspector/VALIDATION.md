# Validation — 8 October 2026

The integration is runnable from its own `.venv`. Live Colab GPT OSS execution is still unverified because no current GPT OSS endpoint was supplied for this integration. No old tunnel was reused.

| Check | Observed result |
|---|---|
| Automated integration suite | **34 passed** in 9.85 seconds (`validation/test-results.log`) |
| Python dependency consistency | `pip check`: no broken requirements |
| Source preservation | All 39 imported-source hashes still match their originals |
| Actual CloudCode tools, controlled coding API | Wrote task files, executed a real Python test successfully, handed off for inspection; no mandatory GUI scaffolding |
| Real local inspector: `qwen3:8b` | Rejected subtraction masquerading as addition; accepted the corrected source after the independent check passed |
| Complete improvement loop | Real local inspector + real CloudCode tools + deterministic coding API fixture: first cycle rejected, actionable follow-up received by coder, second cycle passed independent addition/negative-input checks |
| Real local vision: `qwen2.5vl:3b` | One click on an owned counter page; independent ordered checkpoints observed 0 then 1; final assertion passed |
| Launcher | Browser layout inspected; installed-model control returned 16 local models |

The automated suite covers failed checks despite completion claims, malformed/missing inspector evidence, iteration limits, stalls, cancellation, command timeout, child-process cleanup, endpoint model mismatch/history loss/token-limit failures, local-only model enforcement, source protection, frozen-check tampering, workspace locking, screen gating, observation feedback and disallowed screen actions.

The live round-trip evidence is in:

- `validation/round-trip/result.json` — labels the coding backend explicitly as a fixture, records four coding requests and confirms follow-up delivery.
- `runs/20261008-101402-e18622fb/` — both coding cycles, inspection responses, actual tests, file snapshots and follow-up prompts.
- `validation/local-inspection/results.json` — separate real-model inspection results; 48.41 seconds for the broken fixture and 28.53 seconds for the repaired one.
- `validation/vision-1791454136/screen-1/result.json` — final real vision test with action, screenshot, checkpoints and assertion.
- `validation/vision-1791454136/screen-1/final.png` — observed final counter state.

Earlier failed vision attempts are preserved. The first was blocked by sandbox browser startup; another exposed a mismatch between the model's pixel-coordinate click format and the adapter. The adapter now supports exact text targets and bounded pixel coordinates, and the final test passed. The model's premature completion claim did **not** pass the earlier failed assertion.

The screen adapter currently covers static local web projects. Native desktop targets and apps requiring development servers need a scoped target adapter before their UI can be tested. No unrelated site or user application was automated.

“Verified” applies to the configured requirements and the strength of their acceptance checks. These small fixtures validate the integration, not arbitrary future projects or live GPT OSS quality.
