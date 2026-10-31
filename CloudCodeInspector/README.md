# CloudCode Inspector

A separate, runnable integration of CloudCode's file/read/terminal tools and the screen-control project's local Ollama vision planner. The two source projects remain unchanged. Source hashes and the small local-planner extraction are recorded in `SOURCE_MANIFEST.json`.

## Start

The project has its own `.venv` with dependencies installed on this computer. Double-click **launch.cmd**. It opens a browser launcher on `127.0.0.1` (no public hosting). This avoids the missing Tcl/Tk installation in the bundled Python runtime.

1. Choose a separate target project. Inside the CloudCode tree, use `CloudCodeInspector/targets/<name>`.
2. Enter the **original task** and current **Colab GPT OSS endpoint**.
3. Keep or change the installed local models, then start.
4. Read progress and the saved evidence path. Use **Stop** to stop after the current bounded request. Closing only the browser tab does not stop the server; use Ctrl+C in its launcher console to close it.

No old tunnel URL is configured. An empty/unreachable/mismatched endpoint blocks coding. Use **Inspect existing project only** to inspect without Colab.

For another machine, use Python 3.12 and run:

```powershell
.\setup.ps1 -Python 'C:\path\to\python.exe'
.\launch.cmd
```

Setup installs ordinary Python dependencies and Chromium, never model weights. On this computer, setup can discover the existing bundled Python. Ollama must already be running with the chosen models installed.

## Models and cloud contract

| Role | Default | Reason |
|---|---|---|
| Coding | `openai/gpt-oss-20b` | Explicit GPT OSS support; change to the exact served 20B/120B name |
| Local inspection | `qwen3:8b` | Installed local text/reasoning model; real inspection validation passed |
| Conditional vision | `qwen2.5vl:3b` | Existing screen-control default; real controlled click validation passed |

`llama3.2-vision:11b` is installed and configurable as an alternative vision model. This is the available model matching the earlier “11 billion LLaMA vision” description; no “LLaMA 3.0 V1 11B” model was invented. Other installed alternatives include `qwen2.5vl:7b`, `minicpm-v4.5:8b`, `llama3.1:8b` and `llama3:latest`. No additional weights were downloaded. Ollama cloud aliases are rejected for inspection/vision.

The Colab server must expose `/v1/models` and `/v1/chat/completions`. Accepted configured identities are `openai/gpt-oss-20b`, `openai/gpt-oss-120b`, `gpt-oss-20b`, `gpt-oss-120b`, `gpt-oss:20b`, and `gpt-oss:120b`. Identity must match in both listing and completions. Startup checks system-message handling, conversation history and the output limit before sending project work. These checks verify observed serving behavior and **server-reported identity**, not cryptographic attestation of loaded weights.

