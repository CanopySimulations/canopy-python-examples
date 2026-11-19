"""Build inner Canopy config objects without signing in or making API calls.

Telemetry reads Canopy chart CSVs (title, names, units, then samples) into SI
channels for a Telemetry simulation. UserMaths makes one gated delta mean per
distance zone, and Parameter and Exploration build three-point Star sweeps.
Use to_config() for the inner payload, not a portal export envelope.
"""

import csv
from dataclasses import asdict, dataclass
from functools import cached_property
import json
import math
from pathlib import Path
import re

from canopy import Units
from canopy.units import UnitDefinition


DELTA_CHANNEL = "vCarDeltaToRef"


@dataclass
class TelemetryChannel:
    name: str
    units: str
    data: list[float]
    description: str = ""


def _telemetry_column(name: str, unit: str, converter: Units, path: Path) -> tuple[TelemetryChannel, UnitDefinition]:
    conversion = converter.try_get_conversion_to_si(unit)
    if conversion is None:
        raise ValueError(f"{path.name}: unknown unit {unit!r} for {name}")
    return TelemetryChannel(name, conversion.si_unit, []), conversion


def _si_sample(field: str, name: str, conversion: UnitDefinition, location: str) -> float:
    try:
        value = float(field) * conversion.factor + conversion.offset
    except ValueError as exc:
        raise ValueError(f"{location}: invalid {name} sample") from exc
    if not math.isfinite(value):
        raise ValueError(f"{location}: nonfinite {name} sample")
    return value


@dataclass
class Telemetry:
    channels: list[TelemetryChannel]

    @classmethod
    def from_csv(cls, path: Path):
        """Read a Canopy chart export: metadata, channel names, units, then samples."""
        converter = Units()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            next(reader, None)  # Chart title, not a sample or channel description.
            names = [name.strip() for name in next(reader, [])]
            units = [unit.strip() for unit in next(reader, [])]
            if not names or not all(names) or len(set(names)) != len(names) or len(units) != len(names):
                raise ValueError(f"{path.name}: expected unique channel names and one unit per channel")
            columns = [_telemetry_column(name, unit, converter, path) for name, unit in zip(names, units)]
            for row in reader:
                if not row:
                    continue
                location = f"{path.name}:{reader.line_num}"
                if len(row) != len(columns):
                    raise ValueError(f"{location}: expected {len(columns)} fields")
                for (channel, conversion), field in zip(columns, row):
                    channel.data.append(_si_sample(field, channel.name, conversion, location))
        channels = [channel for channel, _ in columns]
        if not channels[0].data:
            raise ValueError(f"{path.name}: no telemetry samples")
        return cls(channels)

    def to_config(self):
        return asdict(self)


def _is_finite_number(value):
    return type(value) in (float, int) and math.isfinite(value)


@dataclass(frozen=True)
class UserMaths:
    gates: tuple
    results: tuple

    @classmethod
    def build(cls, zones, distance_channel="sRun"):
        if not isinstance(zones, (dict, list)) or not zones:
            raise ValueError("Supply at least one distance range in zones")
        named = zones.items() if isinstance(zones, dict) else (
            (f"Zone{i}", bounds) for i, bounds in enumerate(zones, 1))
        gates, results = [], []
        for name, bounds in named:
            if not name.isidentifier():
                raise ValueError(f"Invalid gate identifier: {name!r}")
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                    or not all(_is_finite_number(v) for v in bounds)
                    or not 0 <= bounds[0] < bounds[1]):
                raise ValueError(f"Zone {name}: expected 0 <= lowerBound < upperBound; got {bounds!r}")
            gates.append({"name": "Windowed Boolean Gate", "gateName": name,
                          "channelName": distance_channel, "lowerBound": bounds[0], "upperBound": bounds[1]})
            results.append({"logicalCondition": name, "channelsAndResults": [{
                "type": "Mean (time)", "channelName": DELTA_CHANNEL, "resultName": f"{DELTA_CHANNEL}_{name}"}]})
        return cls(tuple(gates), tuple(results))

    @property
    def names(self):
        return [r["channelsAndResults"][0]["resultName"] for r in self.results]

    @property
    def ranges(self):
        return [(gate["lowerBound"], gate["upperBound"]) for gate in self.gates]

    def to_config(self):
        return {"scalars": {"booleanGates": list(self.gates), "scalarResultDefinitions": list(self.results)}}

    def to_json(self):
        return json.dumps(self.to_config(), allow_nan=False)


@dataclass(frozen=True)
class Parameter:
    path: str
    minimum: float
    maximum: float
    value_type: str

    @classmethod
    def from_spec(cls, item):
        path = item["path"].removeprefix("car.")
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+\]|\.[A-Za-z_]\w*)*", path):
            raise ValueError(f"Invalid car parameter path: {path!r}")
        lo, hi = item["min"], item["max"]
        if not all(_is_finite_number(v) for v in (lo, hi)) or lo >= hi:
            raise ValueError(f"{path}: expected finite min < max")
        kind = item.get("valueType", "absolute")
        if kind not in {"multiplicative", "absolute", "additive"}:
            raise ValueError(f"{path}: unknown valueType {kind!r}")
        return cls(path, lo, hi, kind)

    @cached_property
    def _keys(self):
        return tuple(int(i) if i else k for k, i in re.findall(r"([A-Za-z_]\w*)|\[(\d+)\]", self.path))

    def leaf(self, car):
        for key in self._keys[:-1]:
            car = car[key]
        return car, self._keys[-1]

    def read(self, car):
        node, key = self.leaf(car)
        value = node[key]
        if not _is_finite_number(value):
            raise ValueError(f"{self.path} must be a finite numeric car setting")
        return value

    def write(self, car, value):
        node, key = self.leaf(car)
        node[key] = float(value)

    def absolute_bounds(self, baseline):
        if self.value_type == "multiplicative":
            if baseline == 0 or self.minimum <= 0 <= self.maximum:
                raise ValueError(f"{self.path}: use absolute or additive bounds to tune through zero")
            return sorted((baseline * self.minimum, baseline * self.maximum))
        offset = baseline if self.value_type == "additive" else 0
        return self.minimum + offset, self.maximum + offset

    def relative_bounds(self, centre, lower, upper):
        if self.value_type == "multiplicative":
            return sorted((lower / centre, upper / centre))
        if self.value_type == "additive":
            return lower - centre, upper - centre
        return lower, upper

    def sweep(self, lower, upper):
        return {"dimensionType": "interpolation", "numberOfPoints": 3, "parallelSubSweeps": [{
            "parameterPath": f"car.{self.path}", "valueType": self.value_type,
            "valueStart": lower, "valueEnd": upper, "interpolationType": "linear"}]}


@dataclass(frozen=True)
class Exploration:
    sweeps: tuple

    @classmethod
    def build(cls, parameters, windows=None):
        if windows is None:
            windows = [(p.minimum, p.maximum) for p in parameters]
        if not parameters or len(parameters) != len(windows):
            raise ValueError("Expected one sweep window per parameter")
        return cls(tuple(p.sweep(lo, hi) for p, (lo, hi) in zip(parameters, windows)))

    def to_config(self):
        return {"design": {"name": "Star", "sweeps": list(self.sweeps)}}

    def to_json(self):
        return json.dumps(self.to_config(), allow_nan=False)
