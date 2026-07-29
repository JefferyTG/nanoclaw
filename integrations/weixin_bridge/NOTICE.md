# NanoClaw Weixin Bridge notices

This bridge vendors `photon-hq/wechat-ilink-client` at commit
`b3e59440e90da344667b5d38af122fdd77a8d2a3` (declared version 0.1.0), from
<https://github.com/photon-hq/wechat-ilink-client>. Its `package.json`
declares MIT, but the inspected commit contains no standalone LICENSE file.
The vendor source is retained verbatim; NanoClaw bridge code is separate and
does not claim that absent license text. Runtime dependency
`wechat-ilink-client@0.1.0` is exact-pinned to the matching published package;
the Bridge directly reuses its protocol constants plus CDN URL/AES primitives.
NanoClaw patches the response acceptance, QR states, cancellable lifecycle,
durable ack/cursor semantics and JSONL IPC around that fixed foundation.

Tencent `openclaw-weixin` at `cef0bfc390393f716903e16d50408118047f87e0`
(release 2.4.6) was inspected only as a protocol correctness reference. No
Tencent source is copied into this bridge. `TENCENT-MIT-LICENSE` is retained
for provenance should a future patch incorporate Tencent code.
