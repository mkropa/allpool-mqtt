#!/usr/bin/env python3
"""
AllPool <-> MQTT Bridge für Home Assistant
==========================================

Liest eine PAUSCH ALLPOOL Schwimmbadsteuerung über das ASCII-Protokoll am
Extender-Port (RS232 / USB-Seriell, 19200 8N1) periodisch aus, veröffentlicht
die Werte per MQTT (inkl. Home-Assistant-Autodiscovery) und nimmt Befehle aus
Home Assistant entgegen.

Kernideen
---------
* Genau EIN Thread (SerialWorker) spricht mit der seriellen Schnittstelle.
  Alle Anfragen (periodische Abfragen, Schreibbefehle aus HA, Rohbefehle)
  landen in einer Warteschlange. Befehle aus HA springen vor wartende
  Abfragen; Befehle untereinander und Abfragen untereinander bleiben FIFO.
* Periodische Abfragen werden nicht doppelt eingereiht: steht ein Wert schon
  in der Warteschlange, wird er nicht noch einmal angehängt.
* Kann ein Schreibbefehl nicht sofort gesendet werden (andere Befehle warten
  noch, Verbindung getrennt), bekommt Home Assistant eine
  Meldung mit der Position in der Warteschlange. Später folgt "ausgeführt",
  "Fehler" oder "verworfen".
* Der Abstand zwischen Antwort und nächstem Befehl wird automatisch gelernt
  (so kurz wie möglich, so lang wie nötig). Gescheiterte Anfragen werden mit
  größerem Abstand wiederholt; der Lernwert wird gespeichert.
* Nach jedem Schreibbefehl wird der Wert sofort zurückgelesen (Rückprüfung).
  Erst der zurückgelesene Wert geht an Home Assistant; bei Abweichung kommt
  eine Warnung.
* Für jeden Wert gibt es in HA eine Konfigurations-Entität "Abfrageintervall".
  Neue Intervalle werden geprüft (Mindest-/Höchstwert und geschätzte
  Auslastung der seriellen Schnittstelle) und dauerhaft gespeichert.

Aufruf
------
  allpool_mqtt.py --config /etc/allpool-mqtt/config.yaml        # Dienstbetrieb
  allpool_mqtt.py --config config.yaml --list                    # Wertetabelle
  allpool_mqtt.py --config config.yaml --dump                    # alles 1x lesen
  allpool_mqtt.py --config config.yaml --raw "42020?"            # Rohbefehl
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import logging
import os
import re
import signal
import sys
import tempfile
import termios
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import serial  # pyserial
import yaml    # PyYAML

try:
    import paho.mqtt.client as mqtt
except ImportError:  # --dump / --raw funktionieren auch ohne paho
    mqtt = None

VERSION = "1.0.0"
log = logging.getLogger("allpool")


# =============================================================================
# ASCII-Protokoll
# =============================================================================

ERROR_CODES = {
    "c": "Prüfsumme stimmt nicht (Übertragungsfehler)",
    "u": "WerteID unbekannt (von dieser Firmware nicht unterstützt)",
    "l": "Wert zu klein (bei Messwerten: Fühler kurzgeschlossen)",
    "h": "Wert zu groß (bei Messwerten: Fühler unterbrochen)",
    "s": "Wert außerhalb der erlaubten Schrittweite",
    "i": "Wert kann nicht interpretiert werden",
    "r": "Wert darf nur gelesen, nicht geändert werden",
    "x": "Zugriff verweigert",
}


def checksum(payload: str) -> str:
    """XOR über alle Zeichen zwischen Startzeichen und '$', als 2-stelliges Hex."""
    cs = 0
    for b in payload.encode("ascii"):
        cs ^= b
    return f"{cs:02X}"


class AllPoolError(Exception):
    """Das ALLPOOL hat mit 'X<code>' geantwortet (fachlicher Fehler, kein Retry)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(ERROR_CODES.get(code, f"unbekannter Fehlercode '{code}'"))


class ProtocolError(Exception):
    """Timeout, kaputte Antwort, Prüfsummenfehler -> Wiederholung sinnvoll."""


