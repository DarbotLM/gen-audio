# Connectors

Seven surfaces are registered with `gen_audio_connectors::roster`, the MCP `list_connectors` tool, and the desktop ConnectorStatus cards.

| Id | Mode without secrets | What "live" means |
| --- | --- | --- |
| `mcp` | `local` | This process. No vendor call. |
| `acp` | `local` | This process. No vendor call. |
| `harness` | `local` | This process. No vendor call. |
| `copilot` | `mock` | HTTPS call to GitHub Models, or to `COPILOT_STUDIO_ENDPOINT` if set. |
| `claude` | `mock` | HTTPS call to the Anthropic Messages API, or the Claude Code CLI when explicitly enabled. |
| `gpt` | `mock` | HTTPS call to the OpenAI chat completions URL. |
| `gemini` | `mock` | HTTPS call to Gemini `generateContent`. |

`complete` on a vendor connector returns text that says it is **not model output** whenever it did not perform the HTTP call. A credential alone does not send the prompt. Set `GEN_AUDIO_CONNECTOR_LIVE=1` as well. With live calls enabled, health reports `configured` and `authenticated: false`: health does not contact a vendor, so only a successful completion proves authentication. The registry validates plugin ids and duplicate factories at startup.

`complete_one_with_limit` is the bounded completion path used by script
authoring. Its output budget is 256 through 4096 tokens; the legacy
`complete_one` remains fixed at 256. Live responses are decoded by provider
shape, and responses that explicitly report an output-limit termination are
rejected as incomplete.

## Environment

Never commit these. `.env` files are gitignored.

| Variable | Connector | Role |
| --- | --- | --- |
| `GEN_AUDIO_CONNECTOR_LIVE` | vendor connectors | Must be `1` before any vendor HTTP call. |
| `GITHUB_TOKEN`, `GH_TOKEN`, or `COPILOT_GITHUB_TOKEN` | copilot | Bearer token for GitHub Models. |
| `COPILOT_GITHUB_MODEL` | copilot | Model id. Default `gpt-4o-mini`. |
| `COPILOT_STUDIO_ENDPOINT` | copilot | Optional https URL you operate. Receives `{"prompt","source","max_tokens"}`. |
| `ANTHROPIC_API_KEY` | claude | Messages API key. |
| `ANTHROPIC_MODEL` | claude | Default `claude-3-5-haiku-latest`. |
| `CLAUDE_CODE_BIN` | claude | Optional path to the `claude` binary. |
| `GEN_AUDIO_CLAUDE_CODE_CLI` | claude | Must be `1` before that binary is spawned. |
| `OPENAI_API_KEY` | gpt | Chat completions key. |
| `OPENAI_MODEL` | gpt | Default `gpt-4o-mini`. |
| `OPENAI_BASE_URL` | gpt | Default `https://api.openai.com/v1`. http is allowed only when the host is exactly `127.0.0.1` or `localhost`. https endpoints must resolve to a public address unless `GEN_AUDIO_CONNECTOR_ALLOW_PRIVATE=1`. |
| `GEMINI_API_KEY` or `GOOGLE_API_KEY` | gemini | Sent as `x-goog-api-key`. |
| `GEMINI_MODEL` | gemini | Default `gemini-2.0-flash`. |

Config shapes live in `schemas/connectors/`.

## Smoke without secrets

```bash
cargo test -p gen-audio-connectors
cargo run -p gen-audio-mcp -- --smoke
cargo run -p gen-audio-acp -- --smoke
cargo run -p gen-audio-harness -- --fixture
```

The MCP smoke writes a fixture tone. The harness trace includes one `connector_health` line per id and a `tool_plan` line with `synthesized: false`.

## Script authoring

MCP exposes three authoring tools without adding a speech-synthesis path:

- `script_analyze` runs locally. It accepts an ordered cast of 1-8 distinct
  persona ids, explicitly interprets `one_based`, `zero_based`, or
  `alternating` labels, requires every selected persona to appear, and returns
  canonical dialogue plus word-based timing warnings.
- `draft_plan` runs locally. It builds an English draft/rewrite prompt, reports
  its exact SHA-256 and bounded output-token budget, and returns
  `externalRequestSent: false`. English is the only supported language and the
  maximum target is 15 minutes at 1-600 words per minute; a plan that needs
  more than the connector's verified token cap is refused rather than clipped.
- `draft_generate` may contact only the selected configured vendor connector.
  It first rebuilds the plan and requires both `confirmed: true` and the exact
  current `promptSha256`. Mock, local, wrong-connector, provider-truncated, and
  malformed dialogue outputs are refused. Accepted dialogue still needs human
  review and always returns `needsReview: true` and `synthesizedSpeech: false`.

The tool schemas reject unknown fields and enforce the raw text, topic,
rewrite, cast, timing, hash, connector, and confirmation bounds. The authoring
module never invokes the speech bridge or a local model worker.

## MCP tools

`script_analyze`, `draft_plan`, `draft_generate`, `synth`, `improve`,
`spectrogram`, `cube_revision`, `serve_health`, `connector_health`,
`list_connectors`, `list_engines`, `fixture_tone`, `benchmark_reference`,
`harness_plan`.

`serve_health` builds `http://<host>:8002/genaid-audio/health`. An unprobed row sets `healthy` and `probed` to false and does not set `ok`, so `isError` stays false without claiming the node is up. A probe sets `healthy` only when the JSON body has `"service": "genaid-audio"`. `GET /health` on the MCP port is the MCP process, not the node speech route. That body says `"service": "gen-audio-mcp"`. `initialize` accepts protocol versions `2024-11-05` and `2025-03-26` and stores no session. The harness `cast_resolved` event reads `voices/cast_map.example.json` and sets `synthesized` to false. ACP `session/new` requires `cwd` and does not open that directory. Its typed session-store interface explicitly advertises process-lifetime in-memory semantics in the default binary; durable stores can be injected without changing protocol handling. Intent plugins are registry-validated, and unrecognized prompts return an unsupported-intent error instead of falling back to a fake list action.

Synth from MCP reads `GEN_AUDIO_KOKORO_MODEL` and `GEN_AUDIO_KOKORO_VOICES` from the environment, and both files must sit under `GEN_AUDIO_MODEL_DIR`. `script` and `castMap` may only name files under `examples/` or `voices/`. The Claude Code prompt is written to the CLI stdin, not argv. `CLAUDE_CODE_BIN` must be a file named `claude`. The desktop capability set allows the three app commands and does not grant shell or filesystem plugins. `run_fixture_improve` returns status fields only, not Python stdout.
