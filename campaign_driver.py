#!/usr/bin/env python3
"""Workshop-oriented PicoGlitcher mutation-style campaign driver PoC."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import dataclasses
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import platform
import random
import re
import socket
import statistics
import sys
import time
from typing import Any, Iterable, Protocol

try:
    import serial  # type: ignore
except Exception:  # pragma: no cover
    serial = None


PROJECT_NAME = "PicoGlitcher / findus"
FIRMWARE_MODULE = "findus/firmware/PicoGlitcher.py"


class GlitcherBackend(Protocol):
    def connect(self) -> None: ...
    def fingerprint(self) -> dict[str, Any]: ...
    def configure(self, config: "GlitchConfig") -> None: ...
    def arm(self, config: "GlitchConfig") -> None: ...
    def wait_for_result(self, timeout_s: float) -> dict[str, Any]: ...
    def reset_target(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class GlitchConfig:
    trigger_edge_count: int
    holdoff_delay: int
    pattern: int
    pattern_width: int
    glitch_clock_frequency: int
    target_vcc: float | None
    pulse_length_ns: int | None
    repeat_index: int
    campaign_stage: str


@dataclass
class BaselineProfile:
    raw_samples: list[bytes]
    normalized_samples: list[str]
    canonical_output: str
    normal_duration_ms_median: float
    time_to_first_byte_ms_median: float
    known_reset_markers: list[str]


@dataclass
class CaptureResult:
    raw: bytes
    text: str
    normalized: str
    time_to_first_byte_ms: float | None
    response_duration_ms: float
    line_count: int


@dataclass
class ClassificationResult:
    label: str
    confidence: float
    reason: str


class JsonlLogger:
    def __init__(self, path: str):
        self._fh = open(path, "a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


class TargetSerial:
    def __init__(self, port: str, baudrate: int, timeout: float = 0.02):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.ser = None

    def connect(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed; cannot open target UART")
        self.ser = serial.Serial(self.port, self.baudrate, timeout=self.timeout)
        self.ser.reset_input_buffer()

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None

    def capture_response(self, timeout_s: float = 0.8, idle_timeout_s: float = 0.15) -> CaptureResult:
        if self.ser is None:
            raise RuntimeError("Target serial is not connected")
        start = time.monotonic()
        first_byte_time = None
        last_byte_time = None
        buf = bytearray()
        while time.monotonic() - start < timeout_s:
            chunk = self.ser.read(256)
            now = time.monotonic()
            if chunk:
                if first_byte_time is None:
                    first_byte_time = now
                last_byte_time = now
                buf.extend(chunk)
            elif first_byte_time is not None and last_byte_time is not None and now - last_byte_time >= idle_timeout_s:
                break
        end = last_byte_time if last_byte_time is not None else time.monotonic()
        decoded = buf.decode("utf-8", errors="replace")
        return CaptureResult(
            raw=bytes(buf),
            text=decoded,
            normalized=normalize_uart_text(decoded),
            time_to_first_byte_ms=(None if first_byte_time is None else (first_byte_time - start) * 1000.0),
            response_duration_ms=max(0.0, (end - start) * 1000.0),
            line_count=0 if not decoded else len(decoded.splitlines()),
        )


class PicoGlitcherFindusBackend:
    """Backend A using Python-side PicoGlitcher/findus API."""

    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate
        self.pg = None

    def _import_picoglitcher_class(self):
        import importlib
        candidates = ["PicoGlitcher", "findus.firmware.PicoGlitcher", "findus.PicoGlitcher"]
        last_error = None
        for mod_name in candidates:
            try:
                mod = importlib.import_module(mod_name)
                if hasattr(mod, "PicoGlitcher"):
                    return getattr(mod, "PicoGlitcher")
            except Exception as exc:  # pragma: no cover
                last_error = exc
        raise RuntimeError(f"Unable to import PicoGlitcher class ({last_error})")

    def connect(self) -> None:
        cls = self._import_picoglitcher_class()
        # TODO: adapt constructor kwargs for your local findus/PicoGlitcher API variant
        # (some releases use port/baudrate kwargs, others auto-discover or use no kwargs).
        try:
            self.pg = cls(port=self.port, baudrate=self.baudrate)
        except TypeError:
            self.pg = cls()

    def fingerprint(self) -> dict[str, Any]:
        if self.pg is None:
            raise RuntimeError("Backend not connected")
        fw = None
        freq = None
        confidence = "probable"
        reason = "glitcher banner + RP2040 + PicoGlitcher-style pattern/edge/frequency behavior"
        try:
            if hasattr(self.pg, "get_firmware_version"):
                fw = self.pg.get_firmware_version()
        except Exception:
            fw = None
        try:
            if hasattr(self.pg, "get_frequency"):
                freq = int(self.pg.get_frequency())
        except Exception:
            freq = None
        if fw is not None:
            confidence = "confirmed"
            reason = "Firmware version query succeeded through PicoGlitcher API"
        return {
            "glitcher_project": PROJECT_NAME,
            "firmware_module": FIRMWARE_MODULE,
            "firmware_version": fw,
            "firmware_identity_confidence": confidence,
            "firmware_reason": reason,
            "cpu_frequency": freq,
            "control_port": self.port,
        }

    def configure(self, config: GlitchConfig) -> None:
        if self.pg is None:
            raise RuntimeError("Backend not connected")
        # TODO: map GlitchConfig -> exact findus calls for your firmware version
        # (e.g., set_trigger/set_number_of_edges/set_pattern_match signatures may differ).
        if hasattr(self.pg, "set_frequency"):
            self.pg.set_frequency(config.glitch_clock_frequency)
        if hasattr(self.pg, "set_number_of_edges"):
            self.pg.set_number_of_edges(config.trigger_edge_count)
        if hasattr(self.pg, "set_pattern_match"):
            self.pg.set_pattern_match(config.pattern, config.pattern_width)

    def arm(self, config: GlitchConfig) -> None:
        if self.pg is None:
            raise RuntimeError("Backend not connected")
        # TODO: choose and map the arm primitive for your workshop mode:
        # arm(), arm_double(), arm_multiplexing(), or arm_pulseshaping_from_config().
        if hasattr(self.pg, "arm"):
            self.pg.arm(config.holdoff_delay, config.pulse_length_ns or 20)

    def wait_for_result(self, timeout_s: float) -> dict[str, Any]:
        if self.pg is None:
            raise RuntimeError("Backend not connected")
        telemetry: dict[str, Any] = {"raw": "", "check_glitch": None}
        if hasattr(self.pg, "check_glitch"):
            try:
                telemetry["check_glitch"] = self.pg.check_glitch()
            except Exception as exc:
                telemetry["check_glitch_error"] = str(exc)
        if hasattr(self.pg, "block"):
            try:
                self.pg.block(timeout=timeout_s)
            except TypeError:
                self.pg.block()
            except Exception as exc:
                telemetry["block_error"] = str(exc)
        return telemetry

    def reset_target(self) -> None:
        if self.pg is None:
            raise RuntimeError("Backend not connected")
        if hasattr(self.pg, "reset_target"):
            self.pg.reset_target()
        elif hasattr(self.pg, "power_cycle_target"):
            self.pg.power_cycle_target()

    def close(self) -> None:
        if self.pg is not None and hasattr(self.pg, "close"):
            try:
                self.pg.close()
            except Exception:
                pass


class PicoGlitcherRawPyboardBackend:
    """Backend B for raw serial experimentation."""

    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate
        self.ser = None

    def connect(self) -> None:
        if serial is None:
            raise RuntimeError("pyserial is not installed; cannot open glitcher port")
        self.ser = serial.Serial(self.port, self.baudrate, timeout=0.1)
        self.ser.reset_input_buffer()

    def _read_banner(self, timeout_s: float = 1.2) -> str:
        if self.ser is None:
            return ""
        start = time.monotonic()
        buf = bytearray()
        while time.monotonic() - start < timeout_s:
            chunk = self.ser.read(256)
            if chunk:
                buf.extend(chunk)
        return buf.decode("utf-8", errors="replace")

    def fingerprint(self) -> dict[str, Any]:
        banner = self._read_banner()
        probable = "glitcher" in banner.lower() or "pico" in banner.lower()
        return {
            "glitcher_project": PROJECT_NAME,
            "firmware_module": FIRMWARE_MODULE,
            "firmware_version": None,
            "firmware_identity_confidence": "probable" if probable else "unknown",
            "firmware_reason": (
                "glitcher banner + RP2040 + PicoGlitcher-style pattern/edge/frequency behavior"
                if probable
                else "No clear banner captured; protocol adaptation required"
            ),
            "cpu_frequency": None,
            "control_port": self.port,
            "raw_banner": banner.strip(),
        }

    def configure(self, config: GlitchConfig) -> None:
        # TODO: replace with actual pyboard-REPL snippets:
        # import PicoGlitcher, instantiate object, call set_frequency/set_pattern_match/etc.
        _ = config

    def arm(self, config: GlitchConfig) -> None:
        # TODO: replace with validated pyboard REPL arm command for your firmware.
        _ = config

    def wait_for_result(self, timeout_s: float) -> dict[str, Any]:
        return {"raw": self._read_banner(timeout_s=timeout_s), "check_glitch": None}

    def reset_target(self) -> None:
        # TODO: implement reset_target()/power_cycle_target() command for pyboard REPL mode.
        pass

    def close(self) -> None:
        if self.ser is not None:
            self.ser.close()
            self.ser = None


class MockBackend:
    def __init__(self, port: str, baudrate: int):
        self.port = port
        self.baudrate = baudrate

    def connect(self) -> None:
        pass

    def fingerprint(self) -> dict[str, Any]:
        return {
            "glitcher_project": PROJECT_NAME,
            "firmware_module": FIRMWARE_MODULE,
            "firmware_version": "mock-0.1",
            "firmware_identity_confidence": "confirmed",
            "firmware_reason": "mock backend",
            "cpu_frequency": 250000000,
            "control_port": self.port,
        }

    def configure(self, config: GlitchConfig) -> None:
        _ = config

    def arm(self, config: GlitchConfig) -> None:
        _ = config

    def wait_for_result(self, timeout_s: float) -> dict[str, Any]:
        _ = timeout_s
        return {"raw": "mock telemetry", "check_glitch": False}

    def reset_target(self) -> None:
        pass

    def close(self) -> None:
        pass


def normalize_uart_text(text: str, volatile_regexes: Iterable[str] | None = None, lowercase: bool = False) -> str:
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b", "<time>", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if volatile_regexes:
        for expr in volatile_regexes:
            cleaned = re.sub(expr, "<volatile>", cleaned)
    if lowercase:
        cleaned = cleaned.lower()
    return cleaned


def bitstring(pattern: int, width: int) -> str:
    return format(pattern & ((1 << width) - 1), f"0{width}b")


def describe_pattern(pattern: int, width: int) -> dict[str, float | int]:
    bits = bitstring(pattern, width)
    hw = bits.count("1")
    transitions = sum(1 for i in range(1, len(bits)) if bits[i] != bits[i - 1])
    leading_zero = len(bits) - len(bits.lstrip("0"))
    trailing_zero = len(bits) - len(bits.rstrip("0"))
    idx = 0
    run_count = 0
    max_run = 0
    while idx < len(bits):
        if bits[idx] == "1":
            run_count += 1
            j = idx
            while j < len(bits) and bits[j] == "1":
                j += 1
            max_run = max(max_run, j - idx)
            idx = j
        else:
            idx += 1
    positions = [i for i, b in enumerate(bits) if b == "1"]
    center_of_mass = float(sum(positions) / len(positions)) if positions else 0.0
    return {
        "hamming_weight": hw,
        "max_run_length": max_run,
        "run_count": run_count,
        "transition_count": transitions,
        "center_of_mass": center_of_mass,
        "density": hw / width if width else 0.0,
        "leading_zero_count": leading_zero,
        "trailing_zero_count": trailing_zero,
    }


def normalized_edit_distance(a: str, b: str) -> float:
    if a == b:
        return 0.0
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    m, n = len(a), len(b)
    prev = list(range(n + 1))
    cur = [0] * (n + 1)
    for i in range(1, m + 1):
        cur[0] = i
        ai = a[i - 1]
        for j in range(1, n + 1):
            cost = 0 if ai == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[n] / max(m, n)


def compute_energy_score(config: GlitchConfig, desc: dict[str, float | int]) -> float:
    density = float(desc["density"])
    max_run = float(desc["max_run_length"]) / max(1, config.pattern_width)
    freq_norm = min(1.0, config.glitch_clock_frequency / 400_000_000.0)
    pulse_norm = min(1.0, (config.pulse_length_ns or 20) / 200.0)
    pulses_norm = min(1.0, float(desc["hamming_weight"]) / max(1, config.pattern_width))
    score = 0.30 * density + 0.20 * max_run + 0.25 * freq_norm + 0.15 * pulse_norm + 0.10 * pulses_norm
    if config.target_vcc is not None:
        score += max(0.0, min(0.15, (3.3 - config.target_vcc) / 10.0))
    return min(1.5, score)


def compute_distance_metrics(current: CaptureResult, baseline: BaselineProfile) -> dict[str, float | int]:
    baseline_ref = baseline.canonical_output
    dist_baseline = normalized_edit_distance(current.normalized, baseline_ref)
    line_count_delta = current.line_count - (0 if not baseline_ref else baseline_ref.count("\n") + 1)
    ttfb_delta = 0.0 if current.time_to_first_byte_ms is None else abs(current.time_to_first_byte_ms - baseline.time_to_first_byte_ms_median)
    dur_delta = abs(current.response_duration_ms - baseline.normal_duration_ms_median)
    return {
        "distance_to_baseline": dist_baseline,
        "line_count_delta": line_count_delta,
        "time_to_first_byte_delta_ms": ttfb_delta,
        "response_duration_delta_ms": dur_delta,
    }


def classify_attempt(
    current: CaptureResult,
    baseline: BaselineProfile,
    telemetry: dict[str, Any],
    distance: dict[str, float | int],
) -> ClassificationResult:
    txt = current.text.lower()
    reset_hit = any(marker.lower() in txt for marker in baseline.known_reset_markers)
    silence = len(current.raw) == 0
    reboot_ttfb = current.time_to_first_byte_ms is not None and baseline.time_to_first_byte_ms_median > 0 and current.time_to_first_byte_ms > baseline.time_to_first_byte_ms_median * 2.5
    reboot_duration = baseline.normal_duration_ms_median > 0 and current.response_duration_ms > baseline.normal_duration_ms_median * 1.8
    if reset_hit or silence or reboot_ttfb:
        return ClassificationResult("RESET_ARTIFACT", 0.8 if reset_hit else 0.65, "Reset/silence/reboot timing indicators observed.")
    dbase = float(distance["distance_to_baseline"])
    td = float(distance["time_to_first_byte_delta_ms"])
    dd = float(distance["response_duration_delta_ms"])
    telemetry_changed = bool(telemetry.get("check_glitch")) or bool(telemetry.get("raw"))
    if dbase <= 0.03 and td <= 15.0 and dd <= 40.0:
        if telemetry_changed:
            return ClassificationResult("EQUIVALENT", 0.78, "Telemetry changed but externally visible behavior is baseline-like.")
        return ClassificationResult("SURVIVED", 0.90, "Output and timing are close to baseline with no reset markers.")
    if dbase >= 0.08 and not reboot_duration:
        return ClassificationResult("KILLED", 0.72, "Non-reset output deviation from baseline appears meaningful.")
    return ClassificationResult("INCONCLUSIVE", 0.50, "Insufficient evidence for confident classification.")


class NoveltyArchive:
    def __init__(self):
        self.non_reset_seen: list[str] = []
        self.reset_seen: list[str] = []

    def score(self, normalized_output: str, classification: str) -> tuple[float, float]:
        seen = self.reset_seen if classification == "RESET_ARTIFACT" else self.non_reset_seen
        nearest = 1.0 if not seen else min(normalized_edit_distance(normalized_output, prev) for prev in seen)
        if classification == "RESET_ARTIFACT":
            novelty = nearest * 0.2
            self.reset_seen.append(normalized_output)
        else:
            novelty = nearest
            self.non_reset_seen.append(normalized_output)
        return nearest, novelty


def seed_patterns(width: int) -> list[tuple[int, str]]:
    patterns: list[tuple[int, str]] = []
    for i in range(width):
        patterns.append((1 << i, "seed_single_shift"))
    for i in range(max(1, width - 2)):
        patterns.append(((1 << i) | (1 << min(width - 1, i + 2)), "seed_two_pulse_gap"))
    for i in range(max(1, width - 1)):
        p = (0b11 << i) & ((1 << width) - 1)
        if p:
            patterns.append((p, "seed_short_burst"))
    alt = sum(1 << i for i in range(0, width, 2))
    patterns.append((alt, "seed_alternating_sparse"))
    front = ((1 << min(2, width)) - 1) << max(0, width - min(2, width))
    back = (1 << min(2, width)) - 1
    patterns.extend([(front, "seed_front_loaded"), (back, "seed_back_loaded")])
    out: list[tuple[int, str]] = []
    seen = set()
    for pat, stage in patterns:
        pat &= (1 << width) - 1
        if pat and pat not in seen:
            seen.add(pat)
            out.append((pat, stage))
    return out


class AdaptivePolicy:
    """Simple explainable scheduler: observe -> classify -> score -> adapt."""

    def __init__(self, width: int, base_frequency: int):
        self.pending: list[GlitchConfig] = []
        self.stats: dict[GlitchConfig, list[float]] = defaultdict(list)
        holdoffs = [100, 250, 500, 900]
        pulse_lengths = [20, 30]
        for pattern, stage in seed_patterns(width):
            for holdoff in holdoffs:
                for length in pulse_lengths:
                    self.pending.append(
                        GlitchConfig(
                            trigger_edge_count=1,
                            holdoff_delay=holdoff,
                            pattern=pattern,
                            pattern_width=width,
                            glitch_clock_frequency=base_frequency,
                            target_vcc=None,
                            pulse_length_ns=length,
                            repeat_index=0,
                            campaign_stage=stage,
                        )
                    )

    def next_candidate(self, reset_rate_recent: float) -> GlitchConfig | None:
        if self.pending:
            return self.pending.pop(0)
        if not self.stats:
            return None
        ranked = sorted(self.stats.items(), key=lambda kv: statistics.mean(kv[1]), reverse=True)
        base = random.choice(ranked[: min(3, len(ranked))])[0]
        low_energy = reset_rate_recent > 0.35
        holdoff = max(1, base.holdoff_delay + random.choice([-50, -20, 20, 50]))
        pattern = base.pattern
        pattern = (pattern >> 1) or 1 if random.random() < 0.5 else (((pattern << 1) & ((1 << base.pattern_width) - 1)) or 1)
        if random.random() < 0.5:
            bit = 1 << random.randrange(0, base.pattern_width)
            pattern ^= bit
            if pattern == 0:
                pattern = bit
        freq_step = 5_000_000
        freq = max(50_000_000, base.glitch_clock_frequency - freq_step) if low_energy else min(350_000_000, base.glitch_clock_frequency + random.choice([-freq_step, 0, freq_step]))
        pulse = max(8, min(120, (base.pulse_length_ns or 20) + random.choice([-5, 0, 5])))
        return GlitchConfig(
            trigger_edge_count=base.trigger_edge_count,
            holdoff_delay=holdoff,
            pattern=pattern,
            pattern_width=base.pattern_width,
            glitch_clock_frequency=freq,
            target_vcc=base.target_vcc,
            pulse_length_ns=pulse,
            repeat_index=base.repeat_index + 1,
            campaign_stage="adapt_low_energy" if low_energy else "adapt_local_perturb",
        )

    def update(self, config: GlitchConfig, score: float, label: str, repeats_per_candidate: int) -> None:
        self.stats[config].append(score)
        if label == "INCONCLUSIVE":
            for i in range(min(2, repeats_per_candidate)):
                self.pending.append(dataclasses.replace(config, repeat_index=config.repeat_index + i + 1, campaign_stage="retry_uncertain"))


def build_backend(name: str, glitcher_port: str, baud_glitcher: int) -> GlitcherBackend:
    if name == "findus":
        return PicoGlitcherFindusBackend(glitcher_port, baud_glitcher)
    if name == "raw-pyboard":
        return PicoGlitcherRawPyboardBackend(glitcher_port, baud_glitcher)
    if name == "mock":
        return MockBackend(glitcher_port, baud_glitcher)
    raise ValueError(f"Unsupported backend: {name}")


def collect_baseline(target: TargetSerial, baseline_runs: int, reset_markers: list[str]) -> BaselineProfile:
    raws: list[bytes] = []
    normalized: list[str] = []
    durations: list[float] = []
    ttfb: list[float] = []
    for _ in range(baseline_runs):
        cap = target.capture_response()
        raws.append(cap.raw)
        normalized.append(cap.normalized)
        durations.append(cap.response_duration_ms)
        if cap.time_to_first_byte_ms is not None:
            ttfb.append(cap.time_to_first_byte_ms)
    canonical = Counter(normalized).most_common(1)[0][0] if normalized else ""
    return BaselineProfile(
        raw_samples=raws,
        normalized_samples=normalized,
        canonical_output=canonical,
        normal_duration_ms_median=statistics.median(durations) if durations else 0.0,
        time_to_first_byte_ms_median=statistics.median(ttfb) if ttfb else 0.0,
        known_reset_markers=reset_markers,
    )


def candidate_score(classification: ClassificationResult, novelty_score: float, energy_score: float, reproducibility_hint: float) -> float:
    base = 0.0
    if classification.label == "KILLED":
        base += 1.0
    elif classification.label == "SURVIVED":
        base += 0.10
    elif classification.label == "EQUIVALENT":
        base -= 0.20
    elif classification.label == "RESET_ARTIFACT":
        base -= 1.0
    else:
        base += 0.25
    # Workshop note: this heuristic is intentionally simple so search decisions remain explainable.
    return base + (0.7 * novelty_score) + (0.5 * reproducibility_hint) - (0.35 * energy_score)


def has_interesting_region(recent_records: deque[dict[str, Any]], required_killed: int, max_reset_rate: float) -> bool:
    if not recent_records:
        return False
    killed = [r for r in recent_records if r["classification"]["label"] == "KILLED"]
    resets = [r for r in recent_records if r["classification"]["label"] == "RESET_ARTIFACT"]
    return len(killed) >= required_killed and (len(resets) / len(recent_records)) <= max_reset_rate


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Mutation-testing-style PicoGlitcher campaign driver PoC")
    p.add_argument("--glitcher-port", default="COM7")
    p.add_argument("--target-port", default="COM8")
    p.add_argument("--baud-target", type=int, default=115200)
    p.add_argument("--baud-glitcher", type=int, default=115200)
    p.add_argument("--output", default="campaign.jsonl")
    p.add_argument("--max-attempts", type=int, default=1000)
    p.add_argument("--baseline-runs", type=int, default=5)
    p.add_argument("--repeats-per-candidate", type=int, default=1)
    p.add_argument("--reset-rate-stop-threshold", type=float, default=0.4)
    p.add_argument("--recent-window-size", type=int, default=50)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backend", choices=["findus", "raw-pyboard", "mock"], default="findus")
    p.add_argument("--pattern-width", type=int, default=8)
    p.add_argument("--frequency", type=int, default=250000000)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--baseline-unstable-threshold", type=float, default=0.2)
    p.add_argument("--max-resets", type=int, default=300)
    p.add_argument("--repro-check-runs", type=int, default=4)
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    random.seed(args.seed)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    campaign_id = datetime.now(timezone.utc).strftime("campaign-%Y%m%dT%H%M%SZ")
    jsonl = JsonlLogger(args.output)
    backend_name = "mock" if args.dry_run else args.backend
    backend = build_backend(backend_name, args.glitcher_port, args.baud_glitcher)
    target = TargetSerial(args.target_port, args.baud_target)
    novelty = NoveltyArchive()
    policy = AdaptivePolicy(args.pattern_width, args.frequency)
    recent: deque[dict[str, Any]] = deque(maxlen=args.recent_window_size)
    total_resets = 0
    try:
        backend.connect()
        firmware = backend.fingerprint()
        firmware["target_uart_port"] = args.target_port
        if args.dry_run:
            baseline = BaselineProfile(
                raw_samples=[b"normal output"],
                normalized_samples=["normal output"],
                canonical_output="normal output",
                normal_duration_ms_median=80.0,
                time_to_first_byte_ms_median=10.0,
                known_reset_markers=["boot", "reset", "brownout", "watchdog"],
            )
            baseline_summary = "dry-run"
        else:
            target.connect()
            baseline = collect_baseline(target, args.baseline_runs, ["boot", "reset", "brownout", "watchdog", "rst"])
            drift = max((normalized_edit_distance(x, baseline.canonical_output) for x in baseline.normalized_samples), default=0.0)
            if drift > args.baseline_unstable_threshold:
                logging.error("Baseline unstable before glitching (drift=%.3f). Stopping for safety.", drift)
                return 2
            baseline_summary = (
                f"samples={len(baseline.raw_samples)} canonical_len={len(baseline.canonical_output)} "
                f"ttfb_ms_median={baseline.time_to_first_byte_ms_median:.2f} duration_ms_median={baseline.normal_duration_ms_median:.2f}"
            )
        jsonl.write({
            "record_type": "campaign_start",
            "campaign_id": campaign_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "python_version": sys.version,
            "args": vars(args),
            "firmware_fingerprint": firmware,
            "baseline_summary": baseline_summary,
        })
        attempt_id = 0
        while attempt_id < args.max_attempts:
            recent_reset_rate = (sum(1 for r in recent if r["classification"]["label"] == "RESET_ARTIFACT") / len(recent)) if recent else 0.0
            if recent and recent_reset_rate > args.reset_rate_stop_threshold:
                logging.warning("Stopping due to high recent reset rate %.2f > %.2f", recent_reset_rate, args.reset_rate_stop_threshold)
                break
            if total_resets >= args.max_resets:
                logging.warning("Stopping due to max resets reached (%d)", total_resets)
                break
            cfg = policy.next_candidate(recent_reset_rate)
            if cfg is None:
                break
            attempt_id += 1
            desc = describe_pattern(cfg.pattern, cfg.pattern_width)
            energy = compute_energy_score(cfg, desc)
            if args.dry_run:
                capture = CaptureResult(
                    raw=b"normal output" if cfg.pattern % 7 else b"BOOT\nreset\n",
                    text="normal output" if cfg.pattern % 7 else "BOOT\nreset\n",
                    normalized="normal output" if cfg.pattern % 7 else "boot reset",
                    time_to_first_byte_ms=12.0 if cfg.pattern % 7 else 45.0,
                    response_duration_ms=80.0 if cfg.pattern % 7 else 190.0,
                    line_count=1 if cfg.pattern % 7 else 2,
                )
                telemetry = {"raw": "dry-run telemetry", "check_glitch": bool(cfg.pattern & 1)}
            else:
                backend.configure(cfg)
                backend.arm(cfg)
                telemetry = backend.wait_for_result(timeout_s=0.8)
                capture = target.capture_response(timeout_s=0.8)
            distance = compute_distance_metrics(capture, baseline)
            label = classify_attempt(capture, baseline, telemetry, distance)
            nearest_seen, novelty_score = novelty.score(capture.normalized, label.label)
            distance["distance_to_nearest_seen"] = nearest_seen
            reproducibility_hint = min(1.0, (sum(1 for r in recent if r["classification"]["label"] == label.label) / max(1, len(recent)))) if recent else 0.0
            cscore = candidate_score(label, novelty_score, energy, reproducibility_hint)
            policy.update(cfg, cscore, label.label, args.repeats_per_candidate)
            if label.label == "RESET_ARTIFACT":
                total_resets += 1
            record = {
                "campaign_id": campaign_id,
                "attempt_id": attempt_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "firmware": firmware,
                "config": {
                    "trigger_edge_count": cfg.trigger_edge_count,
                    "holdoff_delay": cfg.holdoff_delay,
                    "pattern": f"0b{bitstring(cfg.pattern, cfg.pattern_width)}",
                    "pattern_width": cfg.pattern_width,
                    "glitch_clock_frequency": cfg.glitch_clock_frequency,
                    "target_vcc": cfg.target_vcc,
                    "pulse_length_ns": cfg.pulse_length_ns,
                    "repeat_index": cfg.repeat_index,
                    "campaign_stage": cfg.campaign_stage,
                },
                "pattern_descriptors": desc,
                "glitcher_telemetry_raw": telemetry.get("raw", ""),
                "target_uart_raw_hex": capture.raw.hex(),
                "target_uart_text": capture.text,
                "normalized_output": capture.normalized,
                "timing": {"time_to_first_byte_ms": capture.time_to_first_byte_ms, "response_duration_ms": capture.response_duration_ms},
                "classification": asdict(label),
                "distance": distance,
                "scores": {"novelty_score": novelty_score, "energy_score": energy, "candidate_score": cscore},
                "notes": "",
            }
            jsonl.write(record)
            recent.append(record)
            if attempt_id % 25 == 0:
                logging.info("attempt=%d reset_rate_recent=%.2f novelty=%.2f label=%s", attempt_id, recent_reset_rate, novelty_score, label.label)
            if has_interesting_region(recent, required_killed=3, max_reset_rate=0.25):
                for rep in range(args.repro_check_runs):
                    if attempt_id >= args.max_attempts:
                        break
                    attempt_id += 1
                    rep_cfg = dataclasses.replace(cfg, repeat_index=cfg.repeat_index + rep + 1, campaign_stage="repro_check")
                    if args.dry_run:
                        rep_capture = capture
                        rep_telemetry = telemetry
                    else:
                        backend.configure(rep_cfg)
                        backend.arm(rep_cfg)
                        rep_telemetry = backend.wait_for_result(timeout_s=0.8)
                        rep_capture = target.capture_response(timeout_s=0.8)
                    rep_desc = describe_pattern(rep_cfg.pattern, rep_cfg.pattern_width)
                    rep_dist = compute_distance_metrics(rep_capture, baseline)
                    rep_class = classify_attempt(rep_capture, baseline, rep_telemetry, rep_dist)
                    rep_nearest, rep_novelty = novelty.score(rep_capture.normalized, rep_class.label)
                    rep_dist["distance_to_nearest_seen"] = rep_nearest
                    rep_energy = compute_energy_score(rep_cfg, rep_desc)
                    rep_score = candidate_score(rep_class, rep_novelty, rep_energy, reproducibility_hint=1.0)
                    jsonl.write({
                        "campaign_id": campaign_id,
                        "attempt_id": attempt_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "firmware": firmware,
                        "config": {
                            "trigger_edge_count": rep_cfg.trigger_edge_count,
                            "holdoff_delay": rep_cfg.holdoff_delay,
                            "pattern": f"0b{bitstring(rep_cfg.pattern, rep_cfg.pattern_width)}",
                            "pattern_width": rep_cfg.pattern_width,
                            "glitch_clock_frequency": rep_cfg.glitch_clock_frequency,
                            "target_vcc": rep_cfg.target_vcc,
                            "pulse_length_ns": rep_cfg.pulse_length_ns,
                            "repeat_index": rep_cfg.repeat_index,
                            "campaign_stage": rep_cfg.campaign_stage,
                        },
                        "pattern_descriptors": rep_desc,
                        "glitcher_telemetry_raw": rep_telemetry.get("raw", ""),
                        "target_uart_raw_hex": rep_capture.raw.hex(),
                        "target_uart_text": rep_capture.text,
                        "normalized_output": rep_capture.normalized,
                        "timing": {"time_to_first_byte_ms": rep_capture.time_to_first_byte_ms, "response_duration_ms": rep_capture.response_duration_ms},
                        "classification": asdict(rep_class),
                        "distance": rep_dist,
                        "scores": {"novelty_score": rep_novelty, "energy_score": rep_energy, "candidate_score": rep_score},
                        "notes": "reproducibility_check",
                    })
        jsonl.write({
            "record_type": "campaign_end",
            "campaign_id": campaign_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attempts": attempt_id,
            "resets": total_resets,
            "recent_reset_rate": (sum(1 for r in recent if r["classification"]["label"] == "RESET_ARTIFACT") / len(recent)) if recent else 0.0,
            "novelty_archive_size": {"non_reset": len(novelty.non_reset_seen), "reset": len(novelty.reset_seen)},
        })
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user; closing resources.")
        jsonl.write({"record_type": "campaign_interrupt", "campaign_id": campaign_id, "timestamp": datetime.now(timezone.utc).isoformat(), "reason": "keyboard_interrupt"})
        return 130
    except Exception as exc:
        logging.exception("Fatal error: %s", exc)
        jsonl.write({"record_type": "campaign_error", "campaign_id": campaign_id, "timestamp": datetime.now(timezone.utc).isoformat(), "error": str(exc)})
        return 1
    finally:
        try:
            target.close()
        except Exception:
            pass
        try:
            backend.close()
        except Exception:
            pass
        try:
            jsonl.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
