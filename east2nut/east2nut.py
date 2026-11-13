#!/usr/bin/python3
"""Publish an EAST EA900 G4 (Intelbras DNB 6kVA) to NUT.

The UPS speaks Modbus RTU and nothing else. Verified on 2026-11-11 against the
real unit: nutdrv_qx sent Q1 six times and timed out ("Device not supported!"),
blazer_ser tried Q1, 'D' and the zinto protocol and gave "No supported UPS
detected", while a hand-built Modbus FC04 frame answered immediately. There is
no native NUT driver, so this daemon translates:

    UPS --Modbus RTU--> east2nut --> /etc/nut/east.dev --> dummy-ups --> upsd

Modbus RTU is single-master. While this runs, nothing else may own the port --
in particular Home Assistant's east_ups integration, which has to be replaced by
Home Assistant's official NUT client integration pointed at this host.

Raw Modbus is implemented here on purpose: the only dependency is pyserial, so
this stays installable on a quorum device without pulling a stack in.

Register map and the status-word bit layout are ported from the east_ups custom
component (custom_components/east_ups/const.py and coordinator.py).
"""

import collections
import csv
import datetime
import json
import os
import queue
import struct
import sys
import time

import serial

try:
    import paho.mqtt.client as mqtt
except ImportError:  # the NUT half must keep working without it
    mqtt = None

PORT = os.environ.get("EAST2NUT_PORT", "/dev/ttyUSB0")
BAUD = int(os.environ.get("EAST2NUT_BAUD", "9600"))
SLAVE = int(os.environ.get("EAST2NUT_SLAVE", "1"))
# tmpfs, NOT /etc. This file is rewritten every INTERVAL seconds -- 8640 times
# a day -- and the host boots from an SD card, so putting it on the root
# filesystem would wear the card out and leave the one file most likely to be
# mid-write when the UPS finally dies sitting on persistent storage. systemd
# provides the directory via RuntimeDirectory=east2nut.
OUT = os.environ.get("EAST2NUT_OUT", "/run/east2nut/east.dev")
INTERVAL = float(os.environ.get("EAST2NUT_INTERVAL", "5"))
# While on battery, poll hard. A battery self-test lasts about 20 s and a real
# transfer can be shorter: at 5 s that is three or four samples, which is not
# enough to see the knee of the discharge curve.
INTERVAL_OB = float(os.environ.get("EAST2NUT_INTERVAL_OB", "1"))
# dummy-loop re-reads the file every TIMER seconds, so keep it at or below the
# normal poll interval.
TIMER = int(os.environ.get("EAST2NUT_TIMER", "2"))

# Every discharge is a free load test, and the only machine that survives a real
# outage to record one is this Pi: Home Assistant runs as a VM on a Dell that
# shuts down, and it polls NUT every ~60 s anyway -- on 2026-11-12 it missed a
# 16 s self-test entirely, logging 209.6 V when the bank actually sagged to
# 187.5 V. So the event log lives here, on persistent storage (systemd
# StateDirectory). Writes happen only during an event, so this is a handful of
# kilobytes per outage, not the 8640-writes-a-day problem that put the state
# file on tmpfs.
EVENTS = os.environ.get("EAST2NUT_EVENTS", "/var/lib/east2nut/discharges.csv")
# Samples kept in memory so an event can be written with the float voltage that
# preceded it -- without that, the drop cannot be separated into its fixed
# offset and its ohmic part, and the resistance estimate is meaningless.
PRE_SAMPLES = int(os.environ.get("EAST2NUT_PRE_SAMPLES", "12"))
# Keep logging after the return to mains, to capture the recovery curve.
POST_SECONDS = float(os.environ.get("EAST2NUT_POST_SECONDS", "60"))

# Low-battery thresholds. Deliberately generous: this bank measured 70 mOhm per
# block and 2.02 V/cell at rest in 11/2026, and a real outage held only 2m45s,
# so the UPS's own runtime estimate is the number that matters, not charge %.
LB_RUNTIME_MIN = float(os.environ.get("EAST2NUT_LB_RUNTIME_MIN", "2"))
LB_CHARGE_PCT = float(os.environ.get("EAST2NUT_LB_CHARGE_PCT", "25"))

