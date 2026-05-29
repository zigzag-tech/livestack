# Realtime Session Data Plane

Date: 2026-05-30

## Context

Livestack already exposes a realtime-looking developer API through job binding,
`useInput().feed()`, and socket.io gateway events. That API is useful, but the
current implementation treats each fed value as a durable Livestack stream
datapoint. For coarse events this is fine. For latency-sensitive media workloads
such as streaming ASR, where audio frames may arrive every 20-100 ms, the
durable stream path becomes too expensive and too jitter-prone to sit in the hot
path.

This note is about Livestack as a framework, not Unchain-specific portable
compute.

## Current Path

The current user-facing feed path is:

```text
browser useInput().feed()
  -> socket.io CMD_FEED
  -> gateway LiveGatewayConn feed listener
  -> input.byTag(tag).feed(data)
  -> JobSpec input stream publish
  -> DataStream.pub()
  -> gRPC vault StreamService.Pub
  -> JSON parse/schema validation/DB datapoint bookkeeping
  -> Redis Stream XADD
  -> worker DataStreamSubscriber
  -> gRPC vault StreamService.Sub
  -> Redis Stream XREAD
  -> JSON parse
  -> processor loop
```

Important code surfaces:

- `client/src/JobSocketIOClient.ts`: emits `CMD_FEED`.
- `gateway/src/LiveGatewayConn.ts`: receives `CMD_FEED` and calls
  `input.byTag(tag).feed(data)`.
- `core/src/jobs/JobSpec.ts`: converts input feed calls into stream datapoint
  publishes through `_getStreamAndSendDataToPLimited`.
- `core/src/stream/DataStream.ts`: publishes datapoints through the vault
  stream client.
- `core/src/stream/DataStreamSubscriber.ts`: subscribes through the vault
  stream client.
- `vault-dev-server/src/stream/service.ts`: validates/persists datapoints and
  uses Redis Streams (`XADD`/`XREAD`).
- `vault-dev-server/src/queue/service.ts`: uses BullMQ/Redis for job placement
  and capacity-driven worker assignment.
- `transcribe/src/server/speech-chunk-to-transcription.ts`: current speech
  worker consumes chunk-like input from the standard JobSpec input stream.

One correction: repeated `feed()` calls do not create one BullMQ job per audio
chunk. BullMQ is mainly in the job/session placement path. The per-chunk cost is
instead from the durable stream plane: JSON/base64 payloads, socket.io framing,
gRPC to vault, schema/DB work, Redis Stream writes/reads, and subscriber JSON
decode.

## Design Principle

Realtime Livestack edges should have two planes:

```text
Control plane:
  queue/scheduler/lease/ledger/lifecycle/observability

Data plane:
  direct low-latency payload exchange between connected runtime nodes
```

Queues and durable streams are still valuable, but they should not be mandatory
for every high-frequency payload. The framework should know that a realtime edge
exists, assign stable IDs, enforce setup-time contracts, route it to a capable
worker, and optionally record useful events. It should not force every media
packet through Redis/DB before the next node can process it.

## Proposed Abstraction

Add a first-class realtime session concept next to `JobSpec`, for example
`RealtimeSessionSpec`, `StreamingJobSpec`, or a `realtime` mode on selected
JobSpec inputs.

Representative API shape:

```ts
const session = await liveflow.openRealtimeSession({
  from: micInput,
  to: streamingAsrWorker,
  mode: "direct",
  durability: "finals-only", // none | sampled | finals-only | full-async
  qos: {
    maxLatencyMs: 80,
    ordered: true,
    dropPolicy: "drop-old-audio",
  },
});
```

The session would:

- use the queue/scheduler only to place or lease a capable worker;
- return a `sessionId` plus a gateway binding or direct worker endpoint;
- keep decoder/model state warm on the owning worker;
- stream binary audio frames directly over WebSocket, gRPC bidirectional
  streaming, or WebRTC data channel;