class AllPoolSerial:
    """Dünne Schicht über pyserial. Wird ausschließlich vom Worker-Thread benutzt."""

    def __init__(self, cfg: dict):
        self.port = cfg.get("port", "/dev/ttyUSB0")
        self.baudrate = int(cfg.get("baudrate", 19200))
        self.use_checksum = bool(cfg.get("checksum", True))
        self.start_char = str(cfg.get("start_char", "#"))[:1] or "#"
        self.retries = int(cfg.get("retries", 3))

        # --- Selbstlernende Zeitsteuerung -----------------------------------
        # gap     = Pause zwischen letzter Antwort und nächstem Befehl
        #           (PAUSCH: "min. 10 ms"; die echte Grenze lernt das Programm)
        # timeout = wie lange auf eine Antwortzeile gewartet wird
        self.adaptive = bool(cfg.get("adaptive_timing", True))
        self.gap_min = float(cfg.get("min_gap", 0.01))
        self.gap_max = float(cfg.get("max_gap", 0.5))
        self.gap = self._clamp_gap(float(cfg.get("start_gap", 0.02)))
        self.good_gap = self.gap   # zuletzt bewährter Abstand (wird gespeichert)
        self.floor = self.gap_min  # unter diesen Wert nicht schrumpfen (zuletzt gescheitert)
        self.floor_successes = 0   # nach vielen Erfolgen wird die Untergrenze neu getestet
        self.timeout_max = float(cfg.get("timeout", 1.0))
        self.timeout_min = float(cfg.get("min_timeout", 0.25))
        self.cur_timeout = self.timeout_max
        self.latencies: collections.deque = collections.deque(maxlen=50)
        self.ok_streak = 0
        self.shrink_after = int(cfg.get("gap_shrink_after", 25))  # Erfolge in Folge
        self.stats = {"requests": 0, "retries": 0, "gap_increases": 0}

        self.ser: Optional[serial.Serial] = None
        self._last_rx = 0.0

    def _clamp_gap(self, g: float) -> float:
        return min(self.gap_max, max(self.gap_min, g))

    def set_learned_gap(self, g: float, floor: Optional[float] = None) -> None:
        """Beim Start gespeicherte Lernwerte übernehmen."""
        if self.adaptive:
            self.gap = self.good_gap = self._clamp_gap(g)
            if floor is not None:
                self.floor = min(self._clamp_gap(floor), self.gap)

    @property
    def latency(self) -> Optional[float]:
        return (sum(self.latencies) / len(self.latencies)) if self.latencies else None

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open

    def open(self) -> None:
        self.close()
        self.ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout_max,
            write_timeout=self.timeout_max,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        self.cur_timeout = self.timeout_max
        self.latencies.clear()
        if self.adaptive:  # nicht mit dem durch Ausfall aufgeblähten Abstand starten
            self.gap = self.good_gap
            self.ok_streak = 0
        # Eventuell halb übertragene Zeile im ALLPOOL abbrechen
        self.ser.write(b"\n")
        time.sleep(0.05)
        self.ser.reset_input_buffer()
        self._last_rx = time.monotonic()

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None

    # ---- Lernen ----------------------------------------------------------
    def _on_success(self, latency: float) -> None:
        self.latencies.append(latency)
        if not self.adaptive:
            return
        # Timeout: großzügig über der langsamsten zuletzt gesehenen Antwort
        if len(self.latencies) >= 5:
            self.cur_timeout = min(self.timeout_max,
                                   max(self.timeout_min, 3 * max(self.latencies) + 0.05))
        # Abstand: nach vielen Erfolgen in Folge vorsichtig verkürzen
        self.ok_streak += 1
        self.floor_successes += 1
        if self.floor > self.gap_min and self.floor_successes >= self.shrink_after * 40:
            self.floor = self.gap_min  # Bedingungen könnten sich gebessert haben
            self.floor_successes = 0
        if self.ok_streak >= self.shrink_after:
            self.good_gap = self.gap  # dieser Abstand hat sich bewährt
            self.ok_streak = 0
            new = self._clamp_gap(max(self.floor, self.gap * 0.85))
            if new >= self.gap:
                return
            old, self.gap = self.gap, new
            log.debug("Sendeabstand %.0f → %.0f ms", old * 1000, self.gap * 1000)

    def _on_failure(self, waited: float) -> None:
        self.ok_streak = 0
        self.stats["retries"] += 1
        if self.adaptive:
            old = self.gap
            self.floor = self._clamp_gap(max(self.floor, old * 1.15))
            self.floor_successes = 0
            self.gap = self._clamp_gap(max(old * 1.5, old + 0.01, self.floor))
            if self.gap > old:
                self.stats["gap_increases"] += 1
                log.info("Sendeabstand erhöht: %.0f → %.0f ms", old * 1000, self.gap * 1000)
            # Wiederholung mit vollem Timeout, bis wieder genug Messwerte da sind
            self.cur_timeout = self.timeout_max
        # Eine verspätete Antwort darf nicht der nächsten Anfrage zugeordnet
        # werden: bis zum maximalen Timeout warten und Eingang leeren.
        rest = self.timeout_max - waited
        if rest > 0:
            time.sleep(rest + 0.05)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._last_rx = time.monotonic()

    # ---- Senden/Empfangen -----------------------------------------------
    def _once(self, body: str) -> str:
        line = self.start_char + body
        if self.use_checksum:
            line += "$" + checksum(body)
        line += "\n"

        wait = self.gap - (time.monotonic() - self._last_rx)
        if wait > 0:
            time.sleep(wait)
        self.ser.reset_input_buffer()
        self.ser.timeout = self.cur_timeout
        self.ser.write(line.encode("ascii"))
        self.ser.flush()  # warten, bis die Zeile tatsächlich draußen ist
        t_sent = time.monotonic()
        try:
            raw = self.ser.readline()
        finally:
            self._last_rx = time.monotonic()
        waited = self._last_rx - t_sent
        log.debug("TX %r  RX %r  (%.0f ms, Abstand %.0f ms, Timeout %.0f ms)",
                  line, raw, waited * 1000, self.gap * 1000, self.cur_timeout * 1000)

        try:
            if not raw:
                raise ProtocolError(f"keine Antwort innerhalb {self.cur_timeout * 1000:.0f} ms")
            if not raw.endswith(b"\n"):
                raise ProtocolError(f"unvollständige Antwort {raw!r}")
            text = raw.decode("ascii", errors="replace").strip()
            if not text:
                raise ProtocolError("leere Antwort")
            kind, rest = text[0], text[1:]
            if "$" in rest:
                data, cs = rest.rsplit("$", 1)
                if checksum(data) != cs.strip().upper():
                    raise ProtocolError(f"Prüfsumme der Antwort falsch: {text!r}")
            else:
                data = rest
            if kind == "X" and data[:1] == "c":
                raise ProtocolError("ALLPOOL meldet Prüfsummenfehler")
            if kind not in (">", "X"):
                raise ProtocolError(f"unerwartete Antwort: {text!r}")
        except ProtocolError:
            self._on_failure(waited)
            raise
        self._on_success(waited)
        if kind == "X":
            raise AllPoolError(data[:1])
        return data

    def transact(self, body: str, retries: Optional[int] = None) -> str:
        """Sendet z.B. '2010?' oder '42020=26.5' und liefert die Nutzdaten der Antwort.
        Scheitert ein Versuch (Timeout, kaputte Antwort), wird er automatisch mit
        größerem Sendeabstand wiederholt."""
        if not self.connected:
            raise serial.SerialException("serielle Schnittstelle nicht geöffnet")
        self.stats["requests"] += 1
        retries = self.retries if retries is None else retries
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                return self._once(body)
            except ProtocolError as e:
                last = e
                lvl = logging.WARNING if attempt == retries else logging.INFO
                log.log(lvl, "'%s' Versuch %d/%d: %s – nächster Versuch mit %.0f ms Abstand",
                        body, attempt + 1, retries + 1, e, self.gap * 1000)
        raise last  # type: ignore[misc]


# =============================================================================
# Wertetabelle
# =============================================================================

DATE_RE = r"^\d{2}\.\d{2}\.\d{2}$"
TIME_RE = r"^\d{2}:\d{2}$"
ON_OFF = {"0": "OFF", "1": "ON"}
AUTO_AUS_HAND = {"1": "Auto", "2": "Aus", "3": "Hand"}


@dataclass
class Value:
    """Ein über das ASCII-Protokoll erreichbarer Wert.

    kind:
      sensor       numerischer Messwert (nur lesen)
      temperature  Temperatur-Messwert (nur lesen)
      binary       0/1-Zustand (nur lesen)
      enum_sensor  Zustand mit Bedeutungstabelle (nur lesen)
      text_sensor  beliebiger Text (nur lesen)
      select       Auswahl / Alternative (lesen + schreiben)
      number       Zahl mit Min/Max/Step (lesen + schreiben)
      switch       0/1 schaltbar (lesen + schreiben)
      text         Datum / Uhrzeit (lesen + schreiben)
    """

    key: str
    id: int
    name: str
    kind: str
    interval: int = 60
    options: Optional[Dict[str, str]] = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: Optional[str] = None
    device_class: Optional[str] = None
    icon: Optional[str] = None
    pattern: Optional[str] = None
    entity_category: Optional[str] = None
    enabled: bool = True
    decoder: Optional[str] = None
    hint: Optional[str] = None  # Zusatztext bei Schreibfehlern
    # Laufzeit
    unsupported: bool = False

    @property
    def writable(self) -> bool:
        return self.kind in ("select", "number", "switch", "text")

    @property
    def component(self) -> str:
        return {
            "sensor": "sensor", "temperature": "sensor", "enum_sensor": "sensor",
            "text_sensor": "sensor", "binary": "binary_sensor", "select": "select",
            "number": "number", "switch": "switch", "text": "text",
        }[self.kind]

    @property
    def decimals(self) -> int:
        if self.kind == "temperature":
            return 1
        if self.step is None or float(self.step).is_integer():
            return 0
        return len(f"{self.step}".split(".")[1])

    # ---- ALLPOOL -> MQTT -------------------------------------------------
    def decode(self, raw: str) -> str:
        raw = raw.strip()
        if self.decoder == "firmware":
            try:
                n = int(raw)
                return f"{n // 100}.{n % 100:02d}"
            except ValueError:
                return raw
        if self.kind in ("binary", "switch"):
            return ON_OFF.get(raw, raw)
        if self.kind in ("enum_sensor", "select"):
            return (self.options or {}).get(raw, raw)
        if self.kind in ("sensor", "temperature", "number"):
            try:
                return f"{float(raw):.{self.decimals}f}"
            except ValueError:
                return raw
        return raw

    # ---- MQTT -> ALLPOOL -------------------------------------------------
    def encode(self, payload: str) -> str:
        """Prüft einen Wert aus Home Assistant und liefert den Protokoll-String.
        Wirft ValueError mit verständlicher Meldung."""
        p = payload.strip()
        if self.kind == "switch":
            m = {"ON": "1", "OFF": "0", "1": "1", "0": "0", "EIN": "1", "AUS": "0"}
            if p.upper() not in m:
                raise ValueError(f"'{p}' ist kein gültiger Schaltzustand (ON/OFF)")
            return m[p.upper()]
        if self.kind == "select":
            opts = self.options or {}
            for raw, label in opts.items():
                if p.lower() == label.lower() or p == raw:
                    return raw
            raise ValueError(f"'{p}' ist keine gültige Auswahl ({', '.join(opts.values())})")
        if self.kind == "number":
            try:
                val = float(p.replace(",", "."))
            except ValueError:
                raise ValueError(f"'{p}' ist keine Zahl")
            lo, hi = self.min, self.max
            if lo is not None and val < lo - 1e-9:
                raise ValueError(f"{val} ist kleiner als das Minimum {lo}")
            if hi is not None and val > hi + 1e-9:
                raise ValueError(f"{val} ist größer als das Maximum {hi}")
            if self.step:
                base = lo or 0.0
                n = round((val - base) / self.step)
                if abs(base + n * self.step - val) > 1e-6:
                    raise ValueError(f"{val} passt nicht zur Schrittweite {self.step}")
            return f"{val:.{self.decimals}f}"
        if self.kind == "text":
            if self.pattern and not re.match(self.pattern, p):
                fmt = "TT.MM.JJ" if self.pattern == DATE_RE else "hh:mm"
                raise ValueError(f"'{p}' hat nicht das Format {fmt}")
            if self.pattern == DATE_RE:
                try:
                    dt.datetime.strptime(p, "%d.%m.%y")
                except ValueError:
                    raise ValueError(f"'{p}' ist kein gültiges Datum")
            if self.pattern == TIME_RE:
                h, m = map(int, p.split(":"))
                if h > 23 or m > 59:
                    raise ValueError(f"'{p}' ist keine gültige Uhrzeit")
            return p
        raise ValueError(f"{self.name} ist nur lesbar")