If the server requires a key, set `COLAB_API_KEY` in the launch environment. Keys are not placed in the JSON config. The official [GPT OSS vLLM guide](https://developers.openai.com/cookbook/articles/gpt-oss/run-vllm) describes an OpenAI-compatible serving route. No runtime or GPU subscription was changed, and no Colab model was assumed running.

## What each cycle does

1. Preserve the original prompt and a stable requirement list. Use your configured list, or let the local inspector extract it once.
2. Run GPT OSS coding in a separate process using the copied CloudCode NodeInsight, NodeForge and NodePulse tools and file-block parser. The integration uses a task-neutral coding loop: the old runner's mandatory `*_logic.py` / `*_gui.py` / `main.py` phases are inappropriate for arbitrary web or existing projects.
3. Save before/after file hashes, source excerpts, changes, coding requests/responses and actual command results.
4. Run configured independent acceptance checks. Their source is frozen outside the coding target before coding starts; hashes are checked for changes.
5. Ask the local inspector to assess every requirement against files and execution evidence. Invalid JSON, missing requirements and invented evidence references fail closed.
6. If inspection identifies a concrete UI doubt and names a configured screen case, run that case, save observations, then inspect again. With no UI doubt, no browser is launched.
7. Generate an actionable follow-up prompt including the unchanged original task, missing requirements, suggested repairs, failing checks and UI observations. Feed it into the next coding cycle.

The default is at most three cycles, with limits for coding responses, commands, network requests, screen actions and subprocesses. Repeated unchanged files/check outcomes stop as `STALLED`. Backend errors, missing evidence, cancellation and cycle exhaustion are distinct recorded outcomes. A workspace lock prevents overlapping inspector runs on the same target.

## Acceptance checks: what “verified” means

`VERIFIED_FOR_CONFIGURED_REQUIREMENTS` requires every requirement to be supported by inspection **and** covered by passing independent checks or configured UI assertions. Failing checks, missing requirements, unresolved UI doubts, incomplete source coverage or files changing during verification prevent this result. The coding agent's “done” text never grants credit.

Natural-language requirement extraction and local-model review can miss details. Review the requirement list and write checks that actually measure each requirement. Without configured checks, the tool can inspect and suggest repairs, but stops with gaps/needs-acceptance-checks instead of certifying success. Passing a narrowly written check only establishes that check's scope.

Copy `config.example.json` to `config.json`, add requirements/checks, then launch with `launch.cmd --config config.json`. Example:

```json
{
  "cloud": {"base_url": "https://YOUR-CURRENT-TUNNEL", "model": "openai/gpt-oss-20b"},
  "requirements": [{"id":"R1", "text":"add(a,b) returns the sum, including negative inputs"}],
  "checks_dir": "checks",
  "checks": [{"id":"addition", "requirements":["R1"],
              "argv":["{python}","-I","{checks}/accept_add.py","{workspace}"], "timeout":60}]
}
```

Put the independent script in `checks/accept_add.py`. Arguments are an array; the runner substitutes `{python}`, `{checks}` and `{workspace}` without shell parsing. Your checks should fail with a nonzero exit status when behavior is wrong. Coding commands still run with the user's normal permissions, as in CloudCode; this application is not an operating-system security sandbox.

## Conditional screen testing

The integrated adapter currently supports **static target web pages** served from the chosen workspace. Set `screen.enabled`, `screen.entry`, and named cases with requirement IDs, goals, assertions and optional ordered text checkpoints. See `examples/counter/config.json`.

The local screen planner sees only the isolated page screenshot/history. It can click/type/use a small key allowlist inside that page. It cannot launch apps, switch desktop windows or navigate external sites. Requests outside the target origin, new windows and WebSockets are blocked. No user's browser profile is attached. The source project's unrelated YouTube-task instructions and cloud fallback are excluded from the adapter.

Use ordered checkpoints for behavior: `0 → 1 → 2 → 0` proves the counter changed before reset; merely observing a final zero would not. A vision model's “complete” claim is insufficient. Assertions and checkpoints decide the outcome. Native desktop apps and applications requiring a development server need a dedicated target adapter; this version reports that gap rather than controlling unrelated apps.

## Command line

```powershell
.\.venv\Scripts\python.exe run.py --models
.\.venv\Scripts\python.exe run.py --config config.json --preflight
.\.venv\Scripts\python.exe run.py --config config.json --prompt-file task.txt --workspace targets\my-project
.\.venv\Scripts\python.exe run.py --config config.json --prompt-file task.txt --workspace targets\my-project --inspect-only
.\.venv\Scripts\python.exe -m pytest -q
```

Run evidence is under `runs/<timestamp>-<id>/`: original prompt, config, model checks, requirement list, frozen acceptance scripts, per-cycle source snapshots/changes, coding logs, inspections, decisions and follow-up prompts. Screen evidence adds screenshots, action traces and independent assertions. Excluded dependency/generated directories and source truncation are explicit; increase source/context limits or narrow the target when necessary.

## Validation and remaining input

See `VALIDATION.md`. The real local inspector and vision model were exercised on small controlled fixtures. The actual copied CloudCode toolchain was exercised with a deterministic local API fixture. **Live Colab GPT OSS end-to-end remains unverified**: provide a current endpoint and exact served model identity, then run preflight and a task with appropriate acceptance checks.
