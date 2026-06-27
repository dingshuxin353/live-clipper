# Security Policy

live-clipper is a local workstation tool. Do not expose the web console to the public internet.

## Reporting a vulnerability

Please report security issues privately to the project maintainers. Do not include private recordings, transcripts, API keys, or generated failure logs in public issues.

## Sensitive data

Depending on configuration, live-clipper may send audio or transcript text to ASR or OpenAI-compatible LLM providers. Failure logs may also contain model payloads when `failure_log_mode = "full"` is enabled.

Use the default redacted failure log mode for normal use.