# MQTT is optional and additive. NUT carries what upsmon and Home Assistant's
# NUT integration need; this republishes the registers NUT has no name for, plus
# the nine control commands, which dummy-ups can never do because it only reads
# a file. Without credentials the daemon simply skips all of it.
MQTT_HOST = os.environ.get("EAST2NUT_MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("EAST2NUT_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("EAST2NUT_MQTT_USER", "")
MQTT_PASS = os.environ.get("EAST2NUT_MQTT_PASS", "")
TOPIC = os.environ.get("EAST2NUT_MQTT_TOPIC", "east2nut")
DISCOVERY = os.environ.get("EAST2NUT_MQTT_DISCOVERY", "homeassistant")


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


class Modbus:
    """Minimal Modbus RTU master: just the two read function codes we need."""

    def __init__(self, port, baud, slave):
        self.slave = slave
        self.ser = serial.Serial(port, baud, bytesize=8, parity="N",
                                 stopbits=1, timeout=1.5)

    def _read(self, fc, address, count):
        req = struct.pack(">BBHH", self.slave, fc, address, count)
        c = crc16(req)
        self.ser.reset_input_buffer()
        self.ser.write(req + bytes([c & 0xFF, (c >> 8) & 0xFF]))
        want = 3 + 2 * count + 2
        got = self.ser.read(want)
        if len(got) != want:
            raise IOError("short reply: wanted %d, got %d" % (want, len(got)))
        if got[0] != self.slave or got[1] != fc:
            raise IOError("unexpected header %02x %02x" % (got[0], got[1]))
        if crc16(got[:-2]) != (got[-1] << 8 | got[-2]):
            raise IOError("CRC mismatch")
        return list(struct.unpack(">%dH" % count, got[3:3 + 2 * count]))

    def input_regs(self, address, count):
        return self._read(0x04, address, count)

    def holding_regs(self, address, count):
        return self._read(0x03, address, count)

    def write_reg(self, address, value):
        """Modbus FC06. The echo of a write is the request itself."""
        req = struct.pack(">BBHH", self.slave, 0x06, address, value)
        c = crc16(req)
        self.ser.reset_input_buffer()
        self.ser.write(req + bytes([c & 0xFF, (c >> 8) & 0xFF]))
        got = self.ser.read(8)
        if len(got) != 8:
            raise IOError("write not acknowledged (got %d bytes)" % len(got))
        if crc16(got[:-2]) != (got[-1] << 8 | got[-2]):
            raise IOError("CRC mismatch on write echo")
        return True

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


def s16(v):
    """Reinterpret an unsigned 16-bit register as signed."""
    return v - 0x10000 if v & 0x8000 else v


def regs_to_string(values):
    out = []
    for reg in values:
        out.append(chr((reg >> 8) & 0xFF))
        out.append(chr(reg & 0xFF))
    return "".join(out).replace("\x00", "").strip()


# Bit layout of input register 70, ported from coordinator._parse_status_word.
BATTERY_DISCONNECTED, BATTERY_STANDBY = 0, 1
BATTERY_BOOST, BATTERY_FLOAT, BATTERY_DISCHARGING = 2, 3, 4
RECTIFIER_BATTERY_MODE = 3
LOAD_ON_BYPASS, LOAD_ON_INVERTER = 1, 2


def read_all(mb):
    """One full poll. Three batched reads instead of 34 round trips."""
    # Input registers 0..71 are contiguous; one FC04 covers the lot.
    i = mb.input_regs(0, 72)
    # Holding registers come in two clusters.
    h1 = mb.holding_regs(5, 23)    # 5..27
    h2 = mb.holding_regs(76, 7)    # serial number

    def inp(addr, scale=1.0):
        return s16(i[addr]) * scale

    def hold(addr, scale=1.0):
        return s16(h1[addr - 5]) * scale

    status_word = i[70]
    d = {
        "bypass_voltage": inp(0, 0.1),
        "bypass_current": inp(3, 0.1),
        "bypass_frequency": inp(6, 0.1),
        "bypass_power_factor": inp(9, 0.01),
        "input_voltage": inp(12, 0.1),
        "input_current": inp(15, 0.1),
        "input_frequency": inp(18, 0.1),
        "input_power_factor": inp(21, 0.01),
        "output_voltage": inp(24, 0.1),
        "output_current": inp(27, 0.1),
        "output_frequency": inp(30, 0.1),
        "output_power_factor": inp(33, 0.01),
        "output_apparent_power": inp(36, 100),
        "output_active_power": inp(39, 100),
        "output_reactive_power": inp(42, 100),
        "load_percentage": inp(45, 0.1),
        "battery_voltage": inp(49, 0.1),
        "battery_current": inp(51, 0.1),
        "battery_remaining_time": inp(54, 1),
        "battery_remaining_capacity": inp(55, 0.1),
        "inverter_current": inp(56, 0.1),
        "rectifier_temperature": inp(57, 0.1),
        "inverter_temperature": inp(58, 0.1),
        "ups_run_days": inp(59, 1),
        "software_version": regs_to_string(i[67:70]),
        "status_word": status_word,
        "rectifier_status": status_word & 0x03,
        "inverter_status": (status_word >> 2) & 0x03,
        "battery_status": (status_word >> 4) & 0x07,
        "bypass_status": (status_word >> 7) & 0x03,
        "load_on_status": (status_word >> 9) & 0x03,
        "rated_power": hold(15),
        "battery_count": hold(5),
        "cell_float_voltage": hold(7, 0.01),
        "cell_boost_voltage": hold(8, 0.01),
        "battery_maintenance_cycle": hold(26),
        "running_time": hold(27, 7),
        "serial_number": regs_to_string(h2),
    }
    return d


def ups_status(d):
    """Map the status word onto NUT's ups.status flags.

    OB is driven by the battery/rectifier state rather than by mains voltage:
    the rectifier reports battery mode and the battery reports discharging at
    the moment of transfer, which is the earliest signal available. upssched on
    the clients is what turns OB into a shutdown, after a dwell.
    """
    flags = []
    on_battery = (d["battery_status"] == BATTERY_DISCHARGING
                  or d["rectifier_status"] == RECTIFIER_BATTERY_MODE)
    if on_battery:
        flags.append("OB")
    elif d["load_on_status"] == LOAD_ON_BYPASS:
        flags.append("BYPASS")
    else:
        flags.append("OL")

    if on_battery and (d["battery_remaining_time"] <= LB_RUNTIME_MIN
                       or d["battery_remaining_capacity"] <= LB_CHARGE_PCT):
        flags.append("LB")

    if d["battery_status"] in (BATTERY_BOOST, BATTERY_FLOAT):
        flags.append("CHRG")
    elif d["battery_status"] == BATTERY_DISCHARGING:
        flags.append("DISCHRG")
    if d["battery_status"] == BATTERY_DISCONNECTED:
        flags.append("RB")  # no battery bank answering: needs attention
    return " ".join(flags)


def render(d):
    """Build the dummy-ups .dev file: a upsc-style dump.

    Standard NUT names first -- those are the ones Home Assistant's NUT
    integration turns into sensors, and they cover every input the battery
    degradation analysis uses (battery.voltage, battery.current, ups.realpower
    and ups.status). The experimental.* block carries the registers NUT has no
    name for; dummy-ups publishes them anyway ("Unknown data. Committing
    anyway..."), so upsc on the clients sees them even though Home Assistant,
    which has a fixed sensor map, will not.
    """
    lines = [
        "device.mfr: EAST",
        "device.model: EA900 G4",
        "device.serial: %s" % d["serial_number"],
        "device.type: ups",
        "ups.mfr: EAST",
        "ups.model: EA900 G4",
        "ups.serial: %s" % d["serial_number"],
        "ups.firmware: %s" % d["software_version"],
        "ups.status: %s" % ups_status(d),
        "ups.load: %.1f" % d["load_percentage"],
        "ups.realpower: %.0f" % d["output_active_power"],
        "ups.realpower.nominal: %.0f" % d["rated_power"],
        "ups.power: %.0f" % d["output_apparent_power"],
        "ups.power.nominal: %.0f" % d["rated_power"],
        "ups.temperature: %.1f" % d["rectifier_temperature"],
        "input.voltage: %.1f" % d["input_voltage"],
        "input.current: %.1f" % d["input_current"],
        "input.frequency: %.1f" % d["input_frequency"],
        "input.bypass.voltage: %.1f" % d["bypass_voltage"],
        "input.bypass.frequency: %.1f" % d["bypass_frequency"],
        "output.voltage: %.1f" % d["output_voltage"],
        "output.current: %.1f" % d["output_current"],
        "output.frequency: %.1f" % d["output_frequency"],
        "output.powerfactor: %.2f" % d["output_power_factor"],
        "battery.voltage: %.1f" % d["battery_voltage"],
        "battery.current: %.1f" % d["battery_current"],
        "battery.charge: %.1f" % d["battery_remaining_capacity"],
        # NUT counts runtime in SECONDS; the register is in minutes.
        "battery.runtime: %.0f" % (d["battery_remaining_time"] * 60),
        "battery.packs: %d" % d["battery_count"],
        "battery.type: VRLA",
        "experimental.rectifier.temperature: %.1f" % d["rectifier_temperature"],
        "experimental.inverter.temperature: %.1f" % d["inverter_temperature"],
        "experimental.inverter.current: %.1f" % d["inverter_current"],
        "experimental.cell.float.voltage: %.2f" % d["cell_float_voltage"],
        "experimental.cell.boost.voltage: %.2f" % d["cell_boost_voltage"],
        "experimental.battery.maintenance.cycle: %d" % d["battery_maintenance_cycle"],
        "experimental.ups.run.days: %d" % d["ups_run_days"],
        "experimental.ups.running.time: %d" % d["running_time"],
        "experimental.status.word: %d" % d["status_word"],
        "TIMER %d" % TIMER,
        "",
    ]
    return "\n".join(lines)


# ============================================================================
# MQTT: the registers NUT has no name for, plus the nine control commands.
# ============================================================================

# Status texts, ported from const.py's *_STATUS_TEXT maps.
RECTIFIER_TEXT = {0: "Off", 1: "Soft Start", 2: "PFC Mode", 3: "Battery Mode"}
INVERTER_TEXT = {0: "Off", 1: "Soft Start", 2: "Normal", 3: "Standby"}
BYPASS_TEXT = {0: "No Bypass", 1: "Normal"}
LOAD_ON_TEXT = {0: "None", 1: "Load on Bypass", 2: "Load on Inverter",
                3: "Load on Other"}
BATTERY_TEXT = {0: "Disconnected", 1: "Standby", 2: "Boost Charging",
                3: "Float Charging", 4: "Discharging"}

# (key, friendly name, unit, device_class, state_class, icon)
# Only what the NUT bridge drops: everything here is absent from Home
# Assistant's NUT integration, which has a fixed sensor map.
MQTT_SENSORS = [
    ("inverter_temperature", "Inversor Temperatura", "°C", "temperature", "measurement", None),
    ("rectifier_temperature", "Retificador Temperatura", "°C", "temperature", "measurement", None),
    ("inverter_current", "Inversor Corrente", "A", "current", "measurement", None),
    ("cell_float_voltage", "Celula Tensao de Flutuacao", "V", "voltage", "measurement", None),
    ("cell_boost_voltage", "Celula Tensao de Boost", "V", "voltage", "measurement", None),
    ("bypass_current", "Bypass Corrente", "A", "current", "measurement", None),
    ("bypass_frequency", "Bypass Frequencia", "Hz", "frequency", "measurement", None),
    ("bypass_power_factor", "Bypass Fator de Potencia", None, "power_factor", "measurement", None),
    ("input_power_factor", "Entrada Fator de Potencia", None, "power_factor", "measurement", None),
    ("output_power_factor", "Saida Fator de Potencia", None, "power_factor", "measurement", None),
    ("output_reactive_power", "Saida Potencia Reativa", "var", "reactive_power", "measurement", None),
    ("rated_power", "Potencia Nominal", "VA", "apparent_power", None, None),
    ("battery_maintenance_cycle", "Ciclo de Manutencao da Bateria", "d", None, None, "mdi:calendar-clock"),
    ("ups_run_days", "Dias Ligado", "d", None, "total_increasing", "mdi:counter"),
    ("running_time", "Tempo de Funcionamento", "d", None, None, "mdi:timer-outline"),
    ("status_word", "Status Word", None, None, None, "mdi:numeric"),
    ("rectifier_status_text", "Retificador Status", None, None, None, "mdi:current-ac"),
    ("inverter_status_text", "Inversor Status", None, None, None, "mdi:sine-wave"),
    ("bypass_status_text", "Bypass Status", None, None, None, "mdi:electric-switch"),
    ("load_on_status_text", "Carga Em", None, None, None, "mdi:transmission-tower"),
    ("battery_status_text", "Bateria Status", None, None, None, "mdi:battery-charging"),
    ("serial_number", "Numero de Serie", None, None, None, "mdi:identifier"),
    ("software_version", "Versao de Firmware", None, None, None, "mdi:chip"),
]

# (key, friendly name, modbus register, value written, icon)
# Ported from button.py. These are the real reason MQTT is here: dummy-ups reads
# a file and has no path back to the UPS, so NUT alone cannot run a battery test.
MQTT_BUTTONS = [
    ("battery_test_20s", "Iniciar Teste de Bateria (20s)", 0x8006, 1, "mdi:battery-clock"),
    ("battery_test_maintenance", "Teste de Manutencao da Bateria", 0x8006, 2, "mdi:battery-heart-variant"),
    ("stop_battery_test", "Parar Teste de Bateria", 0x8007, 1, "mdi:stop-circle-outline"),
    ("clear_fault", "Limpar Falha", 0x8000, 1, "mdi:alert-remove"),
    ("clear_history", "Limpar Historico", 0x8001, 1, "mdi:delete-sweep"),
    ("buzzer_mute", "Silenciar Alarme", 0x8002, 1, "mdi:volume-off"),
    ("buzzer_unmute", "Reativar Alarme", 0x8002, 0, "mdi:volume-high"),
    ("switch_to_bypass", "Passar para Bypass", 0x8003, 1, "mdi:electric-switch"),
    ("switch_to_inverter", "Passar para Inversor", 0x8004, 1, "mdi:sine-wave"),
]


def mqtt_payload(d):
    """Everything the MQTT half publishes, as one JSON document."""
    out = {k: d[k] for k, _, _, _, _, _ in MQTT_SENSORS if k in d}
    out["rectifier_status_text"] = RECTIFIER_TEXT.get(d["rectifier_status"], "?")
    out["inverter_status_text"] = INVERTER_TEXT.get(d["inverter_status"], "?")
    out["bypass_status_text"] = BYPASS_TEXT.get(d["bypass_status"], "?")
    out["load_on_status_text"] = LOAD_ON_TEXT.get(d["load_on_status"], "?")
    out["battery_status_text"] = BATTERY_TEXT.get(d["battery_status"], "?")
    return out


class Publisher:
    """MQTT discovery publisher plus the command path back to Modbus.

    Commands arrive on paho's network thread but the serial bus has a single
    master, so they are queued and executed by the poll loop instead of being
    written from the callback.
    """

    def __init__(self, serial_number):
        self.cmds = queue.Queue()
        self.ok = False
        self.serial = serial_number or "unknown"
        self.state_topic = "%s/state" % TOPIC
        self.avail_topic = "%s/availability" % TOPIC
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id="east2nut-%s" % self.serial)
        if MQTT_USER:
            self.client.username_pw_set(MQTT_USER, MQTT_PASS)
        self.client.will_set(self.avail_topic, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        self.client.loop_start()

    def _device(self):
        return {
            "identifiers": ["east_ea900_g4_%s" % self.serial],
            "name": "Nobreak EAST EA900 G4",
            "manufacturer": "EAST / Intelbras",
            "model": "EA900 G4 (DNB 6kVA)",
            "serial_number": self.serial,
        }

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if getattr(reason_code, "is_failure", reason_code != 0):
            print("mqtt connect failed: %s" % reason_code, file=sys.stderr, flush=True)
            self.ok = False
            return
        self.ok = True
        print("mqtt connected to %s:%d" % (MQTT_HOST, MQTT_PORT), flush=True)
        self._announce()
        client.subscribe("%s/cmd/+" % TOPIC, qos=1)
        client.publish(self.avail_topic, "online", retain=True)

    def _announce(self):
        """Publish discovery configs. object_id fixes the entity_id prefix to
        nobreak_, keeping these clear of the sensor.east_* names NUT creates."""
        dev = self._device()
        avail = [{"topic": self.avail_topic}]
        for key, name, unit, dclass, sclass, icon in MQTT_SENSORS:
            cfg = {
                "name": name,
                "object_id": "nobreak_%s" % key,
                "unique_id": "east2nut_%s_%s" % (self.serial, key),
                "state_topic": self.state_topic,
                "value_template": "{{ value_json.%s }}" % key,
                "availability": avail,
                "device": dev,
            }
            if unit:
                cfg["unit_of_measurement"] = unit
            if dclass:
                cfg["device_class"] = dclass
            if sclass:
                cfg["state_class"] = sclass
            if icon:
                cfg["icon"] = icon
            self.client.publish("%s/sensor/east2nut/%s/config" % (DISCOVERY, key),
                                json.dumps(cfg), retain=True, qos=1)
        for key, name, _reg, _val, icon in MQTT_BUTTONS:
            cfg = {
                "name": name,
                "object_id": "nobreak_%s" % key,
                "unique_id": "east2nut_%s_%s" % (self.serial, key),
                "command_topic": "%s/cmd/%s" % (TOPIC, key),
                "payload_press": "PRESS",
                "availability": avail,
                "device": dev,
            }
            if icon:
                cfg["icon"] = icon
            self.client.publish("%s/button/east2nut/%s/config" % (DISCOVERY, key),
                                json.dumps(cfg), retain=True, qos=1)
        print("mqtt discovery: %d sensores, %d botoes"
              % (len(MQTT_SENSORS), len(MQTT_BUTTONS)), flush=True)

    def _on_message(self, client, userdata, msg):
        key = msg.topic.rsplit("/", 1)[-1]
        for k, name, reg, val, _icon in MQTT_BUTTONS:
            if k == key:
                self.cmds.put((k, name, reg, val))
                return
        print("mqtt: comando desconhecido %r" % key, file=sys.stderr, flush=True)

    def publish_state(self, payload):
        if self.ok:
            self.client.publish(self.state_topic, json.dumps(payload), retain=True)

    def drain(self, mb):
        """Run queued commands on the poll loop's thread."""
        while True:
            try:
                key, name, reg, val = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                mb.write_reg(reg, val)
                print("comando %s -> reg 0x%04X = %d OK" % (key, reg, val), flush=True)
            except Exception as err:  # noqa: BLE001
                print("comando %s falhou: %s" % (key, err), file=sys.stderr, flush=True)


# ============================================================================
# Discharge recorder: every transfer to battery is a free load test.
# ============================================================================

CSV_FIELDS = [
    "timestamp", "event", "phase", "ups_status", "battery_voltage",
    "battery_current", "battery_charge", "battery_runtime_s",
    "output_active_power", "rectifier_temperature", "load_percentage",
]


def _row(when, event_id, phase, data):
    return {
        "timestamp": when,
        "event": event_id,
        "phase": phase,
        "ups_status": ups_status(data),
        "battery_voltage": "%.1f" % data["battery_voltage"],
        "battery_current": "%.1f" % data["battery_current"],
        "battery_charge": "%.1f" % data["battery_remaining_capacity"],
        "battery_runtime_s": "%.0f" % (data["battery_remaining_time"] * 60),
        "output_active_power": "%.0f" % data["output_active_power"],
        "rectifier_temperature": "%.1f" % data["rectifier_temperature"],
        "load_percentage": "%.1f" % data["load_percentage"],
    }


class DischargeLog:
    """Appends a CSV row per sample while the UPS is on battery.

    Holds the preceding samples in memory so the float voltage immediately
    before the transfer is written with the event. Without that reference the
    total sag cannot be split into the fixed float-to-rest offset and the ohmic
    I*R term, and dividing the whole drop by the current overstates the internal
    resistance by two to four times -- the mistake that produced a 147 mOhm
    reading in the first analysis of this bank.
    """

    def __init__(self, path):
        self.path = path
        self.recent = collections.deque(maxlen=PRE_SAMPLES)
        self.event_id = None
        self.ended_at = None

    def _append(self, rows):
        if not rows:
            return
        d = os.path.dirname(self.path)
        if d and not os.path.isdir(d):
            os.makedirs(d, mode=0o755, exist_ok=True)
        new = not os.path.exists(self.path)
        with open(self.path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            if new:
                w.writeheader()
            for r in rows:
                w.writerow(r)
            fh.flush()
            os.fsync(fh.fileno())

    def observe(self, data, now_iso, on_battery):
        """Returns the interval the caller should sleep before the next poll."""
        if on_battery and self.event_id is None:
            # Transfer just started: open an event and flush the pre-samples.
            self.event_id = now_iso.replace(":", "").replace("-", "")
            self.ended_at = None
            pre = [_row(t, self.event_id, "pre", d) for t, d in self.recent]
            self._append(pre + [_row(now_iso, self.event_id, "discharge", data)])
            print("descarga iniciada (evento %s): %.1f V, %.1f A, %.0f W"
                  % (self.event_id, data["battery_voltage"],
                     data["battery_current"], data["output_active_power"]),
                  flush=True)
        elif on_battery:
            self._append([_row(now_iso, self.event_id, "discharge", data)])
        elif self.event_id is not None:
            # Mains is back. Keep sampling for a while to catch the recovery.
            if self.ended_at is None:
                self.ended_at = time.monotonic()
                print("descarga encerrada (evento %s), gravando recuperacao"
                      % self.event_id, flush=True)
            self._append([_row(now_iso, self.event_id, "recovery", data)])
            if time.monotonic() - self.ended_at >= POST_SECONDS:
                print("evento %s completo em %s" % (self.event_id, self.path),
                      flush=True)
                self.event_id = None
                self.ended_at = None

        self.recent.append((now_iso, data))
        return INTERVAL_OB if (on_battery or self.event_id is not None) else INTERVAL


def write_atomic(path, text):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, mode=0o755, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o644)  # dummy-ups reads this as the nut user
    os.replace(tmp, path)


def main():
    mb = None
    pub = None
    fails = 0
    log = DischargeLog(EVENTS)
    nap = INTERVAL
    while True:
        started = time.monotonic()
        try:
            if mb is None:
                mb = Modbus(PORT, BAUD, SLAVE)
            data = read_all(mb)
            write_atomic(OUT, render(data))
            if pub is None and mqtt is not None and MQTT_HOST:
                pub = Publisher(data["serial_number"])
            if pub is not None:
                pub.drain(mb)
                pub.publish_state(mqtt_payload(data))
            on_battery = "OB" in ups_status(data).split()
            nap = log.observe(data, datetime.datetime.now().isoformat(timespec="seconds"),
                              on_battery)
            if fails:
                print("recovered after %d failed polls" % fails, flush=True)
                fails = 0
        except Exception as err:  # noqa: BLE001
            fails += 1
            print("poll failed (%d): %s" % (fails, err), file=sys.stderr, flush=True)
            if mb is not None:
                mb.close()
                mb = None
            # Do not leave a stale file claiming the mains is fine. After a
            # minute of silence, say the UPS is unreachable so upsmon's
            # deadtime notices instead of trusting old data.
            if fails >= 6:
                try:
                    write_atomic(OUT, "ups.status: OFF\nTIMER %d\n" % TIMER)
                except Exception:
                    pass
        time.sleep(max(0.5, nap - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
