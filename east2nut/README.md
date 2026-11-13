# east2nut — EAST EA900 G4 (Intelbras DNB 6 kVA) → NUT

Publishes a UPS that speaks **only Modbus RTU** to Network UPS Tools, so that
Proxmox, PBS and Home Assistant can all see it and shut down on mains loss.

This is **not a Home Assistant add-on**. It is a systemd daemon for a Debian
host. It lives in this repository because it has no other home and because
nothing else in the house would explain where the UPS data comes from.

```
UPS --RS232/Modbus RTU--> srv-qdevice01 (Raspberry Pi, also the corosync qdevice)
                            east2nut.service   → /run/east2nut/east.dev
                            dummy-ups + upsd   → :3493
                                 ↓         ↓            ↓
                   Home Assistant    PBS/Proxmox    other clients
                   (NUT integration)  (upsmon)
```

## Why a translator exists at all

There is no NUT driver for this UPS, and that is a measured fact, not an
assumption. Tested on 2026-11-11 against the real unit:

| Attempt | Result |
|---|---|
| `nut-scanner -U` | finds nothing — the adapter is a generic CH340, not a UPS USB id |
| `nutdrv_qx` (serial) | sent `Q1` six times, `read: timeout (0)`, **`Device not supported!`** |
| `blazer_ser` | tried `Q1`, `D` and the zinto protocol → **`No supported UPS detected`** |
| hand-built Modbus `FC04 addr 0` | `01 04 02 08 e2 3e b9` → 227.4 V, **instant reply** |

So the UPS answers Modbus and is deaf to Megatec/Q1. `dummy-ups` in
`dummy-loop` mode reads a file in `upsc` dump format, and this daemon writes
that file.

Also note there is **no `hidraw`** device: this is not a USB-HID UPS, so
`usbhid-ups` is out too.

## Design notes that are easy to get wrong

**Modbus RTU is single-master.** While this runs, nothing else may own the
serial port. In particular Home Assistant's `east_ups` custom integration must
be removed and replaced with Home Assistant's official **NUT client**
integration pointed at this host. Both cannot read the bus.

**The state file lives in RAM, not on disk.** It is rewritten every 10 s —
8640 times a day — and this host boots from an SD card (`/dev/mmcblk0p2`).
Writing it to `/etc` would wear the card out and would leave the one file most
likely to be mid-write when the UPS finally dies sitting on persistent storage.
`RuntimeDirectory=east2nut` puts it on `/run`, which is `tmpfs`.
`RuntimeDirectoryPreserve=yes` keeps the directory across a daemon restart so
`dummy-ups` never loses its file.

**Writes are queued, not written from the MQTT callback.** Commands arrive on
paho's network thread, but the bus has a single master, so they are queued and
executed by the poll loop.

**`ups.status` comes from the battery and rectifier state, not from mains
voltage.** The rectifier reports battery mode and the battery reports
discharging at the moment of transfer, which is the earliest signal available.

**`battery.runtime` is in seconds.** The Modbus register is in minutes.

## MQTT is optional and additive

NUT's variable namespace covers everything `upsmon` needs and everything Home
Assistant's NUT integration can turn into sensors — including the four inputs
the battery degradation analysis consumes (`battery.voltage`,
`battery.current`, `ups.realpower`, `ups.status`).

What it does not cover is the rest of the register map, and it offers no way to
*write* to the UPS, because `dummy-ups` only reads a file. So the daemon also
publishes, when credentials are present:

- **23 sensors** — inverter temperature and current, cell float/boost voltage,
  power factors, reactive power, rated power, maintenance cycle, run days,
  status word, and the rectifier/inverter/bypass/load/battery statuses as text.
- **9 buttons** — including **Battery Test**, which writes Modbus `FC06` to
  registers `0x8000`–`0x8007`. This is the only way to measure real autonomy,
  and NUT alone cannot do it.

Without `/etc/east2nut.conf` the daemon skips all of it silently and the NUT
half keeps working.

## Install

