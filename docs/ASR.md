# Web Audio ASR (v1)

## Status and scope

**Status: Web-only v1 implemented; real browser/provider validation pending.**
The intended Web-only v1 path is: a browser records an utterance, the server
normalizes it, a configured cloud ASR provider returns text, and only that text
enters the existing message path. `main.py` constructs the optional service from
`asr_model` and injects it into `WebChannel`; disabled or incomplete configuration
returns `asr_unavailable` without affecting text chat.

It does not add local microphone capture, a wake word, TTS, audio persistence,
or Feishu audio-message handling. ASR is optional; ordinary text channels remain
available when it is disabled or unconfigured.

## Data flow

```text
MediaRecorder complete Blob
  -> POST /api/asr multipart upload
  -> per-request private temporary directory
  -> size, declared audio MIME and ffprobe audio-stream/duration validation
  -> FFmpeg: one audio stream -> 16 kHz / mono / PCM s16le WAV
  -> cloud ASR (normalized file only)
  -> non-empty transcript
  -> existing WebSocket text send
  -> InboundMessage(channel="web", existing session/chat identifiers)
  -> Gateway -> Agent
```

An empty, whitespace-only, failed or timed-out transcription must **not** create
an `InboundMessage` and therefore must not reach the Agent. A successful
transcript follows exactly the normal Web text route and uses the current Web
session key; it must not create a second audio-specific session model.

`MediaRecorder` output is browser-dependent. The client may offer preferred
types with `MediaRecorder.isTypeSupported()`, but it must send the actual
`recorder.mimeType` and the server must treat that value as an untrusted hint.
The full final Blob is uploaded only after `dataavailable` following `stop()`;
individual timeslice chunks are not assumed independently decodable.

Browser microphone capture requires a secure context. Local development may use
localhost, but a LAN Web deployment needs HTTPS before recording is considered a
supported feature. The existing unauthenticated Web management surface is not a
safe exposure boundary for microphone capture. Current v1 parses the multipart
payload in memory and applies its service byte limit afterwards; it is not a
streaming upload boundary.

## Configuration and dependencies

`NanoClawConfig.asr_model` is the v1 configuration surface. `ASR_API_KEY`
overrides its `api_key`; it is intentionally separate from the chat-model key.
When that environment variable is present, `save_config` deliberately writes an
empty ASR key so a Web settings save cannot persist the process secret.

| Field | Proposed default | Meaning |
|---|---:|---|
| `asr_model.enabled` | `false` | Enables construction of the Web ASR service. |
| `asr_model.provider` | `openai_compatible` | Current provider selector. |
| `asr_model.api_key/base_url/model` | empty/default | Provider credential and endpoint/model. |
| `asr_model.timeout_sec/max_retries` | `90` / `1` | Provider request/retry policy. |
| `asr_model.max_audio_bytes/max_duration_sec/max_concurrency` | 10 MiB / 120 / 2 | Input and resource limits. |
| `asr_model.language/prompt` | empty | Optional provider hints. |
| `asr_model.ffmpeg_path/ffprobe_path` | `ffmpeg` / `ffprobe` | System tool locations. |

FFmpeg and ffprobe are operating-system dependencies, not Python dependencies
and must never be installed automatically at runtime. The service invokes them
without a shell and returns safe errors. v1 discovers a missing binary on the
first request; proactive startup health checking remains future hardening.

## Validation and normalization

1. v1 rejects empty payloads, non-audio declared types and service-oversize input;
   the client filename is reduced to a small extension allowlist.
2. `ffprobe` verifies that an audio stream exists, reads duration and rejects
   invalid, zero/negative or excessive duration before conversion.
3. FFmpeg is invoked by argument vector with `-nostdin`, explicit first-audio
   mapping and video/subtitle/data disabled. It creates PCM signed 16-bit, mono,
   16 kHz WAV; only that normalized WAV reaches the provider.

Magic-byte/container validation, streaming request limits, post-normalization
output cap and multi-audio-stream rejection remain hardening work, not claims
about v1 behavior.

Input byte limits do not remove decompression/CPU risk. Conversion has a 30-second
timeout, but v1 has no independent normalized-output cap. A stronger hostile-input
boundary would require an OS sandbox and is outside v1.

## Temporary data and privacy

For each request v1 creates a Python `TemporaryDirectory` with a random prefix
and OS-private permissions. Both filenames are server-generated; the source
keeps only an allowlisted extension inferred from the untrusted upload name. It
does not place raw audio in sessions, `ImageStore`, workspace or logs. A
configurable dedicated temp root remains future hardening.

```text
<system-temp>/nanoclaw-asr-*/input.<allowlisted-extension-or-bin>
<system-temp>/nanoclaw-asr-*/normalized.wav
```

The TemporaryDirectory context removes source and normalized audio after normal
success and handled media/provider failure. Tests cover this with a mocked media
normalizer. Cancellation while a subprocess is communicating terminates and
reaps it before the temporary directory is removed.
Cleanup telemetry may contain only a request UUID and stage, never raw paths,
transcript contents, auth headers or audio bytes.

Raw or normalized audio must not be added to `ImageStore`, session JSONL,
workspace, logs, diagnostics or retries. The final transcript is text and thus
follows the existing session-history retention behavior; the user-facing privacy
notice must say so. Cloud ASR receives only the normalized file and required
provider metadata.

## Failure behavior

| Condition | User-visible outcome | Agent input |
|---|---|---|
| Browser permission, unsupported recorder or empty Blob | Local recording error | None |
| Empty, oversize or probe rejection | "Audio format or duration is unsupported" | None |
| FFmpeg absent or conversion failure | "Audio conversion is unavailable/failed" | None |
| ASR timeout, rate limit or provider error | Retry-safe failure message; no blind retry loop | None |
| Empty/whitespace transcript | "No speech detected" | None |
| Successful non-empty transcript | Normal Web message processing | One text message |

Idempotency for a retried Web recording is not implemented in v1; adding it is
required before treating browser retry as exactly-once delivery.

## Testing and acceptance

- Unit/contract currently covers byte/MIME limits, invalid probe output,
  whitespace transcription rejection, provider multipart/error handling and
  request-temp cleanup with fakes.
- Lifecycle coverage currently includes FFmpeg/ffprobe non-zero exit, timeout
  and cancellation process cleanup; a real local FFmpeg smoke test verifies WebM
  to 16 kHz mono PCM WAV normalization.
- Web tests cover unavailable service, missing/oversized upload, successful
  multipart transcription and invalid/empty service results. A fake-service HTTP
  smoke test verifies the real aiohttp multipart bridge.
- Still pending manual or future automated coverage: permission denial,
  recorder-unsupported behavior, aborted upload, real browser microphone input,
  maximum-duration media, multiple browsers and HTTPS LAN deployment.
- Manual real acceptance (separately authorized): Chrome, Firefox and Safari;
  localhost and HTTPS LAN; observed container/MIME, 1 s and maximum-duration
  clips, cleanup audit, latency and transcription correctness.

No test in this repository may call a paid ASR endpoint or upload a real user
recording without explicit authorization.

## Future Feishu audio entry point (not v1)

Feishu support is a separate channel adapter change. It must accept only the
documented `audio` message type, parse its resource key from the event content,
download via the authenticated message-resource API using the event's message ID
and file key, stream-limit the response, and use the same validation,
normalization, cleanup and ASR service above. It must not accept arbitrary URLs
or turn a `file_key` into a filesystem path. Deduplicate by Feishu message ID.

Before implementation, re-check the current Feishu SDK/API resource `type`,
permissions and event payload against official documentation. Current code is
text-only, so this document must not be read as existing Feishu support.