- stream partial/final results back over the same channel or a paired low
  latency output channel;
- write durable records asynchronously according to the declared durability
  policy.

For ASR specifically:

- raw PCM/audio frames: direct binary stream, not durable datapoints;
- partial hypotheses: direct low-latency output;
- final transcript segments: durable Livestack datapoints;
- raw frame logs: optional sidecar recorder, never blocking decode;
- queue record: session/job metadata, placement, lifecycle, and completion, not
  every frame.

## Why This Is Materially Faster

Repeated `feed()` can be acceptable for coarse events or multi-second audio
chunks because ASR compute dominates. It is not equivalent for true streaming
ASR.

A direct session hot path removes per-frame:

- JSON wrapping and base64 expansion for binary media;
- framework stream validation and datapoint relation work;
- synchronous DB writes before delivery;
- Redis Stream `XADD` and `XREAD` hops;
- gRPC vault round trips between gateway/runtime and stream service;
- hidden buffering caused by socket emits without delivery/backpressure acks.

The speed difference is not just "queue vs no queue". It is durable datapoint
publish/subscribe versus direct stateful media delivery. Redis can remain in the
control plane and async recording path without determining per-frame latency.

## Backpressure And Failure Semantics

Realtime sessions need explicit behavior that the current `feed()` path does not
make visible enough:

- frame sequence numbers and capture timestamps;
- worker-side watermarks or credits;
- drop/coalesce policies for stale audio;
- optional client resampling/chunk resizing;
- close/abort/barge-in messages;
- heartbeat and lease expiry;
- recoverability policy: resume from last final segment, not from every raw
  frame unless full async recording is enabled.

For speech workloads, dropping old unprocessed audio may be preferable to
preserving every packet if the user is interacting live. For other workloads,
the edge can choose reliable ordered delivery.

## Implementation Sketch

1. Introduce a transport-neutral realtime session interface in `core`.
   - Session metadata and lifecycle types.
   - QoS/durability policy types.
   - A runtime-facing async iterable or duplex interface for frames/results.

2. Extend gateway support beyond `CMD_FEED`.
   - Add commands such as `open_realtime_session`, `realtime_frame`,
     `realtime_ack`, and `close_realtime_session`.
   - Prefer binary payloads for media.
   - Preserve socket.io initially for developer ergonomics; allow gRPC/WebRTC
     transports later.

3. Keep scheduling in the existing queue/capacity system.
   - Use BullMQ/Redis to place the session owner or start a worker if needed.
   - Do not enqueue each frame.
   - Reuse capability/lease primitives for worker selection.

4. Add async recorders.
   - Final transcript segments are normal durable datapoints.
   - Raw media can be sampled or stored in a side channel when requested.
   - Recording lag should never slow the decoder hot path.

5. Migrate `@livestack/transcribe` first.
   - Keep existing chunk-based JobSpec behavior for compatibility.
   - Add a streaming ASR worker that accepts a realtime frame stream.
   - Update the speech template to select realtime mode for mic input.

## Compatibility

The existing `useInput().feed()` API can remain as the durable default. A new
input declaration can opt into the session data plane:

```ts
input: {
  mic: realtimeAudioInput({ sampleRate: 16000, frameMs: 20 }),
}
```

Or the hook can expose a parallel method:

```ts
const { sendFrame, close } = useRealtimeInput({ job, tag: "mic" });
```

This keeps ordinary Livestack jobs simple while giving demanding live apps a
transport path that matches their latency requirements.

## Conclusion

For realtime-demanding applications, Livestack nodes should be directly
connected on the data plane. The worker queue should act as metadata,
scheduling, lease, lifecycle, and optional after-the-fact record keeping. It
should not be the required transport for every live payload.

The framework-level rule should be:

> Queues place work. Durable streams record important state. Realtime sessions
> carry hot-path data directly between connected nodes.
