---
tags: [civ7, switch2, homebrew, research]
status: active
---

# Index

Research thread: launch homebrew on a stock Switch 2 from the Civilization VII chat-XSS entry point. Recovered after a frozen session; the vault holds reference clones under `research/`, root notes [[Research overview]] and [[Work log]], and detailed findings in this folder.

### ⭐ Start here (new sessions)

- **[Handoff v2](handoff-v2.md)** — the session entry point: stages passed, current stage, the frozen command, console protocol, file map, dead ends
- **[Project status](project-status.md)** — the wall, last input, remaining roadmap, expected outcomes, the honest position

### Historical record (open on demand)

- [Recovered status](recovered-status.md)
- [Bridge design](bridge-design.md)
- [Primitive matrix](primitive-matrix.md)
- [Probe plan](probe-plan.md)
- [SDK signatures](sdk-signatures.md)
- [Wave harness](wave-harness.md)
- [Open questions](open-questions.md)

### Deep dives

- [Verification pass](verification-pass.md) — existing notes re-derived from code, plus the first new findings
- [Filesystem breakout](filesystem-breakout.md) — exact geometry and gates of the M3 exploit
- [Payload policy](payload-policy.md) — privilege source, image format, IPC command surface
- [Compat ABI](compat-abi.md) — Switch 1 vs Switch 2 SVC numbering and its consequence
- [Launch gate](launch-gate.md) — session TOCTOU and CodeFS object exchange for M4
- [Retained memory](retained-memory.md) — the corruption-free arbitrary R/W channel the gate depends on
- [Address planner](address-planner.md) — the signed UTF-8 cache-address encoding behind the alias
- [selectedMarker plan](selected-marker-plan.md) — the A–D alias chain's Stage-A blocker hunt (SM1–SM4)
- [FS memory control plan](fs-memory-control-plan.md) — the controller port feeding retained memory
- [Porting roadmap](porting-roadmap.md) — how to reproduce the ns2jb pipeline from the game process
- [Payload guide](payload-guide.md) — where to load payloads and how telemetry comes back
- [Filesystem access](filesystem-access.md) — how the game obtains a path-capable save IFileSystem (M3)
- [Native worker port](native-worker-port.md) — what the P7 waves still need (the missing concurrency substrate)
- [Gated canary plan](gated-canary-plan.md) — signal-gated canary start replacing phase tuning (the live window lever; see its round-33 sign correction)
- [Save I/O depth scaling](save-io-depth-scaling.md) — deep-path walk-cost question put to the FS reviewer
- `../Questions/` — FS-team Q&A: [path limit](../Questions/fs-path-limit-question.md), [controller read](../Questions/fs-controller-read-query.md), [concurrency timing](../Questions/fs-concurrency-timing-question.md), [the response](../Questions/fs-questions-response.md) (digested in [selected-marker plan](selected-marker-plan.md), treat as hypotheses)

## State in one line

The entry point works (JavaScript plus ROP inside Civ VII, boot-persistent). Native homebrew is blocked by stock kernel permissions, so the missing piece is a system-process breakout; ns2jb has one through the filesystem service, and porting it to the game process is the main question.
