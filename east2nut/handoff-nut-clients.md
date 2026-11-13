# Handoff: NUT clients on PBS and Proxmox (graceful shutdown on mains loss)

You are configuring NUT **clients** on Proxmox VE nodes and a Proxmox Backup
Server. The NUT **server already exists and works** — do not rebuild it, and do
not touch the machine that runs it.

## What already exists (all verified 2026-11-11)

**Address everything by IP, never by hostname.** On 2026-11-11 this host's
dynamic DNS record vanished after a reboot and Home Assistant lost the UPS
entirely, while the MQTT side -- configured with an IP -- reconnected on its
own. In a real outage the DNS server is on the same grid, so a name is a
dependency you cannot rely on at exactly the moment you need it.

**NUT server:** `192.168.0.5` (`srv-qdevice01`) — a Raspberry Pi (Debian 13
trixie, aarch64) that is also the **corosync qdevice** for the cluster.

- UPS name in NUT: **`east`**  → address it as `east@192.168.0.5.fortress.internal`
- `upsd` listens on `0.0.0.0:3493`
- The UPS is an **EAST EA900 G4 / Intelbras DNB 6 kVA**, 16 × 12 V VRLA blocks.
- It speaks **only Modbus RTU**. `nutdrv_qx` and `blazer_ser` were both tested
  against the real unit and both failed to detect it. A daemon
  (`east2nut.service`, `/usr/local/sbin/east2nut.py`) reads Modbus and writes
  `/etc/nut/east.dev`, which the `dummy-ups` driver republishes. This is why
  there is no real NUT driver — that is expected, not a misconfiguration.
- Reads are **anonymous**: `upsc east@192.168.0.5.fortress.internal` works with
  no credentials. `upsmon` still needs a login.
- Credentials for `upsmon`: user **`upsmon`**, password in
  **`/root/.nut-upsmon-pass` on `srv-qdevice01`** (mode 600). Fetch it from
  there. Do not invent one and do not change the server's `upsd.users`.
- The server's own `upsmon` runs as **primary** with a deliberately **no-op
  `SHUTDOWNCMD`** (it only calls `logger`). Leave it that way unless explicitly
  asked — see "The Pi goes last" below.

## The constraint that drives every decision

**Measured autonomy is about 2 minutes 45 seconds**, not the nameplate figure.
The battery bank is at end of life: internal resistance 70 mΩ per block against
a healthy 25–35, resting 2.02 V/cell against a healthy 2.13–2.15, and
`battery.runtime` currently reports ~300 s at ~30 % load. There were four
transfers to battery in November 2026, three of them within five hours on the
7th. Replacement is pending and is not your task.

Consequences you must design around:

1. **Do not wait for `LB` (low battery).** The default `upsmon` policy acts on
   `OB LB`. On this bank `LB` may arrive with 20 seconds left, or not in time at
   all. Trigger on **`ONBATT` plus a short dwell** using `upssched`.
2. Budget: ~15 s dwell to reject a blink, then 30–90 s for a PowerEdge-class
   host to halt. That is most of the window. Do not add further delays.
3. **There is no killpower.** `dummy-ups` cannot write to the UPS, and the
   EA900's command set has no "turn output off". The UPS will drain until it
   dies. So verify each host's BIOS/iDRAC is set to power on when AC returns
   (Dell: *AC Power Recovery* = On, or Last State), otherwise the fleet stays
   down after the outage. **Report what you find; do not change BIOS settings
   without asking.**

## Goal

On each Proxmox VE node and on the PBS host:

1. Install `nut-client` only (NOT `nut-server`).
2. `MONITOR east@192.168.0.5.fortress.internal 1 upsmon <password> secondary`
   — **`secondary`**, never `primary`. Only the Pi is primary.
3. `upssched` with `AT ONBATT * START-TIMER <name> 15` and
   `AT ONLINE * CANCEL-TIMER <name>`, so a 15-second outage starts shutdown and
   a blink cancels it.
4. The timer handler must do the host-appropriate work **before** halting:
   - **Proxmox VE:** stop or shut down guests in a sensible order, then halt the
     node. Decide between `qm shutdown` / `pct shutdown` per guest and Proxmox's
     own shutdown policy — inspect what is configured and prefer the native
     mechanism if one is already set up. Do not attempt live migration: with
     mains gone there is nowhere to migrate to.
   - **PBS:** stop any running backup/verify/GC task cleanly rather than letting
     it be killed mid-write, then halt. Check `proxmox-backup-manager task list`
     for what is in flight.
5. Make sure `MINSUPPLIES 1`, `POLLFREQ 5`, `POLLFREQALERT 5` and a `DEADTIME`
   of about 15 s are set, so losing contact with the Pi is noticed rather than
   silently trusted.

## The Pi goes last

`srv-qdevice01` is both the NUT server and the corosync qdevice. If it halts
first, every client loses the UPS signal exactly when it matters, and the cluster
loses its quorum arbitrator. Its `SHUTDOWNCMD` is a no-op on purpose. If a
policy for it is wanted, it must fire **after** the nodes are down, and that is a
separate decision — raise it, do not implement it unprompted.

## Verification you must actually perform

Do not declare success on config files alone.

1. `upsc east@192.168.0.5.fortress.internal` from each client — must return
   `ups.status: OL ...` and live values that change between two reads.
2. `upsmon -K` (config check) and the service active with no auth errors in
   `journalctl -u nut-monitor`.
3. **Dry-run the timer path without halting anything:** temporarily point the
   timer handler at `logger` instead of the real shutdown, then simulate. You
   can simulate on the server side by stopping `east2nut.service` on the Pi for
   ~90 s, which makes the `.dev` file go stale and then reports
   `ups.status: OFF` — this exercises `DEADTIME`, not `ONBATT`. **Coordinate
   before doing that: it blinds every client while it runs.** To exercise
   `ONBATT` properly you need a real transfer, so the honest test is the next
   outage, and the dry-run with `logger` is what tells you the plumbing fires.
4. Confirm `upssched` cancels on `ONLINE`: a blink must not leave a timer armed.

## Report back

- Which hosts were configured, with their roles and OS versions.
- The exact `upsmon.conf`, `upssched.conf` and timer handler you installed.
- The result of each verification step, with the commands and their output.
- Each host's AC-power-recovery setting as found (do not change it).
- Anything you chose differently from this brief, and why.

## Out of scope

Replacing the battery bank; changing anything on `srv-qdevice01`; changing
`east2nut.py`; changing the Home Assistant side (it is already a NUT client of
this server and works); iDRAC-based forced shutdown (a possible later backstop,
by timeout only, not the primary mechanism).
