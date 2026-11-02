# Roadmap

Traceon Replay 0.1 covers recording, offline replay with first-divergence
reports, diffing, pytest integration, a local Studio and Langfuse import.
The items below are planned; priorities may change with user feedback.
Proposals and discussion are welcome in GitHub issues.

## P1 -- next

- **Live comparison mode.** Re-run an agent against real tools and models
  while comparing every call with a recording as it happens, and report where
  the live run first departs from the baseline (without failing closed). For
  evaluating model or prompt changes on recorded scenarios.
- **LangChain / LangGraph adapter.** Record and replay tool and chat-model
  calls through callbacks, without decorating each function by hand.
- **OpenAI Agents SDK adapter.** Instrument function tools and model calls of
  the OpenAI Agents SDK.
- **Provider client helpers.** Thin wrappers for the OpenAI and Anthropic
  Python clients that record requests and responses with correct `decode`
  functions and usage extraction.
- **Fixture editing helpers.** Re-record a single call, drop volatile fields,
  and minimize fixtures for bug reports.

## P2 -- later

- **OpenTelemetry export.** Emit recorded and replayed runs as OTel spans
  following the GenAI semantic conventions, so they can be viewed in existing
  tracing backends.
- **OpenTelemetry import.** Build fixtures from OTLP / GenAI-convention traces,
  in addition to Langfuse.
- **More importers.** Other tracing tools with a public trace API.
- **Studio improvements.** Side-by-side payload diff, search across fixtures,
  timeline view.
- **Streaming responses.** First-class recording of streamed model output
  (chunks and timing), beyond the materialized list stored today.

## Not planned

- A hosted service or a trace-collection backend. Traceon stays a local tool
  that complements observability platforms.
- Reconstructing model behavior. Replay returns what was recorded; it does not
  simulate a model for prompts it has not seen.
