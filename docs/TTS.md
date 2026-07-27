# Web Text-to-Speech (v1)

## Scope

Web TTS is an optional, best-effort presentation feature. The page starts with
auto-read disabled. Once the user enables it, only later live Agent replies are
spoken; user messages, thinking/tool events, control replies and history replay
are excluded. TTS never changes MessageBus, Agent or session persistence.

The first provider is `edge-tts`, an unofficial client for Microsoft Edge's
online speech service. Text selected for speech leaves the local process. No
audio or TTS request is written to workspace, session JSONL or logs.

## Incremental flow

```text
live Agent token events
  -> text segmenter
  -> POST /api/tts for one bounded segment
  -> edge-tts MP3 in memory
  -> ordered browser playback
```

The segmenter starts the first segment at an early complete sentence boundary,
then combines later short sentences into larger speech units before cutting at
strong punctuation (`。！？!?；;` and newline). Weak punctuation (`，,：:`) is
only a long-text fallback. A higher hard limit handles text with no punctuation,
preferring a safe boundary whenever possible. The `done` event flushes the
remaining text but never submits the full reply again. Synthesis may run while
the previous segment is playing; audio must remain ordered and bounded.

Before synthesis, the service removes emoji presentation characters while
preserving normal Chinese/English text and punctuation. The browser may prepare
up to two segments concurrently, but playback always follows source order.

## Lifecycle and failure boundary

- Disabling auto-read aborts the active HTTP request, stops audio, revokes the
  Blob URL and clears pending text/audio.
- Sending a new user message, changing/deleting a session or losing the
  WebSocket stops the previous reply's speech.
- A generation identifier prevents a late response from playing after cancel.
- One segment failure abandons the rest of that reply but keeps auto-read
  enabled for a later reply.
- TTS HTTP and playback failures are fully caught and do not append chat
  messages, alter the WebSocket or block the send controls.

## Server limits

`tts_model` configures provider, voice, rate, total/connect/receive timeouts,
maximum input characters, maximum MP3 bytes and concurrency. The service
accepts plain text only, performs no automatic retry and returns `audio/mpeg`
with `Cache-Control: no-store`. It uses no temporary file or media player.
Audio exists only in the server's in-memory buffer and browser Blob URLs; Blob
URLs are revoked on completion or cancellation, so no disk cleanup job is
required for this implementation.

## Acceptance

Automated tests use fake providers/Communicate streams and must cover empty and
oversize input, output cap, timeout, cancellation, concurrency, safe HTTP
errors and normal WebSocket chat after TTS failure. Browser acceptance covers
default-off state, incremental first playback, strict ordering, disable/stop,
new-message/session/disconnect cancellation, history exclusion and autoplay
rejection. Tests must not call the real Microsoft service without explicit
authorization.
