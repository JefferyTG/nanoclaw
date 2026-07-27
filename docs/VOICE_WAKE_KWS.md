# Local Voice Wake / KWS Candidate

## Status

**Candidate design, frozen for v1 ASR, not implemented.** This note records a
possible later local wake-keyword path using `sherpa-onnx` KWS and
`sounddevice`/PortAudio. It is not part of the Web-only ASR increment described
in `docs/ASR.md`.

The repository currently declares neither `sounddevice` nor `sherpa-onnx`, and
contains no bundled wake-word model. This task does not install a dependency,
download a model, open a microphone, add configuration, or create a background
audio service.

## Frozen candidate architecture

```text
PortAudio callback thread
  -> bounded PCM queue
  -> one KWS worker thread
  -> loop.call_soon_threadsafe(WakeEvent)
  -> asyncio wake-state coordinator
  -> later capture/STT policy (separate decision)
```

- Input target: mono, signed 16-bit PCM, typically 16 kHz; exact frame size and
  sample-rate requirements must come from the selected sherpa-onnx model.
- The PortAudio callback copies a small frame into a bounded thread-safe queue
  with `put_nowait`; it never blocks, logs, resamples, calls asyncio or invokes
  a model.
- A dedicated worker performs resampling if needed, KWS inference, consecutive
  hit confirmation and a short cooldown/debounce period.
- Cross-thread delivery uses `loop.call_soon_threadsafe`; the asyncio side owns
  state transitions and any subsequent user-visible action.
- Queue pressure drops frames with a metric rather than accumulating unbounded
  memory. Wake events are coalesced while a wake action is already active.

## Lifecycle requirements before unfreezing

1. Validate the selected input device and sample rate before starting capture.
2. Create worker/queues, open and start the stream, and roll back every prior
   resource if any step fails.
3. On stop: prevent new frames, stop/abort and close the stream, signal and join
   the worker without blocking the event loop, then clear references/queues.
4. On macOS permission denial, device switch/removal, Bluetooth profile change,
   sleep/wake or stream status failure: enter a visible degraded state and let
   the coordinator choose a bounded retry/rebuild policy.
5. Keep raw audio memory-only by default; no ring buffer, logging or model
   upload unless a separate privacy/retention decision explicitly authorizes it.

## Decisions still required

- Exact sherpa-onnx model, licence, model storage location, keyword(s), false
  accept/reject target and supported languages.
- Whether a wake event starts browser recording, local capture, push-to-talk or
  only changes UI state. A wake event is not a text `InboundMessage`.
- macOS distribution and microphone permission plan (`NSMicrophoneUsageDescription`
  for a packaged app; Terminal/IDE TCC behavior during development).
- Supported audio devices, reconnect behavior and whether Bluetooth input is a
  supported or best-effort mode.
- CPU, battery and long-running reliability acceptance criteria.

## Unfreeze gate and tests

Do not unfreeze until the model/licence and privacy decisions are approved and a
dedicated implementation owner is assigned. Required tests include fake callback
frames, queue-full behavior, KWS fixtures, cooldown/debounce, stream startup and
partial-start rollback, stop/cancel child-thread cleanup, device-loss recovery,
and explicit manual macOS permission/sleep/Bluetooth tests. Real microphone tests
require a controlled environment and explicit authorization.
