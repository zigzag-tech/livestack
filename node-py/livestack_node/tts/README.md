# Harmony IndexTTS 2.5

Explicit managed CUDA provider, model `IndexTeam/IndexTTS-2.5`. This is not an alias for IndexTTS-2 or VoxCPM. The class is named `IndexTTS2` upstream and package metadata still says 2.0.0: implementation identity is the exact `infer_v2_5.py` source commit plus pinned checkpoint revision, not that stale package version.

## Controls and semantics

`POST /v1/tts/jobs` accepts exactly:

```json
{"jobId":"histo-example-1","ownerId":"histo:documentary","text":"One complete authored thought.","language":"en","voiceId":"2faa7514bbd48e09","referenceSha256":"caa57bcba8985001390d9d1b7d703cde20ba5718c4e432cfb508dd6f43d89300","controls":{"durationFactor":1.15,"emotionAlpha":0.6,"emotionVector":[0,0,0.05,0,0,0.1,0,0.6],"intervalSilenceMs":300,"maxTextTokensPerSegment":120,"seed":18631019}}
```

- `durationFactor`: finite 0.5–2.0. It scales the native acoustic length-regulator target. Values above 1 make delivery longer/slower. It is **not exact duration in seconds** and adds no authored dramatic rests. Measure the actual generated sample clock.
- `emotionVector`: optional eight values 0–1, ordered happy, angry, sad, afraid, disgusted, melancholic, surprised, calm. Upstream may bias/normalize emotion mixtures; these are requested conditioning strengths, not measured emotions. `emotionAlpha` 0–1 scales that conditioning. It does not change the authorized speaker reference.
- `intervalSilenceMs`: integer 0–2000, only between upstream internal text segments. It does not promise pauses at every punctuation mark. Place intentional between-phrase rests in the timeline, after measuring speech.
- `maxTextTokensPerSegment`: integer 20–200; upstream internal segmentation limit. Prefer complete authored phrases/paragraphs, not one generation per sentence.
- `seed`: integer 0–4294967295. Seeds retained, not cross-version bit-exact reproducibility guarantees.
- Languages: en/zh/ja/es/ar. The reference is registered in the worker's closed private configuration; callers cannot supply arbitrary reference paths.
- No arbitrary `instruct`, SSML, exact-second duration, external emotion reference, or free-text emotion model is advertised. QwenEmotion is deliberately not loaded in this runtime. The eight-dimensional conditioning is actually forwarded.
- Input Unicode text is retained verbatim in the job and hash. Upstream normal text normalization/tokenization still applies; no LLM semantic rewriting occurs in this provider.

## Job control

Health and capabilities are read-only public metadata. Job routes require the private installed node bearer token:
- `GET /v1/tts/capabilities`
- `POST /v1/tts/jobs`
- `GET /v1/tts/jobs/{jobId}`
- `POST /v1/tts/jobs/{jobId}/cancel`
- `GET /v1/tts/jobs/{jobId}/artifacts/audio.wav` and `receipt.json`

The shared TypeScript client is `core/src/managedTts.ts`. Each control transport is bounded to at most 20 seconds; a transport timeout means unknown execution, never a cancellation receipt. Reconcile the same known job ID. Exact job/request replay does not run inference again, and changed request under an existing job ID refuses 409. Requests have no voice/model fallback.

Every generation obtains a real local Harmony unit lease and exact host/device admission before `manager.run`. The single GPU executor owns model loading, inference, unloading, CUDA synchronization and measured peak allocation. The model is unpinned and yields after idle. A conservative 16 GiB residency/allocator budget is declared independently of measured peaks. Local broker grant is not a claim of fleet/canonical authorization.

Upstream inference is not interruptible through all decoder stages. Cancellation acknowledges a request, then **joins the actual native call and CUDA synchronization** before terminal state or lease release. No bounded physical-cancellation latency is claimed. A restart with any retained nonterminal job refuses until that exact old incarnation is reconciled; no blind restart/retry.

