"""Congestion-Aware Engine (core host).

Honest capability statement: switching the kernel congestion controller
(BBR <-> Cubic <-> Vegas) requires CAP_NET_ADMIN / privileged sysctls that
unprivileged containers (Railway, and the platform's own process driver)
do not grant — no userspace process can do it for another socket. This
engine does what IS possible in userspace, for real:

  * measures per-dial connect RTT and the host TCP retransmit ratio
    (/proc/net/snmp, readable in the container) as a loss estimate
  * adapts the socket parameters userspace owns on every dial it wraps:
    SO_SNDBUF/SO_RCVBUF, TCP_NODELAY, TCP_QUICKACK toggling — tuning
    window sizes toward the observed BDP and de-bufferbloating when loss
    is high
  * emits an advisory (bus + logs + panel) naming the CC the kernel
    SHOULD run (CC_DEFAULT vs alternatives by CC_SWITCH_THRESHOLD on the
    loss/RTT EWMAs) so the operator sees the recommendation

All thresholds and buffers come from env. The dial wrapper is installed
next to PreConnect in engines.core_host and removed on stop.
"""
from __future__ import annotations

import socket
import time

from ..base import Engine, EngineContext


class CongestionEngine(Engine):
    NAME = "Congestion"
    TITLE = "Congestion-Aware — RTT/loss EWMA + adaptive socket tuning + CC advisory"
    HANDLES = frozenset()
    HOSTS = frozenset({"core"})

    async def init(self, config: dict) -> None:
        self.rtt_ewma = 0.0          # seconds
        self.loss_ewma = 0.0         # 0..1
        self._last_retrans = self._read_retrans()
        self._last_check = time.monotonic()
        self.status.metrics.update({
            "rtt_ewma_ms": 0.0, "loss_ewma_percent": 0.0,
            "sockets_tuned": 0, "advisory": self.cfg.cc_default, "mode": "observe",
        })

    # ---- measurements ------------------------------------------------------------
    def _read_retrans(self) -> tuple[int, int] | None:
        """(RetransSegs, OutSegs) from /proc/net/snmp — global counters of the
        container's network namespace; good enough as a loss estimate."""
        try:
            with open("/proc/net/snmp", "r", encoding="ascii", errors="replace") as fh:
                lines = fh.read().splitlines()
            for i, line in enumerate(lines):
                if line.startswith("Tcp:"):
                    keys = line.split()
                    if keys[0] == "Tcp:" and i + 1 < len(lines):
                        values = lines[i + 1].split()
                        header = [k for k in keys[1:]]
                        row = dict(zip(header, values[1:]))
                        return int(row.get("RetransSegs", 0)), int(row.get("OutSegs", 0))
        except (OSError, ValueError):
            return None
        return None

    def observe_connect(self, elapsed_s: float) -> None:
        if elapsed_s <= 0:
            return
        self.rtt_ewma = elapsed_s if self.rtt_ewma == 0 else \
            0.7 * self.rtt_ewma + 0.3 * elapsed_s
        now = time.monotonic()
        if now - self._last_check >= self.cfg.cc_rtt_window_sec:
            self._last_check = now
            current = self._read_retrans()
            if current and self._last_retrans:
                d_retrans = max(0, current[0] - self._last_retrans[0])
                d_out = max(1, current[1] - self._last_retrans[1])
                loss = min(1.0, d_retrans / d_out)
                self.loss_ewma = 0.6 * self.loss_ewma + 0.4 * loss if self.loss_ewma else loss
            self._last_retrans = current or self._last_retrans
            self._update_advisory()
        self.status.metrics["rtt_ewma_ms"] = round(self.rtt_ewma * 1000, 1)
        self.status.metrics["loss_ewma_percent"] = round(self.loss_ewma * 100, 3)

    def _update_advisory(self) -> None:
        # CC_SWITCH_THRESHOLD: loss percent above which the advisory flips
        # from the default (bbr for Iran) to loss-based/conservative.
        threshold = max(0.1, self.cfg.cc_switch_threshold)
        loss_pct = self.loss_ewma * 100
        if loss_pct > threshold * 4:
            advisory, mode = "westwood/vegas-class (severe loss)", "degrade-hard"
        elif loss_pct > threshold:
            advisory, mode = "cubic (loss-based)", "degrade"
        else:
            advisory, mode = self.cfg.cc_default, "observe"
        if advisory != self.status.metrics.get("advisory"):
            self.log.info(f"congestion advisory -> {advisory} "
                          f"(rtt={self.rtt_ewma*1000:.0f}ms loss={loss_pct:.2f}%)")
            self.bus.publish("engine.lifecycle", {
                "action": "cc-advisory", "name": self.NAME, "advisory": advisory})
        self.status.metrics["advisory"] = advisory
        self.status.metrics["mode"] = mode

    # ---- socket tuning --------------------------------------------------------------
    def tune(self, writer) -> None:
        """Userspace-controllable socket parameters, adapted by measurement."""
        try:
            sock = writer.transport.get_extra_info("socket")
            if sock is None:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            buf = int(self.cfg.cc_sock_buf)
            if self.loss_ewma * 100 > self.cfg.cc_switch_threshold:
                # high loss: smaller buffers reduce bufferbloat/repair cost
                buf = buf // 2
            elif self.rtt_ewma > 0.15:
                # long fat pipe: grow toward bandwidth-delay product
                buf = buf * 2
            buf = max(16384, min(4 * 1024 * 1024, buf))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, buf)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, buf)
            if self.cfg.cc_quickack and hasattr(socket, "TCP_QUICKACK"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_QUICKACK, 1)
            self.status.metrics["sockets_tuned"] += 1
        except OSError:
            pass

    # ---- called by the core host dial wrapper -------------------------------------
    def note_dial(self, elapsed_s: float, writer=None) -> None:
        """One completed destination dial: measure, maybe re-advise, tune socket."""
        self.observe_connect(elapsed_s)
        if writer is not None:
            self.tune(writer)

    async def process(self, ctx: EngineContext) -> EngineContext:
        return ctx

    def defaults(self) -> dict:
        return {
            "CC_DEFAULT": self.cfg.cc_default,
            "CC_SWITCH_THRESHOLD": self.cfg.cc_switch_threshold,
            "EMUNEL_CC_RTT_WINDOW_SEC": self.cfg.cc_rtt_window_sec,
            "EMUNEL_CC_SOCK_BUF": self.cfg.cc_sock_buf,
            "note": "kernel CC switching needs privileges containers don't grant; "
                    "userspace tuning + advisory is the real scope",
        }

    def snapshot_metrics(self) -> dict:
        return dict(self.status.metrics)
