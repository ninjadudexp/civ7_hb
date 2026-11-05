---
tags: [civ7, switch2, homebrew, sdk, porting]
status: draft
---

# SDK Signatures

Relocation inventory for running ns2jb's app-side primitives inside the Civ VII image. Offsets below belong to the SmileBASIC image and are reference values only; the byte prefixes are what transfers. Full byte strings live in `research/ns2jb/engine/shared/sdk-functions.json` and `research/ns2jb/engine/switch2/app/thread-code.json`.

| function | SB offset | first 32 bytes | note |
| --- | --- | --- | --- |
| sdk:allocate | 5732112 | `fd7bbca9f70b00f9fd030091f65702a9f44f03a9d50280523f2000f1e3030054` | allocator entry |
| sdk:free | 5616480 | `e48c0114` | branch stub, resolve target |
| sdk:getService | 1312016 | `fd7bbea9f44f01a9fd030091f30300aa020100b4280040395f0400f181050054` | service open |
| sdk:send | 1410140 | `410400d4c0035fd6` | svc #0x22 `SendSyncRequestWithUserBuffer`, Switch 1 numbering |
| sdk:close | 1410028 | `c10200d4c0035fd6` | svc #0x16 `CloseHandle`, Switch 1 numbering |
| sdk:memset | 5797280 | `620600b44800008b5f0c00f10100003901f11f38c30500545f1c00f101040039` | fill |
| sdk:socket | 1314112 | `6c121214` | branch stub |
| sdk:connect | 1313872 | `6c121214` | same stub bytes as socket |
| sdk:closeSocket | 1314192 | `6c121214` | same stub bytes |
| sdk:poll | 1314256 | `6c121214` | same stub bytes |
| sdk:fcntl | 1314432 | `ff8303d1fd7b0da9fd430391e8058092e903009108f0dff229010291a20f3ba9` | real body |
| sdk:errno | 1314992 | `00121214` | stub |
| thread:create | 1255648 | `ff8303d1fd7b08a9fd030291fc6f09a9fa670aa9f85f0ba9f6570ca9f44f0da9` | worker frame creation |
| thread:join | 1256736 | `fd7bbda9f50b00f9fd030091f44f02a908184d3928040037081c4d39f30300aa` | join |
| thread:exit | 1256368 | `ffc300d1fd7b01a9fd430091f31300f9f30300aaa0630091bf0f00f9e1230091` | thread exit |
| thread:current | 1217616 | `68d03bd500fd40f9c0035fd6` | processor number |
| thread:longjmp | 5998196 | `135040a9155841a9176042a9196843a91b7044a91d7845a9023440f95f000091` | unwinding |
| thread:loadTable | 310672 | `a80240f90041348bf44f42a9f50b40f9fd7bc3a8c0035fd6` | plan-table interpreter |
| thread:call4 | 3378856 | `e00319aae10315aae20314aae30313aae7031faa083540f900013fd6f44f4ea9` | call helper |
| thread:saveResult | 2564720 | `600200b9f30b40f9fd7bc2a8c0035fd6` | 32-bit result store |
| thread:saveResult64 | 1621008 | `600200f9f30b40f9fd7bc2a8c0035fd6` | 64-bit result store |
| thread:tryWait | 1217808 | `685f00b0e10300aa08dd45f900810391e46b1214` | wait helper |
| thread:tryWaitImplementation | 1222112 | `fd7bbda9f65701a9fd030091f44f02a9f30301aaf60300aaf6681294f403002a` | wait body |
| thread:sleep | 1217792 | `e86b1214` | stub |

## Relocation procedure

1. Scan the region reachable from `sdk_base` for each prefix.
2. For stub prefixes, decode the branch and verify the destination body against the full byte string in the repo file.
3. Compare the complete byte string, not the prefix. ns2jb does this through `verifyCode`, and aborts a session when a function body differs.
4. Record a relocation table per session: name, offset, full bytes, sha256 of the verified window, branch target where applicable.

## Ambiguity to watch

`socket`, `connect`, `closeSocket`, and `poll` share one stub prefix, so a prefix hit alone cannot name the function. Match stub chains by their branch target plus the argument order at the destination. The same risk applies to `free` and `errno`.

## ABI check

`send` and `close` are single-instruction SVC wrappers, so the immediate field names the
numbering family. Decoded correctly, SmileBASIC's wrappers are **Switch 1**: `send` is
`svc #0x22` (`SendSyncRequestWithUserBuffer`) and `close` is `svc #0x16` (`CloseHandle`).
The earlier `0x441`/`0x2c1` reading was a hex-slicing mistake, not Switch 2 numbering.

Civ VII's own stub table decodes the same way (Switch 1), so there is no ABI divergence
between the two titles — only a choice of SVC (`0x21` plain vs `0x22` with-user-buffer).
See [[Compat ABI]] for the on-hardware confirmation.