def default_values() -> List[Value]:
    V = Value
    uni_hint = ("Universalausgänge sind nur schaltbar, wenn am ALLPOOL "
                "'System / Anschlussklemmen / Universalausgang x / Funktion / Händisch' eingestellt ist")
    vals = [
        # --- Geräteinfos (nur beim Start) ---------------------------------------
        V("geraetetyp", 110, "Gerätetyp", "text_sensor", 0, entity_category="diagnostic", icon="mdi:information-outline"),
        V("firmware", 120, "Firmware", "text_sensor", 0, entity_category="diagnostic", decoder="firmware", icon="mdi:chip"),
        # --- Ausgänge / Relais ---------------------------------------------------
        V("filterrelais", 1010, "Filterpumpe", "binary", 30, device_class="running", icon="mdi:pump"),
        V("motorstrom", 1012, "Filterpumpe Motorstrom", "sensor", 60, unit="A", device_class="current", step=0.1),
        V("heizrelais", 1020, "Heizung Relais", "binary", 60, device_class="heat"),
        V("solarrelais", 1030, "Solar Relais", "binary", 30, device_class="running", icon="mdi:solar-power"),
        V("fuellrelais", 1040, "Füllventil", "binary", 60, device_class="opening", icon="mdi:water-plus"),
        V("dosierrelais", 1050, "Dosierpumpe", "binary", 60, device_class="running", icon="mdi:beaker"),
        V("rueckspuelung_zustand", 1100, "Rückspülung Zustand", "enum_sensor", 60,
          options={"0": "Filtern", "1": "Nachspülen", "2": "Entleeren", "3": "Rückspülen"}, icon="mdi:valve"),
        V("rueckspuelventil", 1101, "Rückspülventil", "enum_sensor", 300,
          options={"0": "Busy", "1": "Ready"}, icon="mdi:valve"),
        V("universalausgang_1", 1510, "Universalausgang 1", "switch", 60, hint=uni_hint),
        V("universalausgang_2", 1520, "Universalausgang 2", "switch", 60, hint=uni_hint),
        V("universalausgang_3", 1530, "Universalausgang 3", "switch", 60, hint=uni_hint),
        V("universalausgang_4", 1540, "Universalausgang 4", "switch", 60, hint=uni_hint),
        V("universalausgang_5", 1550, "Universalausgang 5", "switch", 60, hint=uni_hint),
        # --- Eingänge / Messwerte ------------------------------------------------
        V("badtemperatur", 2010, "Badtemperatur", "temperature", 60),
        V("kollektortemperatur", 2020, "Kollektortemperatur", "temperature", 30),
        V("niveauelektroden", 2050, "Niveauelektroden", "text_sensor", 120, icon="mdi:waves"),
        V("universaleingang_1", 2510, "Universaleingang 1", "binary", 60),
        V("temperatur_universaleingang_1", 2512, "Temperatur Universaleingang 1", "temperature", 60, enabled=False),
        V("universaleingang_2", 2520, "Universaleingang 2", "binary", 60),
        V("universaleingang_3", 2530, "Universaleingang 3", "binary", 60),
        V("temperatur_universaleingang_3", 2532, "Temperatur Universaleingang 3", "temperature", 60, enabled=False),
        # --- System --------------------------------------------------------------
        V("system", 30010, "System (Hauptschalter)", "select", 300, options={"1": "Auto", "2": "Aus"}, icon="mdi:power"),
        V("ferien_temp_heizung", 30030, "Ferien Temperatur Heizung", "number", 3600, min=5.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        V("ferien_temp_solar", 30031, "Ferien Temperatur Solar", "number", 3600, min=5.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        V("ferien_beginn", 30035, "Ferienbeginn (TT.MM.JJ)", "text", 3600, pattern=DATE_RE, icon="mdi:calendar-start"),
        V("ferien_ende", 30036, "Ferienende (TT.MM.JJ)", "text", 3600, pattern=DATE_RE, icon="mdi:calendar-end"),
        # --- Filter ---------------------------------------------------------------
        V("filter_modus", 33010, "Filter Modus", "select", 60, options=AUTO_AUS_HAND, icon="mdi:pump"),
        V("whirlpool_modus", 33011, "Whirlpool Modus", "select", 300, options=AUTO_AUS_HAND, icon="mdi:hot-tub"),
        # --- Rückspülung ----------------------------------------------------------
        V("rueckspuelung_modus", 39010, "Rückspülung Modus", "select", 300,
          options={"1": "Auto", "2": "Aus", "3": "Start"}, icon="mdi:valve"),
        # --- Konventionelle Heizung ----------------------------------------------
        V("heizung_modus", 42010, "Heizung Modus", "select", 300, options=AUTO_AUS_HAND, icon="mdi:radiator"),
        V("heizung_soll_bad", 42020, "Heizung Solltemperatur Bad", "number", 600, min=5.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        V("heizung_soll_whirlpool", 42025, "Heizung Solltemperatur Whirlpool", "number", 600, min=5.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        # --- Solar ----------------------------------------------------------------
        V("solar_modus", 45010, "Solar Modus", "select", 60, options=AUTO_AUS_HAND, icon="mdi:solar-power"),
        V("solar_max_bad", 45020, "Solar Maximaltemperatur Bad", "number", 600, min=15.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        V("solar_max_whirlpool", 45025, "Solar Maximaltemperatur Whirlpool", "number", 600, min=15.0, max=45.0, step=0.5, unit="°C", device_class="temperature"),
        # --- Niveau ---------------------------------------------------------------
        V("nachfuellen", 48010, "Automatisch Nachfüllen", "select", 300, options={"1": "Nein", "2": "Ja"}, icon="mdi:water-plus"),

        # === Erweitert: IDs aus der Menüstruktur (FW 4.2.13), NICHT in der =======
        # === Protokolltabelle. Format ungeprüft -> standardmäßig deaktiviert. ====
        V("bad_fuellen", 48020, "Bad füllen Dauer (hh:mm)", "text", 0, pattern=TIME_RE, enabled=False, icon="mdi:water-plus"),
        V("stat_filterzeit", 30060, "Statistik Filterzeit", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_motorschutz", 30061, "Statistik Motorschutz ausgelöst", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_heizungszeit", 30062, "Statistik Heizungszeit", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_solarzeit", 30063, "Statistik Solarzeit", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_solarenergie", 30064, "Statistik Solarenergie", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_rueckspuelungen", 30065, "Statistik Rückspülungen", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_fuellzeit", 30066, "Statistik Füllzeit", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_betriebszeit", 30067, "Statistik Gerätebetriebszeit", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_geraetetemp", 30068, "Statistik Gerätetemperatur", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_woche_bad", 30069, "Statistik Wochenschnitt Bad", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
        V("stat_woche_whirlpool", 30070, "Statistik Wochenschnitt Whirlpool", "text_sensor", 3600, enabled=False, entity_category="diagnostic"),
    ]
    return vals


def apply_value_overrides(values: List[Value], overrides: dict) -> List[Value]:
    """Überschreibt Felder aus config.yaml (Abschnitt 'values') und erlaubt eigene Werte."""
    by_key = {v.key: v for v in values}
    allowed = {"name", "kind", "interval", "options", "min", "max", "step", "unit",
               "device_class", "icon", "pattern", "entity_category", "enabled", "hint"}
    for key, ov in (overrides or {}).items():
        ov = dict(ov or {})
        if key not in by_key:
            if "id" not in ov or "kind" not in ov:
                log.error("Eigener Wert '%s' braucht mindestens 'id' und 'kind' – ignoriert", key)
                continue
            if not re.match(r"^[a-z0-9_]+$", key):
                log.error("Schlüssel '%s' darf nur a-z, 0-9 und _ enthalten – ignoriert", key)
                continue
            v = Value(key=key, id=int(ov.pop("id")), name=ov.pop("name", key), kind=ov.pop("kind"))
            values.append(v)
            by_key[key] = v
        v = by_key[key]
        for k, val in ov.items():
            if k not in allowed:
                log.warning("Unbekanntes Feld '%s' bei Wert '%s' ignoriert", k, key)
                continue
            if k == "options" and val is not None:
                val = {str(a): str(b) for a, b in val.items()}
            setattr(v, k, val)
    return values


# =============================================================================
# Warteschlange
# =============================================================================

_req_counter = 0
_req_lock = threading.Lock()


def _next_req_id() -> int:
    global _req_counter
    with _req_lock:
        _req_counter += 1
        return _req_counter


@dataclass
class Request:
    kind: str                     # read | write | verify | limit | raw
    body: str                     # z.B. "2010?" oder "42020=26.5"
    value: Optional[Value] = None
    payload: Optional[str] = None  # Originalwert aus HA (für Meldungen)
    created: float = field(default_factory=time.monotonic)
    rid: int = field(default_factory=_next_req_id)
    extra: dict = field(default_factory=dict)


class RequestQueue:
    """Thread-sichere FIFO mit Positionsabfrage und Duplikatprüfung für Lesezugriffe."""

    def __init__(self):
        self._dq: collections.deque = collections.deque()
        self._cv = threading.Condition()

    PRIORITY_KINDS = ("write", "raw", "verify")  # springen vor die Abfragen

    def put(self, req: Request) -> int:
        """Reiht ein und liefert die Anzahl der davor wartenden Einträge.

        Befehle (write/raw) kommen hinter den letzten wartenden Befehl, aber vor
        alle Abfragen. Untereinander bleiben Befehle und Abfragen jeweils FIFO."""
        with self._cv:
            if req.kind in self.PRIORITY_KINDS:
                idx = sum(1 for r in self._dq if r.kind in self.PRIORITY_KINDS)
                ahead = sum(1 for r in self._dq if r.kind in ("write", "raw"))
                self._dq.insert(idx, req)
            else:
                ahead = len(self._dq)
                self._dq.append(req)
            self._cv.notify()
            return ahead

    def put_front(self, req: Request) -> None:
        with self._cv:
            self._dq.appendleft(req)
            self._cv.notify()

    def get(self, timeout: float) -> Optional[Request]:
        with self._cv:
            if not self._dq:
                self._cv.wait(timeout)
            return self._dq.popleft() if self._dq else None

    def has_read(self, key: str) -> bool:
        with self._cv:
            return any(r.kind == "read" and r.value is not None and r.value.key == key for r in self._dq)

    def drop_reads(self) -> int:
        """Entfernt alle Lese-/Grenzwertabfragen (Befehle bleiben erhalten)."""
        with self._cv:
            keep = [r for r in self._dq if r.kind in ("write", "raw")]
            n = len(self._dq) - len(keep)
            self._dq = collections.deque(keep)
            return n

    def pop_expired_writes(self, max_age: float) -> List[Request]:
        now = time.monotonic()
        with self._cv:
            old = [r for r in self._dq if r.kind == "write" and now - r.created > max_age]
            for r in old:
                self._dq.remove(r)
            return old

    def __len__(self) -> int:
        with self._cv:
            return len(self._dq)


# =============================================================================
# Bridge
# =============================================================================

class Bridge:
    def __init__(self, cfg: dict, values: List[Value]):
        self.cfg = cfg
        self.values = [v for v in values]
        self.by_key = {v.key: v for v in self.values}

        m = cfg.get("mqtt", {})
        ha = cfg.get("homeassistant", {})
        p = cfg.get("polling", {})
        self.base = str(m.get("base_topic", "allpool")).rstrip("/")
        self.disc = str(ha.get("discovery_prefix", m.get("discovery_prefix", "homeassistant"))).rstrip("/")
        self.node_id = str(ha.get("node_id", "allpool"))
        self.device_name = str(ha.get("device_name", "AllPool"))

        self.min_interval = int(p.get("min_interval", 5))
        self.max_interval = int(p.get("max_interval", 86400))
        self.max_bus_load = float(p.get("max_bus_load", 0.5))
        self.command_timeout = float(p.get("command_timeout", 120))
        self.query_limits = bool(p.get("query_limits", True))
        self.allow_raw_write = bool(p.get("allow_raw_write", False))
        self.state_file = cfg.get("state_file", "/var/lib/allpool-mqtt/state.json")

        self.serial = AllPoolSerial(cfg.get("serial", {}))
        self.reconnect_interval = float(cfg.get("serial", {}).get("reconnect_interval", 10))
        self.max_failures = int(cfg.get("serial", {}).get("max_failures", 3))
        self.fail_count = 0
        self.q = RequestQueue()
        self.stop = threading.Event()
        self.busy = False
        self.avg_tx = 0.06            # Sekunden pro Anfrage (gleitender Mittelwert)
        self.firmware: Optional[str] = None
        self.states: Dict[str, str] = {}
        self.next_due: Dict[str, float] = {}
        self.intervals: Dict[str, int] = {v.key: int(v.interval) for v in self.values}
        self._load_state()
        self._serial_online: Optional[bool] = None
        self._last_queue_len = -1
        self._last_load_pub = ""
        self._saved_gap = self.serial.good_gap
        self._last_timing_pub = ""
        self._last_timing_ts = 0.0
        self.mq = None
        self.mqtt_connected = False

    # ---------------------------------------------------------------- Zustand
    def _load_state(self) -> None:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                data = json.load(f)
            for k, iv in data.get("intervals", {}).items():
                if k in self.intervals:
                    self.intervals[k] = int(iv)
            timing = data.get("timing", {})
            if timing.get("gap") is not None:
                fl = timing.get("floor")
                self.serial.set_learned_gap(float(timing["gap"]), float(fl) if fl is not None else None)
                log.info("Gelernter Sendeabstand: %.0f ms", self.serial.gap * 1000)
            log.info("Gespeicherte Intervalle geladen aus %s", self.state_file)
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("Zustandsdatei %s nicht lesbar: %s", self.state_file, e)

    def _save_state(self) -> None:
        try:
            d = os.path.dirname(self.state_file) or "."
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"intervals": self.intervals,
                           "timing": {"gap": round(self.serial.good_gap, 4),
                                      "floor": round(self.serial.floor, 4)}}, f, indent=2)
            self._saved_gap = self.serial.good_gap
            os.replace(tmp, self.state_file)
        except Exception as e:
            log.error("Zustandsdatei %s nicht schreibbar: %s", self.state_file, e)

    @property
    def active_values(self) -> List[Value]:
        return [v for v in self.values if v.enabled]

    def bus_load(self, override: Optional[Dict[str, int]] = None) -> float:
        """Geschätzter Anteil der Zeit, in der die Schnittstelle mit Polling belegt ist."""
        iv = dict(self.intervals)
        iv.update(override or {})
        t = max(self.avg_tx, 0.03)
        return sum(t / iv[v.key] for v in self.active_values
                   if not v.unsupported and iv.get(v.key, 0) > 0)

    # ---------------------------------------------------------------- Topics
    def t(self, *parts: str) -> str:
        return "/".join([self.base, *parts])

    @property
    def avail_topic(self) -> str:
        return self.t("bridge", "availability")

    @property
    def serial_topic(self) -> str:
        return self.t("bridge", "serial")

    # ---------------------------------------------------------------- MQTT
    def publish(self, topic: str, payload, retain: bool = False, qos: int = 0) -> None:
        if self.mq is None:
            return
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload, ensure_ascii=False)
        self.mq.publish(topic, payload, qos=qos, retain=retain)

    def notify(self, level: str, message: str, **extra) -> None:
        """Meldung an Home Assistant (Sensor 'Letzte Meldung' + Event-Topic) und Log."""
        getattr(log, {"error": "error", "warning": "warning"}.get(level, "info"))(message)
        ev = {"level": level, "message": message,
              "time": dt.datetime.now().astimezone().isoformat(timespec="seconds"), **extra}
        self.publish(self.t("bridge", "event"), ev)
        self.publish(self.t("bridge", "last_message"), message[:250], retain=True)
        self.publish(self.t("bridge", "last_message", "attributes"), ev, retain=True)

    def setup_mqtt(self) -> None:
        if mqtt is None:
            raise RuntimeError("paho-mqtt ist nicht installiert")
        m = self.cfg.get("mqtt", {})
        cid = str(m.get("client_id", f"allpool-mqtt-{self.node_id}"))
        if hasattr(mqtt, "CallbackAPIVersion"):  # paho-mqtt >= 2.0
            self.mq = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        else:
            self.mq = mqtt.Client(client_id=cid)
        if m.get("username"):
            self.mq.username_pw_set(str(m["username"]), str(m.get("password") or ""))
        if m.get("tls"):
            self.mq.tls_set(ca_certs=m.get("ca_certs") or None)
            if m.get("tls_insecure"):
                self.mq.tls_insecure_set(True)
        self.mq.will_set(self.avail_topic, "offline", qos=1, retain=True)
        self.mq.reconnect_delay_set(1, 60)
        self.mq.on_connect = self._on_connect
        self.mq.on_disconnect = self._on_disconnect
        self.mq.on_message = self._on_message
        host, port = str(m.get("host", "localhost")), int(m.get("port", 1883))
        log.info("Verbinde mit MQTT-Broker %s:%d", host, port)
        self.mq.connect_async(host, port, keepalive=int(m.get("keepalive", 60)))
        self.mq.loop_start()

    def _on_connect(self, client, userdata, flags, rc, *args) -> None:
        rc_val = getattr(rc, "value", rc)
        if rc_val != 0:
            log.error("MQTT-Verbindung abgelehnt: %s", rc)
            return
        self.mqtt_connected = True
        log.info("MQTT verbunden")
        subs = [
            self.t("+", "set"),
            self.t("+", "interval", "set"),
            self.t("bridge", "refresh"),
            self.t("raw", "request"),
            f"{self.disc}/status",
        ]
        for s in subs:
            client.subscribe(s, qos=1)
        self.publish(self.avail_topic, "online", retain=True, qos=1)
        self.publish_discovery()
        self.publish_all_states()

    def _on_disconnect(self, client, userdata, *args) -> None:
        self.mqtt_connected = False
        log.warning("MQTT-Verbindung getrennt – automatische Wiederverbindung läuft")

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", errors="replace").strip()
            topic = msg.topic
            log.debug("MQTT RX %s = %r", topic, payload)
            if topic == f"{self.disc}/status":
                if payload == "online":
                    # HA neu gestartet -> Discovery und Zustände erneut senden
                    threading.Timer(2.0, lambda: (self.publish_discovery(), self.publish_all_states())).start()
                return
            if topic == self.t("bridge", "refresh"):
                n = self.enqueue_all_reads()
                self.notify("info", f"Aktualisierung aller Werte angefordert ({n} Abfragen eingereiht)")
                return
            if topic == self.t("raw", "request"):
                self.handle_raw(payload)
                return
            rel = topic[len(self.base) + 1:].split("/")
            if len(rel) == 2 and rel[1] == "set":
                v = self.by_key.get(rel[0])
                if v and v.enabled:
                    self.handle_set(v, payload)
            elif len(rel) == 3 and rel[1] == "interval" and rel[2] == "set":
                v = self.by_key.get(rel[0])
                if v and v.enabled:
                    self.handle_interval(v, payload)
        except Exception:
            log.exception("Fehler bei Verarbeitung von %s", msg.topic)

    # ---------------------------------------------------------- Discovery
    def _device(self) -> dict:
        d = {"identifiers": [f"allpool_{self.node_id}"], "name": self.device_name,
             "manufacturer": "PAUSCH", "model": "ALLPOOL"}
        if self.firmware:
            d["sw_version"] = self.firmware
        return d

    def _origin(self) -> dict:
        return {"name": "allpool-mqtt", "sw_version": VERSION}

    def _cfg_topic(self, component: str, object_id: str) -> str:
        return f"{self.disc}/{component}/{self.node_id}/{object_id}/config"

    def _value_discovery(self, v: Value) -> dict:
        c = {
            "name": v.name,
            "unique_id": f"{self.node_id}_{v.key}",
            "state_topic": self.t(v.key, "state"),
            "json_attributes_topic": self.t(v.key, "attributes"),
            "availability": [{"topic": self.avail_topic}, {"topic": self.serial_topic}],
            "availability_mode": "all",
            "device": self._device(),
            "origin": self._origin(),
        }
        if v.icon:
            c["icon"] = v.icon
        if v.entity_category:
            c["entity_category"] = v.entity_category
        if v.writable:
            c["command_topic"] = self.t(v.key, "set")
        k = v.kind
        if k in ("sensor", "temperature"):
            c["state_class"] = "measurement"
            c["suggested_display_precision"] = v.decimals
            if k == "temperature":
                c["device_class"], c["unit_of_measurement"] = "temperature", "°C"
            else:
                if v.device_class:
                    c["device_class"] = v.device_class
                if v.unit:
                    c["unit_of_measurement"] = v.unit
        elif k == "enum_sensor":
            c["device_class"] = "enum"
            c["options"] = list((v.options or {}).values())
        elif k == "binary":
            c["payload_on"], c["payload_off"] = "ON", "OFF"
            if v.device_class:
                c["device_class"] = v.device_class
        elif k == "select":
            c["options"] = list((v.options or {}).values())
        elif k == "number":
            c.update({"min": v.min, "max": v.max, "step": v.step, "mode": "box"})
            if v.unit:
                c["unit_of_measurement"] = v.unit
            if v.device_class:
                c["device_class"] = v.device_class
        elif k == "switch":
            c.update({"payload_on": "ON", "payload_off": "OFF", "state_on": "ON", "state_off": "OFF"})
        elif k == "text":
            c["mode"] = "text"
            if v.pattern:
                c["pattern"] = v.pattern
            c["min"], c["max"] = 0, 16
        return c

    def _interval_discovery(self, v: Value) -> dict:
        return {
            "name": f"Abfrageintervall {v.name}",
            "unique_id": f"{self.node_id}_{v.key}_interval",
            "state_topic": self.t(v.key, "interval"),
            "command_topic": self.t(v.key, "interval", "set"),
            "min": 0, "max": self.max_interval, "step": 1, "mode": "box",
            "unit_of_measurement": "s",
            "icon": "mdi:timer-cog-outline",
            "entity_category": "config",
            "availability": [{"topic": self.avail_topic}],
            "device": self._device(),
            "origin": self._origin(),
        }

    def publish_discovery(self) -> None:
        for v in self.values:
            topic = self._cfg_topic(v.component, v.key)
            itopic = self._cfg_topic("number", f"{v.key}_interval")
            if v.enabled:
                self.publish(topic, self._value_discovery(v), retain=True)
                self.publish(itopic, self._interval_discovery(v), retain=True)
            else:  # deaktivierte Werte aus HA entfernen
                self.publish(topic, "", retain=True)
                self.publish(itopic, "", retain=True)
        dev, org = self._device(), self._origin()
        bav = [{"topic": self.avail_topic}]
        extra = [
            ("binary_sensor", "bridge_serial", {
                "name": "Serielle Verbindung", "state_topic": self.serial_topic,
                "payload_on": "online", "payload_off": "offline",
                "device_class": "connectivity", "entity_category": "diagnostic"}),
            ("sensor", "bridge_queue", {
                "name": "Warteschlange", "state_topic": self.t("bridge", "queue"),
                "unit_of_measurement": "Befehle", "state_class": "measurement",
                "icon": "mdi:tray-full", "entity_category": "diagnostic"}),
            ("sensor", "bridge_load", {
                "name": "Auslastung Schnittstelle", "state_topic": self.t("bridge", "load"),
                "unit_of_measurement": "%", "state_class": "measurement",
                "icon": "mdi:speedometer", "entity_category": "diagnostic"}),
            ("sensor", "bridge_gap", {
                "name": "Sendeabstand", "state_topic": self.t("bridge", "timing"),
                "value_template": "{{ value_json.gap_ms }}",
                "json_attributes_topic": self.t("bridge", "timing"),
                "unit_of_measurement": "ms", "state_class": "measurement",
                "icon": "mdi:timer-sand", "entity_category": "diagnostic"}),
            ("sensor", "bridge_latency", {
                "name": "Antwortzeit ALLPOOL", "state_topic": self.t("bridge", "timing"),
                "value_template": "{{ value_json.latency_ms }}",
                "unit_of_measurement": "ms", "state_class": "measurement",
                "icon": "mdi:timer-outline", "entity_category": "diagnostic"}),
            ("sensor", "bridge_retries", {
                "name": "Wiederholungen", "state_topic": self.t("bridge", "timing"),
                "value_template": "{{ value_json.retries }}",
                "state_class": "total_increasing",
                "icon": "mdi:replay", "entity_category": "diagnostic"}),
            ("sensor", "bridge_last_message", {
                "name": "Letzte Meldung", "state_topic": self.t("bridge", "last_message"),
                "json_attributes_topic": self.t("bridge", "last_message", "attributes"),
                "icon": "mdi:message-alert-outline"}),
            ("button", "bridge_refresh", {
                "name": "Alle Werte aktualisieren", "command_topic": self.t("bridge", "refresh"),
                "payload_press": "PRESS", "icon": "mdi:refresh"}),
        ]
        for comp, oid, c in extra:
            c.update({"unique_id": f"{self.node_id}_{oid}", "availability": bav,
                      "device": dev, "origin": org})
            self.publish(self._cfg_topic(comp, oid), c, retain=True)

    def publish_all_states(self) -> None:
        for v in self.active_values:
            self.publish(self.t(v.key, "interval"), str(self.intervals[v.key]), retain=True)
            if v.key in self.states:
                self.publish(self.t(v.key, "state"), self.states[v.key], retain=True)
        self.publish(self.serial_topic, "online" if self.serial.connected else "offline", retain=True)
        self._publish_load(force=True)
        self._publish_timing(force=True)
        self.publish(self.t("bridge", "queue"), str(len(self.q)), retain=True)

    def _publish_timing(self, force: bool = False) -> None:
        s = self.serial
        lat = s.latency
        d = {"gap_ms": round(s.gap * 1000), "learned_gap_ms": round(s.good_gap * 1000),
             "timeout_ms": round(s.cur_timeout * 1000),
             "latency_ms": round(lat * 1000) if lat is not None else None,
             "requests": s.stats["requests"], "retries": s.stats["retries"],
             "gap_increases": s.stats["gap_increases"], "adaptive": s.adaptive}
        js = json.dumps(d)
        if force or js != self._last_timing_pub:
            self._last_timing_pub = js
            self.publish(self.t("bridge", "timing"), js, retain=True)

    def _publish_load(self, force: bool = False) -> None:
        s = f"{self.bus_load() * 100:.1f}"
        if force or s != self._last_load_pub:
            self._last_load_pub = s
            self.publish(self.t("bridge", "load"), s, retain=True)

    def _set_serial_online(self, online: bool) -> None:
        if online != self._serial_online:
            self._serial_online = online
            self.publish(self.serial_topic, "online" if online else "offline", retain=True)

    def publish_value(self, v: Value, raw: Optional[str], error: Optional[str] = None) -> None:
        if raw is not None:
            state = v.decode(raw)
        else:
            # Messwert ungültig (z.B. Fühler defekt) -> "None" = unbekannt in HA
            state = "None" if v.kind in ("sensor", "temperature", "enum_sensor") else self.states.get(v.key, "None")
        self.states[v.key] = state
        self.publish(self.t(v.key, "state"), state, retain=True)
        self.publish(self.t(v.key, "attributes"), {
            "allpool_id": v.id, "raw": raw, "error": error,
            "last_update": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        }, retain=True)

    # ---------------------------------------------------------- Befehle aus HA
    def handle_set(self, v: Value, payload: str) -> None:
        if not v.writable:
            return
        if v.unsupported:
            self.notify("error", f"{v.name}: wird von dieser Firmware nicht unterstützt", key=v.key)
            return
        try:
            raw = v.encode(payload)
        except ValueError as e:
            self.notify("error", f"{v.name}: ungültiger Wert – {e}", key=v.key, value=payload)
            # HA-Anzeige auf tatsächlichen Wert zurücksetzen
            if v.key in self.states:
                self.publish(self.t(v.key, "state"), self.states[v.key], retain=True)
            return
        req = Request("write", f"{v.id}={raw}", value=v, payload=payload)
        ahead = self.q.put(req)  # Anzahl der Befehle davor (Abfragen werden übersprungen)
        if not self.serial.connected:
            self.notify("warning", f"{v.name} → {payload}: wartet, serielle Verbindung zum ALLPOOL getrennt "
                        f"(verworfen nach {int(self.command_timeout)} s)",
                        key=v.key, value=payload, status="queued", position=ahead + 1, request_id=req.rid)
        elif ahead > 0:
            self.notify("info", f"{v.name} → {payload}: wartet auf {ahead} vorherige(n) Befehl(e) "
                        f"(Position {ahead + 1})",
                        key=v.key, value=payload, status="queued", position=ahead + 1, request_id=req.rid)

    def handle_interval(self, v: Value, payload: str) -> None:
        old = self.intervals.get(v.key, v.interval)

        def reject(msg: str) -> None:
            self.notify("warning", f"Abfrageintervall {v.name}: {msg}", key=v.key, value=payload)
            self.publish(self.t(v.key, "interval"), str(old), retain=True)

        try:
            new = int(round(float(payload.replace(",", "."))))
        except ValueError:
            return reject(f"'{payload}' ist keine Zahl")
        if new != 0 and not (self.min_interval <= new <= self.max_interval):
            return reject(f"{new} s ist nicht erlaubt – 0 (nur beim Start/auf Anforderung) "
                          f"oder {self.min_interval}…{self.max_interval} s")
        load = self.bus_load({v.key: new})
        if new != 0 and load > self.max_bus_load:
            return reject(f"{new} s abgelehnt – die serielle Schnittstelle wäre zu {load:.0%} "
                          f"ausgelastet (erlaubt: {self.max_bus_load:.0%}). Andere Intervalle erhöhen "
                          f"oder Werte deaktivieren.")
        self.intervals[v.key] = new
        self._save_state()
        self.publish(self.t(v.key, "interval"), str(new), retain=True)
        now = time.monotonic()
        if new > 0:
            self.next_due[v.key] = min(self.next_due.get(v.key, now + new), now + new)
        self._publish_load()
        if new != old:
            self.notify("info", f"Abfrageintervall {v.name}: {old} s → {new} s "
                        f"(Auslastung {load:.1%})", key=v.key)

    def handle_raw(self, payload: str) -> None:
        """Rohbefehl: {"id": 42020, "value": "26.5"} oder {"id": 42020, "query": "h"} oder '42020?'"""
        try:
            if payload.startswith("{"):
                d = json.loads(payload)
                vid = int(d["id"])
                if "value" in d and d["value"] is not None:
                    body = f"{vid}={d['value']}"
                else:
                    body = f"{vid}?{str(d.get('query', ''))[:1]}"
            else:
                body = payload.lstrip("#%").split("$")[0].strip()
            if not re.match(r"^\d{1,5}(\?[lLhH]?|=[0-9A-Za-z.:\-]{1,16})$", body):
                raise ValueError("Format: <ID>? | <ID>?l | <ID>?h | <ID>=<Wert>")
            if "=" in body and not self.allow_raw_write:
                raise ValueError("Schreibende Rohbefehle sind deaktiviert (polling.allow_raw_write)")
        except Exception as e:
            self.publish(self.t("raw", "response"), {"request": payload, "error": str(e)})
            return
        self.q.put(Request("raw", body))

    # ---------------------------------------------------------- Polling
    def enqueue_read(self, v: Value) -> bool:
        if v.unsupported or self.q.has_read(v.key):
            return False
        self.q.put(Request("read", f"{v.id}?", value=v))
        return True

    def enqueue_verify(self, write_req: Request, expected: Optional[str]) -> None:
        """Rückprüfung direkt nach einem Schreibbefehl – läuft als nächste Anfrage."""
        v = write_req.value
        self.q.put_front(Request("verify", f"{v.id}?", value=v, payload=write_req.payload,
                                 extra={"expected": expected, "rid": write_req.rid}))

    def enqueue_all_reads(self) -> int:
        return sum(1 for v in self.active_values if self.enqueue_read(v))

    def scheduler_loop(self) -> None:
        now = time.monotonic()
        # Start: zuerst Geräteinfo, dann Grenzwerte, dann alle Werte einmal
        for key in ("geraetetyp", "firmware"):
            if key in self.by_key and self.by_key[key].enabled:
                self.enqueue_read(self.by_key[key])
        if self.query_limits:
            for v in self.active_values:
                if v.kind == "number":
                    self.q.put(Request("limit", f"{v.id}?l", value=v, extra={"which": "min"}))
                    self.q.put(Request("limit", f"{v.id}?h", value=v, extra={"which": "max"}))
        self.enqueue_all_reads()
        for v in self.active_values:
            iv = self.intervals[v.key]
            self.next_due[v.key] = now + iv if iv > 0 else float("inf")

        while not self.stop.wait(0.25):
            now = time.monotonic()
            for v in self.active_values:
                iv = self.intervals.get(v.key, 0)
                if iv <= 0 or v.unsupported:
                    continue
                if now >= self.next_due.get(v.key, 0):
                    self.next_due[v.key] = now + iv
                    if self.serial.connected:
                        self.enqueue_read(v)
            for r in self.q.pop_expired_writes(self.command_timeout):
                self.notify("error", f"{r.value.name} → {r.payload}: verworfen – konnte nicht innerhalb von "
                            f"{int(self.command_timeout)} s an das ALLPOOL gesendet werden",
                            key=r.value.key, value=r.payload, status="expired", request_id=r.rid)
            if now - self._last_timing_ts >= 10:
                self._last_timing_ts = now
                self._publish_timing()
                if abs(self.serial.good_gap - self._saved_gap) > 0.0005:
                    self._save_state()
            n = len(self.q)
            if n != self._last_queue_len:
                self._last_queue_len = n
                self.publish(self.t("bridge", "queue"), str(n), retain=True)

    # ---------------------------------------------------------- Serieller Worker
    def worker_loop(self) -> None:
        while not self.stop.is_set():
            if not self.serial.connected:
                try:
                    self.serial.open()
                    # Testabfrage: antwortet das ALLPOOL wirklich?
                    self.serial.transact("120?")
                    log.info("Serielle Schnittstelle %s geöffnet, ALLPOOL antwortet", self.serial.port)
                    self.fail_count = 0
                    was = self._serial_online
                    self._set_serial_online(True)
                    if was is False:  # nur bei Wiederverbindung melden
                        self.notify("info", f"Verbindung zum ALLPOOL wiederhergestellt ({self.serial.port})")
                        self.enqueue_all_reads()
                except Exception as e:
                    self.serial.close()
                    if self._serial_online is not False:
                        self.notify("error", f"ALLPOOL an {self.serial.port} nicht erreichbar: {e} "
                                    f"– neuer Versuch alle {int(self.reconnect_interval)} s")
                    self._set_serial_online(False)
                    self.q.drop_reads()
                    self.stop.wait(self.reconnect_interval)
                    continue
            req = self.q.get(timeout=0.5)
            if req is None:
                continue
            self.busy = True
            try:
                self.execute(req)
            except Exception as e:  # SerialException, OSError, termios.error, …
                if not isinstance(e, (serial.SerialException, OSError, termios.error)):
                    log.exception("Unerwarteter Fehler bei %s", req.body)
                if req.kind in ("write", "raw"):
                    self.q.put_front(req)  # Befehl nicht verlieren
                self._connection_lost(f"serieller Fehler: {e}")
            finally:
                self.busy = False

    def _connection_lost(self, reason: str) -> None:
        log.error("Verbindung zum ALLPOOL verloren (%s)", reason)
        self.serial.close()
        self._set_serial_online(False)
        n = self.q.drop_reads()
        self.notify("error", f"Verbindung zum ALLPOOL verloren ({reason}). {n} Abfragen verworfen, "
                    f"wartende Befehle bleiben {int(self.command_timeout)} s erhalten.")

    def execute(self, req: Request) -> None:
        v = req.value
        if req.kind == "write" and time.monotonic() - req.created > self.command_timeout:
            self.notify("error", f"{v.name} → {req.payload}: verworfen (zu lange in der Warteschlange)",
                        key=v.key, status="expired", request_id=req.rid)
            return
        t0 = time.monotonic()
        try:
            resp = self.serial.transact(req.body, retries=0 if self.fail_count else None)
            self.avg_tx = 0.9 * self.avg_tx + 0.1 * (time.monotonic() - t0)
            self.fail_count = 0
        except AllPoolError as e:
            self.fail_count = 0  # Gerät hat geantwortet
            self._handle_device_error(req, e)
            return
        except ProtocolError as e:
            self.fail_count += 1
            if self.fail_count >= self.max_failures:
                if req.kind in ("write", "raw"):
                    self.q.put_front(req)
                self.fail_count = 0
                self._connection_lost(f"{self.max_failures} Anfragen in Folge ohne gültige Antwort")
                return
            if req.kind == "write":
                # Unklar, ob angekommen -> vorne wieder einreihen. Bleibt das ALLPOOL
                # stumm, greift gleich die Verbindungsverlust-Erkennung und der Befehl
                # wartet auf die Wiederverbindung (max. command_timeout).
                self.q.put_front(req)
                self.notify("warning", f"{v.name} → {req.payload}: keine gültige Antwort vom ALLPOOL ({e}) "
                            "– wird wiederholt", key=v.key, status="retry", request_id=req.rid)
            elif req.kind == "raw":
                self.publish(self.t("raw", "response"), {"request": req.body, "error": str(e)})
            elif req.kind == "verify" and req.extra.get("expected") is not None:
                exp = req.extra["expected"]
                self.publish_value(v, exp)
                self.notify("warning", f"{v.name} → {v.decode(exp)}: ausgeführt, aber Rückprüfung "
                            f"fehlgeschlagen ({e}) – angezeigt wird die Schreibantwort",
                            key=v.key, status="unverified", request_id=req.extra.get("rid"))
            else:
                log.warning("Abfrage %s (%s) fehlgeschlagen: %s", req.body, v.name if v else "", e)
            return

        if req.kind == "read":
            self.publish_value(v, resp)
            if v.key == "firmware":
                fw = v.decode(resp)
                if fw != self.firmware:
                    self.firmware = fw
                    log.info("ALLPOOL Firmware %s", fw)
                    self.publish_discovery()
        elif req.kind == "write":
            # Wert sofort zurücklesen; erst das Ergebnis geht an Home Assistant
            self.enqueue_verify(req, expected=resp)
        elif req.kind == "verify":
            self.publish_value(v, resp)
            exp = req.extra.get("expected")
            if exp is None:
                pass  # nach Fehler: nur den echten Wert an HA melden
            elif v.decode(resp) == v.decode(exp):
                self.notify("info", f"{v.name} → {v.decode(resp)}: ausgeführt und bestätigt",
                            key=v.key, value=req.payload, status="done", request_id=req.extra.get("rid"))
            else:
                self.notify("warning", f"{v.name}: auf {v.decode(exp)} gesetzt, das ALLPOOL meldet beim "
                            f"Zurücklesen aber {v.decode(resp)}", key=v.key, value=req.payload,
                            status="mismatch", request_id=req.extra.get("rid"))
        elif req.kind == "limit":
            try:
                f = float(resp)
                if getattr(v, req.extra["which"]) != f:
                    log.info("%s: %s laut ALLPOOL = %s", v.name, req.extra["which"], f)
                    setattr(v, req.extra["which"], f)
                    if self.mqtt_connected:
                        self.publish(self._cfg_topic(v.component, v.key), self._value_discovery(v), retain=True)
            except ValueError:
                pass
        elif req.kind == "raw":
            self.publish(self.t("raw", "response"), {"request": req.body, "response": resp})
        self._publish_load()

    def _handle_device_error(self, req: Request, e: AllPoolError) -> None:
        v = req.value
        if req.kind == "raw":
            self.publish(self.t("raw", "response"), {"request": req.body, "error": str(e), "code": e.code})
            return
        if req.kind == "limit":
            return
        if e.code == "u":
            if not v.unsupported:
                v.unsupported = True
                self.notify("warning", f"{v.name} (ID {v.id}): {e} – Abfrage deaktiviert", key=v.key)
                self.publish_value(v, None, error=str(e))
            if req.kind == "write":
                self.notify("error", f"{v.name} → {req.payload}: {e}", key=v.key, status="failed",
                            request_id=req.rid)
            return
        if req.kind == "verify":
            exp = req.extra.get("expected")
            self.publish_value(v, exp, error=str(e))
            if exp is not None:
                self.notify("warning", f"{v.name} → {v.decode(exp)}: ausgeführt, aber Rückprüfung "
                            f"fehlgeschlagen ({e})", key=v.key, status="unverified",
                            request_id=req.extra.get("rid"))
            return
        if req.kind == "read":
            # z.B. 'l'/'h' bei Temperaturfühler: kurzgeschlossen / unterbrochen
            msg = str(e)
            if v.kind == "temperature" and e.code in ("l", "h"):
                msg = "Fühler kurzgeschlossen" if e.code == "l" else "Fühler unterbrochen / nicht angeschlossen"
            prev = self.states.get(v.key)
            self.publish_value(v, None, error=msg)
            if prev != "None":
                log.warning("%s: %s", v.name, msg)
            return
        # write
        hint = f" – {v.hint}" if v.hint and e.code in ("r", "x") else ""
        self.notify("error", f"{v.name} → {req.payload}: vom ALLPOOL abgelehnt: {e}{hint}",
                    key=v.key, value=req.payload, status="failed", code=e.code, request_id=req.rid)
        self.enqueue_verify(req, expected=None)  # HA-Anzeige auf echten Wert bringen

    # ---------------------------------------------------------- Lebenszyklus
    def run(self) -> None:
        self.setup_mqtt()
        threads = [threading.Thread(target=self.worker_loop, name="serial", daemon=True),
                   threading.Thread(target=self.scheduler_loop, name="scheduler", daemon=True)]
        for th in threads:
            th.start()
        log.info("AllPool-MQTT %s läuft (%d aktive Werte, geschätzte Auslastung %.1f%%)",
                 VERSION, len(self.active_values), self.bus_load() * 100)
        try:
            while not self.stop.wait(1.0):
                pass
        finally:
            log.info("Beende …")
            if self.mq is not None:
                self.publish(self.avail_topic, "offline", retain=True, qos=1)
                time.sleep(0.3)
                self.mq.loop_stop()
                self.mq.disconnect()
            for th in threads:
                th.join(timeout=3)
            self.serial.close()
            self._save_state()


# =============================================================================
# Hilfsfunktionen / CLI
# =============================================================================

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def setup_logging(level: str) -> None:
    fmt = "%(asctime)s %(levelname)-7s %(message)s"
    if os.environ.get("INVOCATION_ID"):  # unter systemd: Zeitstempel macht journald
        fmt = "%(levelname)-7s %(message)s"
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO), format=fmt, stream=sys.stdout)


def cmd_list(values: List[Value]) -> None:
    print(f"{'Schlüssel':32} {'ID':>5}  {'Typ':12} {'Int.':>6}  {'aktiv':5}  Name")
    for v in values:
        print(f"{v.key:32} {v.id:>5}  {v.kind:12} {v.interval:>6}  {'ja' if v.enabled else 'nein':5}  {v.name}")


def cmd_dump(cfg: dict, values: List[Value], all_values: bool) -> int:
    s = AllPoolSerial(cfg.get("serial", {}))
    s.open()
    rc = 0
    try:
        for v in values:
            if not (v.enabled or all_values):
                continue
            try:
                raw = s.transact(f"{v.id}?")
                print(f"{v.id:>5}  {v.name:40} {v.decode(raw):>12}   (roh: {raw})")
            except (AllPoolError, ProtocolError) as e:
                print(f"{v.id:>5}  {v.name:40} FEHLER: {e}")
                rc = 1
    finally:
        s.close()
    return rc


def cmd_raw(cfg: dict, body: str) -> int:
    s = AllPoolSerial(cfg.get("serial", {}))
    s.open()
    try:
        print(">" + s.transact(body.lstrip("#%")))
        return 0
    except (AllPoolError, ProtocolError) as e:
        print(f"FEHLER: {e}")
        return 1
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="AllPool <-> MQTT Bridge für Home Assistant")
    ap.add_argument("-c", "--config", default="/etc/allpool-mqtt/config.yaml", help="Pfad zur config.yaml")
    ap.add_argument("--list", action="store_true", help="Wertetabelle anzeigen und beenden")
    ap.add_argument("--dump", action="store_true", help="alle aktiven Werte einmal lesen (ohne MQTT)")
    ap.add_argument("--dump-all", action="store_true", help="wie --dump, auch deaktivierte Werte")
    ap.add_argument("--raw", metavar="BEFEHL", help="Rohbefehl senden, z.B. '2010?' oder '42020?h'")
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args()

    cfg = load_config(args.config) if os.path.exists(args.config) else {}
    if not cfg and not args.list:
        print(f"Konfigurationsdatei {args.config} fehlt", file=sys.stderr)
        return 2
    setup_logging(cfg.get("log_level", "INFO"))
    values = apply_value_overrides(default_values(), cfg.get("values"))

    if args.list:
        cmd_list(values)
        return 0
    if args.dump or args.dump_all:
        return cmd_dump(cfg, values, args.dump_all)
    if args.raw:
        return cmd_raw(cfg, args.raw)

    bridge = Bridge(cfg, values)

    def _sig(signum, frame):
        bridge.stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    bridge.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
