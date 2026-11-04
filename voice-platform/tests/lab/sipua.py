"""A small, fully scripted SIP/RTP user agent for the telephony lab.

Plays the two roles a carrier network plays around LiveKit SIP:

* **PSTN caller** — sends INVITE to LiveKit SIP, streams G.711 µ-law audio, records what the
  agent says, and behaves as the *transferee* when LiveKit sends REFER (accepts, reports
  progress with NOTIFY/sipfrag, calls the Refer-To target).
* **Office lines** — answers INVITEs per destination number: answer (and speak), busy (486),
  no answer (ring until CANCEL → 487), reject (603) or unavailable (503).

Scope is deliberately small: UDP only, no authentication, no retransmission timers (loopback),
PCMU only. Every SIP message is logged in ``endpoint.trace`` so tests can assert on the exact
signalling (e.g. that a REFER was received and which Refer-To it carried).
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
import socket
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np

# --------------------------------------------------------------------------------------
# G.711 µ-law
# --------------------------------------------------------------------------------------
_BIAS, _CLIP = 0x84, 32635


def ulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    sign = (x < 0).astype(np.int32)
    x = np.minimum(np.abs(x), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(x)).astype(np.int32) - 7, 0, 7)
    mantissa = (x >> (exponent + 3)) & 0x0F
    return (~((sign << 7) | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()


def ulaw_decode(data: bytes) -> np.ndarray:
    u = (~np.frombuffer(data, dtype=np.uint8).astype(np.int32)) & 0xFF
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    x = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(u & 0x80, -x, x).astype(np.int16)


def _lowpass(x: np.ndarray, cutoff: float, taps: int = 101) -> np.ndarray:
    """Windowed-sinc FIR; ``cutoff`` as a fraction of the sample rate (0–0.5)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
    return np.convolve(x, h / h.sum(), mode="same")


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Band-limited resampling (downsampling without a low-pass aliases 4–8 kHz energy into
    the telephone band and was measurably degrading recognition)."""
    if src == dst or len(pcm) == 0:
        return pcm.astype(np.int16)
    x = pcm.astype(np.float64)
    if dst < src:
        x = _lowpass(x, 0.45 * dst / src)
    n = int(len(x) * dst / src)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(y, -32768, 32767).astype(np.int16)


# --------------------------------------------------------------------------------------
# SIP message parsing/building
# --------------------------------------------------------------------------------------
_COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
            "l": "content-length", "c": "content-type", "r": "refer-to", "o": "event"}  # fmt: skip


@dataclass
class SipMessage:
    start: str
    headers: list[tuple[str, str]]
    body: str = ""

    @property
    def is_request(self) -> bool:
        return not self.start.startswith("SIP/2.0")

    @property
    def method(self) -> str:
        return self.start.split(" ", 1)[0] if self.is_request else self.cseq_method

    @property
    def status(self) -> int:
        return int(self.start.split(" ")[1]) if not self.is_request else 0

    def get(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return None

    def get_all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k == name.lower()]

    @property
    def call_id(self) -> str:
        return self.get("call-id") or ""

    @property
    def cseq_method(self) -> str:
        return (self.get("cseq") or "0 X").split()[1]

    @property
    def cseq_num(self) -> int:
        return int((self.get("cseq") or "0 X").split()[0])

    @staticmethod
    def parse(data: bytes) -> SipMessage:
        text = data.decode(errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        headers = []
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip().lower()
                headers.append((_COMPACT.get(k, k), v.strip()))
        return SipMessage(lines[0], headers, body)

    def encode(self) -> bytes:
        hdrs = [(k, v) for k, v in self.headers if k.lower() != "content-length"]
        lines = [self.start, *(f"{_title(k)}: {v}" for k, v in hdrs)]
        lines.append(f"Content-Length: {len(self.body.encode())}")
        return ("\r\n".join(lines) + "\r\n\r\n" + self.body).encode()


def _title(name: str) -> str:
    special = {"call-id": "Call-ID", "cseq": "CSeq", "www-authenticate": "WWW-Authenticate"}
    return special.get(name.lower(), "-".join(p.capitalize() for p in name.split("-")))


def _tag(value: str | None) -> str | None:
    m = re.search(r";\s*tag=([^;>\s]+)", value or "")
    return m.group(1) if m else None


def _uri(value: str) -> str:
    m = re.search(r"<([^>]+)>", value)
    return (m.group(1) if m else value.split(";")[0]).strip()


def _user(uri: str) -> str:
    m = re.match(r"(?:sips?|tel):([^@;>]+)", _uri(uri))
    return m.group(1) if m else ""


def _hostport(uri: str, default: tuple[str, int]) -> tuple[str, int]:
    m = re.match(r"sips?:(?:[^@]+@)?([^;:>]+)(?::(\d+))?", _uri(uri))
    if not m:
        return default
    return m.group(1), int(m.group(2) or 5060)


def _branch() -> str:
    return "z9hG4bK" + uuid.uuid4().hex[:16]


def sdp_offer(port: int, session_id: int) -> str:
    return (
        f"v=0\r\no=- {session_id} {session_id} IN IP4 127.0.0.1\r\ns=lab\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {port} RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\na=fmtp:101 0-16\r\na=ptime:20\r\na=sendrecv\r\n"
    )


def sdp_remote(body: str) -> tuple[str, int]:
    ip = re.search(r"^c=IN IP4 (\S+)", body, re.M)
    port = re.search(r"^m=audio (\d+)", body, re.M)
    if not (ip and port):
        raise ValueError("SDP without audio")
    return ip.group(1), int(port.group(1))


# --------------------------------------------------------------------------------------
# RTP
# --------------------------------------------------------------------------------------
class RtpSession(asyncio.DatagramProtocol):
    RATE = 8000
    FRAME = 160  # 20 ms

    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.remote: tuple[str, int] | None = None
        self.received: list[tuple[float, np.ndarray]] = []
        self.pending: list[tuple[np.ndarray, asyncio.Future[tuple[float, float]]]] = []
        self.sent_frames = 0
        self._task: asyncio.Task[None] | None = None
        self._seq = random.randint(0, 1000)
        self._ts = random.randint(0, 10000)
        self._ssrc = random.getrandbits(32)

    @property
    def port(self) -> int:
        assert self.transport is not None
        return int(self.transport.get_extra_info("sockname")[1])

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=("127.0.0.1", 0)
        )

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 12 or data[1] & 0x7F != 0:  # PCMU only (ignore DTMF/CN)
            return
        cc = data[0] & 0x0F
        self.received.append((time.monotonic(), ulaw_decode(data[12 + 4 * cc :])))

    def start(self, remote: tuple[str, int]) -> None:
        self.remote = remote
        if self._task is None:
            self._task = asyncio.create_task(self._send_loop())

    async def _send_loop(self) -> None:
        assert self.transport is not None and self.remote is not None
        silence = np.zeros(self.FRAME, dtype=np.int16)
        next_t = time.monotonic()
        current: tuple[np.ndarray, asyncio.Future[tuple[float, float]], float] | None = None
        offset = 0
        while True:
            if current is None and self.pending:
                pcm, fut = self.pending.pop(0)
                current, offset = (pcm, fut, time.monotonic()), 0
            if current is not None:
                chunk = current[0][offset : offset + self.FRAME]
                offset += self.FRAME
                if len(chunk) < self.FRAME:
                    chunk = np.pad(chunk, (0, self.FRAME - len(chunk)))
                if offset >= len(current[0]):
                    if not current[1].done():
                        current[1].set_result((current[2], time.monotonic()))
                    current = None
            else:
                chunk = silence
            header = struct.pack("!BBHII", 0x80, 0, self._seq & 0xFFFF, self._ts, self._ssrc)
            self.transport.sendto(header + ulaw_encode(chunk), self.remote)
            self.sent_frames += 1
            self._seq += 1
            self._ts = (self._ts + self.FRAME) & 0xFFFFFFFF
            next_t += 0.02
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))

    def say(self, pcm16k: np.ndarray) -> asyncio.Future[tuple[float, float]]:
        fut: asyncio.Future[tuple[float, float]] = asyncio.get_running_loop().create_future()
        self.pending.append((resample(pcm16k, 16000, self.RATE), fut))
        return fut

    def recording(self, since: float = 0.0, until: float = float("inf")) -> np.ndarray:
        """Received audio as 16 kHz int16 (for Vosk)."""
        parts = [p for t, p in self.received if since <= t <= until]
        pcm = np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)
        return resample(pcm, self.RATE, 16000)

    def speech_segments(self, threshold: float = 300, gap_s: float = 0.8) -> list[list[float]]:
        segs: list[list[float]] = []
        for t, p in self.received:
            loud = float(np.sqrt(np.mean(p.astype(np.float32) ** 2))) > threshold
            if loud:
                if segs and t - segs[-1][1] <= gap_s:
                    segs[-1][1] = t
                else:
                    segs.append([t, t])
        return segs

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.transport is not None:
            self.transport.close()


# --------------------------------------------------------------------------------------
# Calls and the endpoint
# --------------------------------------------------------------------------------------
@dataclass
class LinePolicy:
    action: str = "answer"  # answer | busy | no_answer | reject | unavailable
    ring_s: float = 0.5
    say: np.ndarray | None = None  # 16 kHz speech to play once answered


@dataclass
class Call:
    call_id: str
    role: str  # uac | uas
    local_uri: str
    remote_uri: str
    remote_addr: tuple[str, int]
    local_tag: str
    remote_tag: str | None = None
    remote_target: str | None = None
    cseq: int = 1
    invite: SipMessage | None = None
    invite_branch: str = ""
    state: str = "calling"
    final_status: int | None = None
    end_reason: str | None = None
    rtp: RtpSession = field(default_factory=RtpSession)
    established: asyncio.Event = field(default_factory=asyncio.Event)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    answered_at: float | None = None
    refer_to: str | None = None
    transfer_call: Call | None = None
    transfer_outcomes: list[int] = field(default_factory=list)
    """Final SIP status of each REFER-initiated call (as reported back in NOTIFY)."""


class SipEndpoint(asyncio.DatagramProtocol):
    def __init__(
        self,
        name: str,
        port: int,
        *,
        lines: dict[str, LinePolicy] | None = None,
        carrier: tuple[str, int] | None = None,
        transfer_timeout_s: float = 30.0,
    ) -> None:
        self.name = name
        self.transfer_timeout_s = transfer_timeout_s  # how long the transferee lets it ring
        self.port = port
        self.lines = lines or {}
        self.carrier = carrier  # where tel: URIs (and unknown hosts) are routed
        self.calls: dict[str, Call] = {}
        self.trace: list[dict[str, Any]] = []
        self.transport: asyncio.DatagramTransport | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- transport ---------------------------------------------------------------------
    async def start(self) -> SipEndpoint:
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.transport, _ = await loop.create_datagram_endpoint(lambda: self, sock=sock)
        return self

    def close(self) -> None:
        for c in self.calls.values():
            c.rtp.close()
        if self.transport is not None:
            self.transport.close()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def _send(self, msg: SipMessage, addr: tuple[str, int]) -> None:
        assert self.transport is not None
        self._log("out", msg)
        self.transport.sendto(msg.encode(), addr)

    def _log(self, direction: str, msg: SipMessage) -> None:
        self.trace.append(
            {
                "t": time.monotonic(),
                "dir": direction,
                "line": msg.start,
                "method": msg.method,
                "status": msg.status,
                "call_id": msg.call_id,
                "refer_to": msg.get("refer-to"),
                "body": msg.body[:200] if msg.method in ("NOTIFY",) else None,
            }
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if data.strip() == b"":  # keepalive
            return
        msg = SipMessage.parse(data)
        self._log("in", msg)
        try:
            if msg.is_request:
                self._on_request(msg, addr)
            else:
                self._on_response(msg, addr)
        except Exception as e:  # never kill the endpoint on a malformed packet
            self.trace.append({"t": time.monotonic(), "dir": "error", "line": repr(e)})

    # --- helpers -----------------------------------------------------------------------
    @property
    def contact(self) -> str:
        return f"<sip:{self.name}@127.0.0.1:{self.port}>"

    def _via(self, branch: str) -> str:
        return f"SIP/2.0/UDP 127.0.0.1:{self.port};branch={branch};rport"

    def _response(
        self,
        req: SipMessage,
        code: int,
        reason: str,
        *,
        to_tag: str | None = None,
        body: str = "",
        extra: list[tuple[str, str]] | None = None,
    ) -> SipMessage:
        to = req.get("to") or ""
        if to_tag and not _tag(to):
            to = f"{to};tag={to_tag}"
        headers = [("via", v) for v in req.get_all("via")]
        headers += [
            ("from", req.get("from") or ""),
            ("to", to),
            ("call-id", req.call_id),
            ("cseq", req.get("cseq") or ""),
        ]
        if code < 300 and req.method in ("INVITE", "SUBSCRIBE", "REFER"):
            headers.append(("contact", self.contact))
        if body:
            headers.append(("content-type", "application/sdp"))
        headers += extra or []
        return SipMessage(f"SIP/2.0 {code} {reason}", headers, body)

    def _in_dialog(
        self, call: Call, method: str, *, body: str = "", extra: list[tuple[str, str]] | None = None
    ) -> SipMessage:
        call.cseq += 1
        target = call.remote_target or call.remote_uri
        headers = [
            ("via", self._via(_branch())),
            ("max-forwards", "70"),
            ("from", f"<{call.local_uri}>;tag={call.local_tag}"),
            ("to", f"<{call.remote_uri}>" + (f";tag={call.remote_tag}" if call.remote_tag else "")),
            ("call-id", call.call_id),
            ("cseq", f"{call.cseq} {method}"),
            ("contact", self.contact),
            *(extra or []),
        ]
        return SipMessage(f"{method} {_uri(target)} SIP/2.0", headers, body)

    def _route(self, uri: str) -> tuple[str, int]:
        if uri.startswith("tel:") and self.carrier is not None:
            return self.carrier
        return _hostport(uri, self.carrier or ("127.0.0.1", 5060))

    # --- UAC ---------------------------------------------------------------------------
    async def call(
        self,
        to_uri: str,
        *,
        from_user: str,
        via: tuple[str, int] | None = None,
    ) -> Call:
        """Place a call. Returns once the INVITE is sent; await ``call.established``."""
        if to_uri.startswith("tel:"):
            num = to_uri[4:]
            host, port = self.carrier or ("127.0.0.1", 5060)
            to_uri = f"sip:{num}@{host}:{port}"
        addr = via or self._route(to_uri)
        call = Call(
            call_id=uuid.uuid4().hex,
            role="uac",
            local_uri=f"sip:{from_user}@127.0.0.1:{self.port}",
            remote_uri=to_uri,
            remote_addr=addr,
            local_tag=uuid.uuid4().hex[:10],
        )
        await call.rtp.open()
        self.calls[call.call_id] = call
        call.invite_branch = _branch()
        sdp = sdp_offer(call.rtp.port, random.randint(1, 2**31))
        call.invite = SipMessage(
            f"INVITE {to_uri} SIP/2.0",
            [
                ("via", self._via(call.invite_branch)),
                ("max-forwards", "70"),
                ("from", f"<{call.local_uri}>;tag={call.local_tag}"),
                ("to", f"<{to_uri}>"),
                ("call-id", call.call_id),
                ("cseq", "1 INVITE"),
                ("contact", f"<sip:{from_user}@127.0.0.1:{self.port}>"),
                ("allow", "INVITE, ACK, CANCEL, BYE, REFER, NOTIFY, OPTIONS"),
                ("content-type", "application/sdp"),
            ],
            sdp,
        )
        self._send(call.invite, addr)
        return call

    def cancel(self, call: Call) -> None:
        assert call.invite is not None
        headers = [h for h in call.invite.headers if h[0] in ("via", "from", "to", "call-id")]
        headers += [("cseq", "1 CANCEL"), ("max-forwards", "70")]
        self._send(
            SipMessage(call.invite.start.replace("INVITE", "CANCEL", 1), headers), call.remote_addr
        )

    def hangup(self, call: Call) -> None:
        if call.state == "established":
            self._send(self._in_dialog(call, "BYE"), call.remote_addr)
            self._end(call, "local_bye")
        elif call.state in ("calling", "ringing") and call.role == "uac":
            self.cancel(call)

    def _ack(self, call: Call, resp: SipMessage) -> None:
        assert call.invite is not None
        if 200 <= resp.status < 300:
            ack = self._in_dialog(call, "ACK")
            ack.headers = [(k, v if k != "cseq" else "1 ACK") for k, v in ack.headers]
            call.cseq -= 1
        else:  # non-2xx ACK is hop-by-hop: same branch as the INVITE
            headers = [
                ("via", self._via(call.invite_branch)),
                ("max-forwards", "70"),
                ("from", call.invite.get("from") or ""),
                ("to", resp.get("to") or ""),
                ("call-id", call.call_id),
                ("cseq", "1 ACK"),
            ]
            ack = SipMessage(call.invite.start.replace("INVITE", "ACK", 1), headers)
        self._send(ack, call.remote_addr)

    def _on_response(self, resp: SipMessage, addr: tuple[str, int]) -> None:
        call = self.calls.get(resp.call_id)
        if call is None or resp.cseq_method != "INVITE":
            return
        if resp.status < 200:
            if resp.status in (180, 183):
                call.state = "ringing"
            return
        call.remote_tag = _tag(resp.get("to"))
        if call.final_status is not None:  # retransmitted final response
            if resp.status < 300:
                self._ack(call, resp)
            return
        call.final_status = resp.status
        self._ack(call, resp)
        if resp.status < 300:
            call.remote_target = _uri(resp.get("contact") or call.remote_uri)
            call.rtp.start(sdp_remote(resp.body))
            call.state = "established"
            call.answered_at = time.monotonic()
            call.established.set()
        else:
            self._end(call, f"failed_{resp.status}")

    # --- UAS ---------------------------------------------------------------------------
    def _on_request(self, req: SipMessage, addr: tuple[str, int]) -> None:
        call = self.calls.get(req.call_id)
        m = req.method
        if m == "INVITE" and call is None:
            self._spawn(self._incoming(req, addr))
        elif m == "ACK":
            if call is not None and call.state == "answering":
                call.state = "established"
                call.answered_at = time.monotonic()
                call.established.set()
        elif m == "CANCEL":
            self._send(self._response(req, 200, "OK"), addr)
            if call is not None and call.invite is not None and call.state != "established":
                self._send(
                    self._response(call.invite, 487, "Request Terminated", to_tag=call.local_tag),
                    addr,
                )
                self._end(call, "cancelled")
        elif m == "BYE":
            self._send(self._response(req, 200, "OK"), addr)
            if call is not None:
                self._end(call, "remote_bye")
        elif m == "REFER" and call is not None:
            self._send(self._response(req, 202, "Accepted"), addr)
            call.refer_to = _uri(req.get("refer-to") or "")
            self._spawn(self._transfer(call))
        elif m in ("OPTIONS", "NOTIFY", "INFO", "UPDATE"):
            self._send(self._response(req, 200, "OK"), addr)
        else:
            self._send(self._response(req, 405, "Method Not Allowed"), addr)

    async def _incoming(self, req: SipMessage, addr: tuple[str, int]) -> None:
        user = _user(req.get("to") or req.start.split(" ")[1])
        policy = self.lines.get(user) or self.lines.get("*") or LinePolicy()
        call = Call(
            call_id=req.call_id,
            role="uas",
            local_uri=_uri(req.get("to") or ""),
            remote_uri=_uri(req.get("from") or ""),
            remote_addr=addr,
            local_tag=uuid.uuid4().hex[:10],
            remote_tag=_tag(req.get("from")),
            remote_target=_uri(req.get("contact") or req.get("from") or ""),
            invite=req,
            state="ringing",
        )
        call.cseq = 100
        self.calls[call.call_id] = call
        self._send(self._response(req, 100, "Trying"), addr)
        if policy.action in ("busy", "reject", "unavailable"):
            code, reason = {
                "busy": (486, "Busy Here"),
                "reject": (603, "Decline"),
                "unavailable": (503, "Service Unavailable"),
            }[policy.action]
            self._send(self._response(req, code, reason, to_tag=call.local_tag), addr)
            call.final_status = code
            self._end(call, policy.action)
            return
        self._send(self._response(req, 180, "Ringing", to_tag=call.local_tag), addr)
        if policy.action == "no_answer":
            return  # ring until the caller CANCELs
        await asyncio.sleep(policy.ring_s)
        if call.ended.is_set():
            return
        await call.rtp.open()
        body = sdp_offer(call.rtp.port, random.randint(1, 2**31))
        call.state = "answering"
        call.final_status = 200
        self._send(self._response(req, 200, "OK", to_tag=call.local_tag, body=body), addr)
        call.rtp.start(sdp_remote(req.body))
        if policy.say is not None:
            await asyncio.sleep(0.5)
            call.rtp.say(policy.say)

    # --- transferee (REFER) -------------------------------------------------------------
    async def _transfer(self, call: Call) -> None:
        def notify(frag: str, terminated: bool) -> None:
            state = "terminated;reason=noresource" if terminated else "active;expires=60"
            msg = self._in_dialog(
                call,
                "NOTIFY",
                body=f"SIP/2.0 {frag}\r\n",
                extra=[
                    ("event", "refer"),
                    ("subscription-state", state),
                    ("content-type", "message/sipfrag;version=2.0"),
                ],
            )
            self._send(msg, call.remote_addr)

        notify("100 Trying", False)
        target = call.refer_to or ""
        new = await self.call(target, from_user=_user(call.local_uri) or self.name)
        call.transfer_call = new
        done = asyncio.ensure_future(new.ended.wait())
        est = asyncio.ensure_future(new.established.wait())
        await asyncio.wait(
            {done, est}, timeout=self.transfer_timeout_s, return_when=asyncio.FIRST_COMPLETED
        )
        for f in (done, est):
            f.cancel()
        if new.established.is_set():
            call.transfer_outcomes.append(200)
            notify("200 OK", True)
            # like a carrier bridging the REFER: drop the transferor's leg if it doesn't
            await asyncio.sleep(3)
            if not call.ended.is_set():
                self.hangup(call)
        else:
            if new.final_status is None:  # no final response in time
                self.cancel(new)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(new.ended.wait(), 5)
            code = new.final_status or 408
            call.transfer_outcomes.append(code)
            notify(f"{code} {'Busy Here' if code == 486 else 'Failed'}", True)

    def _end(self, call: Call, reason: str) -> None:
        if call.ended.is_set():
            return
        call.state = "ended"
        call.end_reason = reason
        call.rtp.close()
        call.ended.set()


async def wait_for(predicate: Any, timeout: float, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


__all__ = [
    "Call",
    "LinePolicy",
    "RtpSession",
    "SipEndpoint",
    "ulaw_decode",
    "ulaw_encode",
    "wait_for",
]