Successful artifacts are native 22050 Hz mono PCM16 WAV, with exact byte digest/frame count, speaker reference digest, text digest, controls, configuration/source/model inventory and actual lease/normal-settlement evidence. A truncation warning refuses success. Model output is not automatically an editorially accepted narration.

## Source and staging

Initial upstream source `d9e41aac89fd00b3d71497fddb287b7f24613712`, checkpoint `c39ce5ba981572cb187443877ff559dfb246ce63`. Auxiliary weights are independently pinned and inventoried. Python 3.11 is required by that upstream release. No upstream setup/install script performs synthesis during staging. Retain the bilibili Model Use License Agreement and upstream copyright notices with copies/distributed derivatives; do not relabel it MIT. This integration introduces no training or private speaker material.

Stage/activate with the independent ZZOPS installed-tree/selector/activation tools, SHA-pinned source asset and expected-current fence, abort drain policy. First install is an unused dedicated namespace, not a shared PolyTTS/broker restart. Subsequent source upgrades must drain this worker's actual jobs; the selector's expected-current fence alone is not a global consumer drain.

## Qualified English auditions (2026-10-19)

Installed executable source initially `33fa772e7e0245d951a59dda68343f0d45b2b75d`, carrier `68682bb20c78e4d44300f76e1d5807a856017be603aaa6a7e11e9ec0bc107fd6`. Exact source import and all 715 installed entries postverified. Python 3.11.13, upstream locked environment plus explicitly retained worker dependencies; native `shared_py` built from the same Livestack source for CPython 3.11. No borrowed Python 3.12 native extension. 30 model files / 8,290,520,027 bytes match pinned metadata and all upstream LFS digests. 339 original source files independently compared after generation.

Initial copied configuration selected GPU0, which was unavailable. Its waiting job was authenticated-cancelled before native synthesis, joined, and released. A paired ZZOPS config update preserved that receipt and moved this worker to physical GPU1 `GPU-ad09a6ed-53fa-ab11-be9a-1ef205b05b97`, Harmony device `xc-tower-ubuntu/a46c4c2e`. Actual broker admission normally evicted idle managed `flash_next`; no raw process kill or shared broker/PolyTTS restart.

Same 54-word English paragraph / speaker / seed / emotion controls:

| durationFactor | Native frames | Native seconds | Words/minute | WAV SHA256 |
|---|---:|---:|---:|---|
| 1.15 | 360448 | 16.346848 | 198.20 | 039479595088973692e0a404c6e19311593fe0c05c2379d5f167fe4afe4d162a |
| 1.6 | 501504 | 22.743946 | 142.46 | 83c0cb43600b81fb541ac603b466010af5adfbcf97306b656691f905e79bc754 |

Both native WAVs are 22050 Hz mono PCM16. Actual shared client verified capability/source, retained status, audio bytes and a replay with unchanged result and zero extra generation, then submitted the separately authorized slower performance. Each generation retained actual unit lease, exact Harmony device grant, CUDA executor join and lease release. Peak allocation/reservation were around 6 GiB; declared allocator limit remains independent and conservative. A cold-load status transport timed out at 20 seconds: the same job was reconciled successfully, never blindly resubmitted. No bounded control-response SLA, live active-cancel, crashed-worker recovery, full film, or subjective cadence acceptance is claimed from these samples.

Upstream already clamps its waveform before returning PCM16; both samples reach the positive rail for a few samples. Preserve native bytes and audition audibility; post gain cannot undo an upstream clamp. The comparison establishes real duration response, not final dramatic pacing.

The initial installed public `params_b:1.6` metadata and corresponding opaque unit suffix were not a verified composite parameter count. Source now omits that unsupported attribute; the GPT checkpoint contains 814,849,460 tensor elements (float32), which is not the full multi-model system count. Do not infer a model size from the historical unit identifier. The exact upstream model ID/revision is authoritative. Documentation/source updates do not mean that an installed worker has been redeployed.

The source enables automatic 40-character chunking on GPUs smaller than 10 GiB. These connected-paragraph auditions qualify the 24 GiB RTX 3090 route, not equivalent quality on an 8 GiB board.
