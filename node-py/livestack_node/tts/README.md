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

All routes except health/capabilities require the private installed node bearer token:
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