```bash
apt-get install -y --no-install-recommends \
    python3-minimal python3-serial python3-paho-mqtt nut-server nut-client
usermod -aG dialout nut          # the driver runs as `nut` and needs the port

install -m 755 east2nut.py       /usr/local/sbin/east2nut.py
install -m 644 east2nut.service  /etc/systemd/system/east2nut.service
install -m 644 -o root -g nut nut/ups.conf  /etc/nut/ups.conf
install -m 644 -o root -g nut nut/upsd.conf /etc/nut/upsd.conf
# fill in the passwords first:
install -m 640 -o root -g nut nut/upsd.users.sample   /etc/nut/upsd.users
install -m 640 -o root -g nut nut/upsmon.conf.sample  /etc/nut/upsmon.conf

sed -i 's/^MODE=.*/MODE=netserver/' /etc/nut/nut.conf
systemctl daemon-reload
systemctl enable --now east2nut
systemctl restart nut-server && systemctl start nut-monitor
upsc east@localhost
```

Optional MQTT, `/etc/east2nut.conf`, mode 600:

```
EAST2NUT_MQTT_HOST=10.0.0.5
EAST2NUT_MQTT_PORT=1883
EAST2NUT_MQTT_USER=east2nut
EAST2NUT_MQTT_PASS=...
```

The matching Mosquitto login goes in the add-on's `logins:` option. Use a
dedicated account — never the Supervisor service account that Home Assistant's
own mqtt entry uses.

## Recording discharges

Every transfer to battery is a free load test, and this host is the only one
that can record one: a real outage takes Home Assistant down with it (it runs
as a VM on a Dell that shuts down), and even when it survives it polls NUT
about once a minute. On 2026-11-12 it missed a 16-second self-test entirely,
logging 209.6 V on a dip that actually reached 187.5 V.

So the daemon polls every 5 s normally and drops to **1 s while on battery**,
appending to `/var/lib/east2nut/discharges.csv` (persistent, via
`StateDirectory`). Each event carries three phases:

- `pre` — the samples held in memory before the transfer. **This is the part
  that makes the data usable:** without the measured float voltage immediately
  before, the sag cannot be split into its fixed float-to-rest offset and its
  ohmic `I*R` term, and dividing the whole drop by the current overstates the
  internal resistance by two to four times.
- `discharge` — one row per second while on battery.
- `recovery` — 60 s after mains returns, for the recovery curve.

Writes happen only during an event, so this is kilobytes per outage, not the
8640-writes-a-day problem that put the state file on tmpfs.

`nobreak-degracao.py` consumes it with `--fetch`. It fits the hourly Home
Assistant statistics and the 1 s events as **separate series** — they measure
different hardware (the bank was replaced on 2026-11-11) at very different
quality — and reports standard errors and R² for each. That matters: the old
bank's 20 hourly events give 70 ±44 mOhm/block with R²=0.12, a range that
straddles the healthy band. Reported bare, that number reads like a measurement
and is not one. The deployed copy lives at
`/homeassistant/scripts/nobreak-degradacao.py`.

## The Pi policy (unresolved, on purpose)

`upsmon` on this host runs as `primary` with a **no-op `SHUTDOWNCMD`**. That is
a deliberate open question, not an oversight. Three things pull against each
other:

1. **Keep the signal alive.** This host is the NUT server. If it halts first,
   every client loses the UPS status at the worst possible moment.
2. **Keep quorum alive.** It is also the corosync qdevice — the tie-breaker. If
   it dies while nodes are still up, an even-sized cluster can lose quorum.
3. **Do not corrupt the card.** It is on the same UPS, so when the bank gives
   out it loses power uncleanly. Moving the state file to tmpfs removed the
   worst of this, but the root filesystem is still mounted `rw`.

Once clients shut down, they no longer need the signal — their `upssched` timer
has already fired. So the defensible policy is: **the Pi halts last, on a timer
long enough for every client to have started shutting down.** That number has
to come from measurement, not a guess, and measured autonomy is only about
**2 min 45 s** with this bank. Until the clients are in place and the slowest
node's shutdown has been timed, a wrong number here is worse than none — which
is why it is a no-op today.

A cheaper mitigation worth considering regardless: mount this host's root
read-only, or at least `commit=`-tune it, so a hard power cut cannot damage it.

## Context on the hardware

The bank is at end of life and the numbers matter for any shutdown policy:
internal resistance **70 mΩ/block** against a healthy 25–35, resting
**2.02 V/cell** against a healthy 2.13–2.15, `battery.runtime` reporting ~300 s
at ~30 % load, and a real outage that held **2 min 45 s**. Four transfers to
battery in November 2026, three within five hours on the 7th.

There is also **no killpower**: `dummy-ups` cannot write to the UPS and the
EA900 command set has no "turn output off", so after the clients halt the UPS
drains until it dies. Hosts must be set to power on when AC returns (Dell:
*AC Power Recovery*), or the fleet stays down after the outage.
