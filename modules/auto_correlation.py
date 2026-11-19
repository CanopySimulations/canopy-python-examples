"""Fit a Canopy car to a reference speed trace using successive Star studies.

    python -m modules.auto_correlation modules\\run.json [--output NEW_DIRECTORY]

Copy the run.json template and edit it for your tenant. Inputs accept config IDs (32-character hex or
UUID), spec-relative JSON files, or names. Exact tenant names take precedence
over Canopy defaults. To pin a tenant config, use its ID.
Inline telemetry (JSON channels or chart CSV) is run once in a Telemetry
simulation; the Star studies use the job reference saved in telemetry.json.
Existing references need no preparation.

Use a dedicated worksheet_name: a run creates the worksheet or clears its
rows, then links each study and its inputs. Choose a new output directory:
output_directory is relative to the spec, while --output is relative to the
working directory. The run first saves a copy of the input spec as run.json.
Other outputs include speed overlays, optimisation-report.md, and best.json
(the best solved job, inputs and zone means).

Specify parameters by path, min, max and optional valueType (default absolute).
Additive and multiplicative bounds are relative to the original car. Each Star
uses three points per parameter and scores the sum of squared zone means of
the speed delta (simulated minus reference speed). A timeout does not cancel
remote jobs. In notebooks, call await close_session() when finished.
"""

import argparse
import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
import logging
import math
from pathlib import Path
import re
from uuid import UUID

import canopy
from canopy.openapi.exceptions import ApiException
import numpy as np
from matplotlib.figure import Figure

if __package__:
    from .config_objects import DELTA_CHANNEL, Exploration, Parameter, Telemetry, UserMaths
    from .telemetry_creator import solve_telemetry, telemetry_study_inputs, write_export
else:  # Also support running the file directly.
    from config_objects import DELTA_CHANNEL, Exploration, Parameter, Telemetry, UserMaths
    from telemetry_creator import solve_telemetry, telemetry_study_inputs, write_export


def read_config(path):
    with path.open(encoding="utf-8-sig") as stream:
        data = json.load(stream)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON configuration object")
    return data.get("config", data)


@dataclass(frozen=True)
class Spec:
    worksheet_name: str
    inputs: dict[str, str]
    study_type: str
    sim_type: str
    zones: dict | list
    parameters: tuple[Parameter, ...]
    sim_version: str | None = None
    telemetry_type: str = "telemetry"
    distance_channel: str = "sRun"
    speed_channel: str = "vCarMean"
    iterations: int = 12
    min_scale: float = 0.0625
    timeout_seconds: float = 1800
    output_directory: str | None = None
    _source_json: bytes | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: Path):
        source_json = path.read_bytes()
        data = json.loads(source_json.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object for the run specification")
        data.pop("_notes", None)
        if not isinstance(data.get("parameters"), list) or not data["parameters"]:
            raise ValueError("parameters must be a nonempty array of {path, min, max} objects")
        spec = cls(**{**data, "parameters": tuple(Parameter.from_spec(p) for p in data["parameters"])})
        object.__setattr__(spec, "_source_json", source_json)
        return spec

    def write_snapshot(self, path: Path) -> None:
        source_json = self._source_json
        if source_json is None:
            data = asdict(self)
            del data["_source_json"]
            data["parameters"] = [
                {"path": p.path, "min": p.minimum, "max": p.maximum, "valueType": p.value_type}
                for p in self.parameters
            ]
            source_json = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode("utf-8")
        with path.open("xb") as stream:
            stream.write(source_json)

    def __post_init__(self):
        if self.output_directory is not None and (
            not isinstance(self.output_directory, str) or not self.output_directory.strip()
        ):
            raise ValueError("output_directory must be a nonempty path")
        if not isinstance(self.worksheet_name, str) or not self.worksheet_name.strip():
            raise ValueError("worksheet_name must be a nonempty name for this run's destination")
        if type(self.iterations) is not int or self.iterations < 1 or not 0 < self.min_scale <= 1:
            raise ValueError("Expected iterations >= 1 and 0 < min_scale <= 1")
        if len(set(p.path for p in self.parameters)) != len(self.parameters):
            raise ValueError("Duplicate car parameter paths")
        kinds = {"car", "track", "weather", self.telemetry_type}
        if not kinds <= self.inputs.keys() or {"exploration", "userMaths"} & self.inputs.keys():
            raise ValueError(f"inputs must include {sorted(kinds)}; User Maths and exploration are generated")
        if not all(isinstance(s, str) and s.strip() for s in self.inputs.values()):
            raise ValueError("Each input must be a config ID, path, tenant name or Canopy default name")

    @property
    def channels(self):
        return [self.distance_channel, self.speed_channel, DELTA_CHANNEL]

    def build_maths(self):
        return UserMaths.build(self.zones, self.distance_channel)

    def validate_study(self, session):
        # create_study silently ignores inputs not used by a study type.
        definition = session.study_types.get_study_type_definition(self.study_type, self.sim_version)
        supported = {item.config_type for item in definition.inputs}
        required = {item.config_type for item in definition.inputs if item.is_required}
        supplied = set(self.inputs) | {"userMaths", "exploration"}
        sims = [canopy.ensure_sim_type_string(s) for s in definition.sim_types]
        if supplied - supported or required - supplied or canopy.ensure_sim_type_string(self.sim_type) not in sims:
            raise ValueError(f"Incompatible study/sim: unused {supplied-supported}, "
                             f"missing {required-supplied}, available sims {definition.sim_types}")


def _config_id(source: str) -> str | None:
    if re.fullmatch(r"[0-9a-fA-F]{32}", source):
        return source
    try:
        return str(UUID(source))
    except ValueError:
        return None


def _load_local_input(kind, path, telemetry_type):
    if path.suffix.lower() != ".csv":
        return canopy.LocalConfig(kind, path.stem, read_config(path))
    if kind != telemetry_type:
        raise ValueError(f"{kind}: {path.name} is a CSV, not a Canopy config JSON. "
                         "Use the ID, name, or exported JSON of the corresponding config.")
    return canopy.LocalConfig(kind, path.stem, Telemetry.from_csv(path).to_config())


async def _load_named_input(session, kind, source, path, version):
    try:
        found = await canopy.find_config(session, kind, name=source, sim_version=version)
    except canopy.NotFoundError:
        pass
    else:
        return found.to_local_config()

    # The default-config API adds its own .json suffix. Keep the literal name
    # as a candidate in case .json is part of the actual display name.
    names = [source]
    if source.lower().endswith(".json") and len(source) > 5:
        names.insert(0, source[:-5])
    for name in names:
        try:
            config = await canopy.load_default_config(session, kind, name, sim_version=version)
        except canopy.NotFoundError:
            continue
        except ApiException as exc:
            if exc.status != 404:
                raise
        else:
            logging.info("No tenant %s named %r; using Canopy default %r", kind, source, name)
            return config
    raise canopy.NotFoundError(
        f"Could not resolve inputs.{kind}={source!r}: no local file at {path}, "
        f"no exact tenant match, and no Canopy default named "
        f"{' or '.join(map(repr, names))} for {version or 'the tenant simulation version'}."
    )


async def load_input(session, kind, source, directory, version, telemetry_type="telemetry"):
    """Resolve by ID, file, tenant name, or built-in name. Stage local uploads."""
    config_id = _config_id(source)
    if config_id is not None:
        config = (await canopy.load_config(session, config_id=config_id, sim_version=version)).to_local_config()
    else:
        path = directory / source
        if path.is_file():
            return _load_local_input(kind, path, telemetry_type)
        if Path(source).is_absolute() or source.startswith(("./", ".\\", "../", "..\\")):
            raise FileNotFoundError(f"inputs.{kind}: no local config file at {path}")
        config = await _load_named_input(session, kind, source, path, version)
    if config.config_type != kind:
        raise ValueError(f"{source}: expected {kind}, got {config.config_type}")
    return config


@dataclass
class RunWorksheet:
    session: canopy.Session
    worksheet: canopy.openapi.GetWorksheetQueryResultWorksheet

    @classmethod
    async def open(cls, session, name):
        tenant = session.authentication.tenant_id
        # find_config returns the newest match. Never choose between duplicates
        # when the next operation will erase the worksheet's existing rows.
        query = canopy.create_list_filter(session, is_study=False, name=name, items_per_page=2)
        matches = (await canopy.openapi.ConfigApi(session.async_client).config_get_configs(
            tenant, "worksheet", filter=query.serialize(), result_type="queryOnly")).query_results
        docs = matches.documents
        if len(docs) > 1 or matches.has_more_results:
            raise ValueError(f"Multiple worksheets match {name!r}; use a unique worksheet name")
        api = canopy.openapi.WorksheetApi(session.async_client)
        if docs:
            worksheet = (await api.worksheet_get_worksheet(tenant, docs[0].document_id)).worksheet
            if worksheet.name != name:
                raise ValueError(f"Refusing to clear {worksheet.name!r}; expected {name!r}")
            result = cls(session, worksheet)
            await result.replace_rows([])
            return result
        labels = canopy.openapi.CollatedLabelDefinitionsWorksheet(
            config_label_definitions=[], simulation_label_definitions=[])
        request = canopy.openapi.WorksheetPostWorksheetRequest(
            name=name, properties=[], notes="",
            outline=canopy.openapi.NewWorksheetDataOutline(rows=[], label_definitions=labels))
        return cls(session, (await api.worksheet_post_worksheet(tenant, request)).worksheet)

    @property
    def id(self):
        return self.worksheet.worksheet_id

    async def upload(self, kind, name, data, version):
        # The convenience create_config helper doesn't expose parent_worksheet_id.
        self.session.authentication.authenticate()
        request = canopy.openapi.NewConfigData(name=name, config_type=kind, config=data, properties=[],
                                               sim_version=version, parent_worksheet_id=self.id)
        return await canopy.openapi.ConfigApi(self.session.async_client).config_post_config(
            self.session.authentication.tenant_id, request)

    async def replace_rows(self, rows):
        sheet = self.worksheet
        properties = sheet.properties
        if not isinstance(properties, list):
            properties = canopy.properties_dict_to_list(properties)
        request = canopy.openapi.WorksheetPutWorksheetRequest(
            name=sheet.name, properties=properties, notes=sheet.notes,
            outline=canopy.openapi.UpdatedWorksheetDataOutline(
                rows=rows, label_definitions=sheet.outline.label_definitions))
        self.session.authentication.authenticate()
        self.worksheet = (await canopy.openapi.WorksheetApi(self.session.async_client).worksheet_put_worksheet(
            self.session.authentication.tenant_id, self.id, request)).worksheet

    async def add_row(self, name, ids, study):
        tenant = self.session.authentication.tenant_id
        configs = [canopy.openapi.WorksheetConfig(
            config_type=kind, inherit_reference=False, reference=canopy.openapi.ConfigResolvedLabelsReference(
                tenant=canopy.openapi.ConfigReferenceTenant(tenant_id=tenant, target_id=config_id)))
            for kind, config_id in ids.items()]
        row = canopy.openapi.WorksheetRow(name=name, configs=configs,
            study=canopy.openapi.WorksheetRowStudy(reference=canopy.openapi.StudyResolvedLabelsReference(
                tenant_id=tenant, target_id=study)))
        await self.replace_rows([*(self.worksheet.outline.rows or []), row])


@dataclass(frozen=True)
class Result:
    study: str
    job: int
    point: tuple
    means: tuple
    score: float


def rank(study, table, points, names, sim_type):
    columns = [f"{name}:{sim_type}" for name in names]
    required = ["jobIndex", *columns]
    if table is None or any(key not in table for key in required):
        raise ValueError(f"Study {study}: expected scalar columns {required}")
    ranked = []
    seen = set()
    for index, *values in table[required].itertuples(index=False, name=None):
        job = int(index)
        if job != index or not 0 <= job < len(points) or job in seen:
            raise ValueError(f"Study {study}: unexpected job index {index}")
        seen.add(job)
        means = tuple(map(float, values))
        score = sum(v * v for v in means)
        if math.isfinite(score):
            ranked.append(Result(study, job, points[job], means, score))
    if not ranked:
        raise RuntimeError(f"Study {study}: no job returned finite means for every zone")
    if len(ranked) < len(points):
        logging.warning("%s: scoring %d of %d points", study, len(ranked), len(points))
    return sorted(ranked, key=lambda r: (r.score, r.job))


async def plot(session, ranked, sim_type, channels, zones, file):
    fig = Figure(figsize=(10, 5), layout="constrained")
    ax = fig.subplots()
    for i, result in enumerate(ranked[:3]):
        job = await canopy.load_study_job(session, result.study, sim_type, channels, result.job)
        if any(c not in job.vector_data for c in channels):
            raise ValueError(f"{result.study} job {result.job}: expected vector channels {channels}")
        distance, speed, delta = (job.vector_as(c, unit)
                                  for c, unit in zip(channels, ("m", "m/s", "m/s")))
        if not i:
            ax.plot(distance, speed - delta, color="black", linestyle="--", label="Telemetry")
        ax.plot(distance, speed, label=f"Job {result.job}: {result.score:.4g}")
    for start, end in zones:
        ax.axvspan(start, end, color="grey", alpha=0.1)
    ax.set(xlabel=f"{channels[0]} [m]", ylabel="Speed [m/s]", title=file.stem)
    ax.legend()
    fig.savefig(file)


def _next_star(ranked, best, low, high, half_width, scale):
    # Fit the latest Star's slopes, but anchor the step at the best solved point.
    anchor = np.array(best.point)
    offsets = (np.array([r.point for r in ranked]) - anchor) / half_width
    design = np.column_stack((np.ones(len(ranked)), offsets))
    fit, _, rank_count, _ = np.linalg.lstsq(design, [r.means for r in ranked], rcond=None)
    if rank_count != design.shape[1]:
        raise RuntimeError(f"Study {ranked[0].study}: not enough independent solved points for gradients")
    step, *_ = np.linalg.lstsq(fit[1:].T, -np.array(best.means), rcond=None)
    centre = np.clip(anchor + half_width * np.clip(step, -scale, scale), low, high)
    # Make the centre a solved point: the midpoint in the interior, an endpoint on a bound.
    radius = np.minimum(scale * half_width, np.minimum(centre - low, high - centre))
    radius = np.where(radius == 0, scale * half_width, radius)
    return centre, np.maximum(low, centre - radius), np.minimum(high, centre + radius)


async def optimise(evaluate, initial, bounds, iterations, min_scale):
    """Estimate the zone-mean Jacobian from each solved Star in scaled coordinates."""
    low, high = np.asarray(bounds, dtype=float).T
    half_width = (high - low) / 2
    centre = np.clip(initial, low, high)
    lower, upper = low, high
    best = None
    scale = 1.0
    for iteration in range(1, iterations + 1):
        ranked = await evaluate(iteration, centre, lower, upper)
        winner = ranked[0]
        improved = best is None or winner.score < best.score
        if improved:
            best = winner
        if best.score == 0:
            return best, "zero zone error"
        if iteration == iterations:
            break
        if not improved and scale <= min_scale:
            return best, "no improvement at minimum step"
        scale = scale if improved else max(min_scale, scale / 2)
        centre, lower, upper = _next_star(ranked, best, low, high, half_width, scale)
    return best, "iteration limit"


def write_report(output, spec, names, steps, reason=None):
    """Report only solved jobs; never present a proposed parameter vector as an improvement."""
    first = steps[0][0]
    best = min((winner for winner, _ in steps), key=lambda r: r.score)
    lines = ["# Auto-correlation report", "", f"Worksheet: {spec.worksheet_name}",
             f"Status: {reason or 'in progress (completed explorations only)'}.", "",
             "Objective: sum of squared signed zone means (lower is better).",
             "The first score below is the first exploration's winner, not necessarily the baseline car.", "",
             "| Step | Study | Best job (zero-based) | Scored jobs | Step score | Best so far |",
             "|---:|---|---:|---:|---:|---:|"]
    incumbent = first.score
    for i, (winner, count) in enumerate(steps, 1):
        incumbent = min(incumbent, winner.score)
        lines.append(f"| {i} | `{winner.study}` | {winner.job} | {count} | {winner.score:.8g} | {incumbent:.8g} |")
    if first.score:
        lines.extend(["", f"Improvement from the first step winner: {100 * (1 - best.score / first.score):.2f}%."])
    lines.extend(["", "## Parameter path", "", "| Step | " + " | ".join(f"`{p.path}`" for p in spec.parameters) + " |",
                  "|---:" + "|---:" * len(spec.parameters) + "|"])
    for i, (winner, _) in enumerate(steps, 1):
        lines.append(f"| {i} | " + " | ".join(f"{v:.8g}" for v in winner.point) + " |")
    lines.extend(["", "## Best job: residuals", "", "| Zone scalar | Signed mean | Contribution to score |",
                  "|---|---:|---:|"])
    for name, value in sorted(zip(names, best.means), key=lambda item: abs(item[1]), reverse=True):
        lines.append(f"| `{name}` | {value:.8g} | {value * value:.8g} |")
    lines.extend(["", "Residuals are ordered by contribution. A small signed mean can hide opposing "
                  "errors within a zone; check the speed overlays before accepting the fit."])
    (output / "optimisation-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _prepare_inputs(worksheet, spec, sources, telemetry_inputs, output):
    version = spec.sim_version
    telemetry_type = spec.telemetry_type
    configs = {kind: source.config_id or await worksheet.upload(kind, source.name, source.raw_data, version)
               for kind, source in sources.items()
               if telemetry_inputs is None or kind != telemetry_type}
    if telemetry_inputs is None:
        return configs, None

    session = worksheet.session
    telemetry = sources[telemetry_type]
    telemetry_study, reference = await solve_telemetry(
        session, {kind: configs[kind] for kind in telemetry_inputs if kind != telemetry_type},
        version, spec.timeout_seconds, worksheet, telemetry=telemetry)
    write_export(output / "telemetry.json", reference, version or session.tenant_sim_version.get())
    configs[telemetry_type] = await worksheet.upload(
        telemetry_type, f"{telemetry.name} (reference)", reference, version)
    return configs, telemetry_study


def _build_star(parameters, baseline, initial, centre, lower, upper, *, first_step):
    """Build the sweep and, only if recentered, a replacement car config."""
    if np.array_equal(initial, centre):
        car = None
    else:
        car = deepcopy(baseline)
        for param, value in zip(parameters, centre):
            param.write(car, value)

    windows = None
    if not first_step or car is not None:
        windows = [p.relative_bounds(x, lo, hi) for p, x, lo, hi in zip(parameters, centre, lower, upper)]
        if not all(math.isfinite(lo) and math.isfinite(hi) for lo, hi in windows):
            raise ValueError("Star sweep bounds must be finite")
    return car, Exploration.build(parameters, windows).to_config()


def _rank_study(study, loaded, parameters, names, sim_type):
    if not loaded.jobs:
        raise RuntimeError(f"Study {study}: missing solved job inputs")
    # Jobs come in jobIndex order. Use the actual solved car values, not the sweep order.
    cars = (canopy.ensure_dict(job.result.study_job_input)["simConfig"]["car"] for job in loaded.jobs)
    points = [tuple(p.read(car) for p in parameters) for car in cars]
    table = loaded.scalar_results.results if loaded.scalar_results is not None else None
    return rank(study, table, points, names, sim_type)


async def correlate(session, spec: Spec, directory, output):
    if output.exists():
        raise FileExistsError(f"Choose a new output directory: {output}")
    maths = spec.build_maths()
    names, ranges = maths.names, maths.ranges
    version = spec.sim_version
    sim = canopy.ensure_sim_type_string(spec.sim_type)
    parameters = spec.parameters
    output.mkdir(parents=True, exist_ok=False)
    spec.write_snapshot(output / "run.json")
    if session is None:
        session = get_session()
    session.authentication.authenticate()
    spec.validate_study(session)
    sources = {kind: await load_input(session, kind, source, directory, version, spec.telemetry_type)
               for kind, source in spec.inputs.items()}
    telemetry_inputs = None
    if "channels" in sources[spec.telemetry_type].raw_data:
        telemetry_inputs = telemetry_study_inputs(session, spec.telemetry_type, sources, version)
    baseline = sources["car"].raw_data
    initial = [p.read(baseline) for p in parameters]
    bounds = [p.absolute_bounds(x) for p, x in zip(parameters, initial)]
    worksheet = await RunWorksheet.open(session, spec.worksheet_name)
    configs, telemetry_study = await _prepare_inputs(worksheet, spec, sources, telemetry_inputs, output)
    configs["userMaths"] = await worksheet.upload("userMaths", "Correlation zone means",
                                                 maths.to_config(), version)
    study_inputs = {}
    steps = []

    async def evaluate(step, centre, lower, upper):
        label = f"{step:02}-star"
        ids = configs.copy()
        car, sweep = _build_star(parameters, baseline, initial, centre, lower, upper, first_step=step == 1)
        if car is not None:
            ids["car"] = await worksheet.upload("car", f"Correlation {label}", car, version)
        ids["exploration"] = await worksheet.upload("exploration", f"Correlation {label}", sweep, version)
        study = await canopy.create_study(session, spec.study_type, f"Correlation {label}",
                                          list(ids.values()), sim_version=version)
        study_inputs[study] = ids
        print(f"{label}: {study}", flush=True)
        await worksheet.add_row(f"Step {step}", ids, study)
        await canopy.wait_for_study(session, study, timeout_seconds=spec.timeout_seconds)
        loaded = await canopy.load_study(session, study, include_study_scalar_results=True, include_job_inputs=True)
        ranked = _rank_study(study, loaded, parameters, names, sim)
        steps.append((ranked[0], len(ranked)))
        write_report(output, spec, names, steps)
        await plot(session, ranked, sim, spec.channels, ranges, output / f"{label}.png")
        return ranked

    best, reason = await optimise(evaluate, initial, bounds, spec.iterations, spec.min_scale)
    summary = {**asdict(best), "parameters": {p.path: value for p, value in zip(parameters, best.point)},
               "zones": spec.zones, "zone_means": dict(zip(names, best.means)), "stop_reason": reason,
               "inputs": study_inputs[best.study], "baseline_car": configs["car"],
               "worksheet_id": worksheet.id, "worksheet_name": spec.worksheet_name}
    if telemetry_study is not None:
        summary["telemetry_study"] = telemetry_study
    (output / "best.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_report(output, spec, names, steps, reason)
    return summary


_sessions: dict[asyncio.AbstractEventLoop, canopy.Session] = {}


def get_session() -> canopy.Session:
    """Borrow the current event loop's authenticated session."""
    loop = asyncio.get_running_loop()
    if loop not in _sessions:
        session = canopy.Session()
        try:
            session.authentication.authenticate()
            _sessions[loop] = session
        finally:
            if loop not in _sessions:
                session.close_sync()  # Sign-in failed before opening an async client.
    return _sessions[loop]


async def close_session() -> None:
    session = _sessions.pop(asyncio.get_running_loop(), None)
    if session is not None:
        await session.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec", type=Path, help="Run specification JSON")
    parser.add_argument("--output", type=Path, help="Override the spec's output_directory for plots and best.json")
    args = parser.parse_args()
    spec = Spec.load(args.spec)
    output = args.output
    if output is None:
        if spec.output_directory is None:
            parser.error("set output_directory in the spec or pass --output")
        output = args.spec.parent / spec.output_directory
    result = await correlate(None, spec, args.spec.parent, output)
    print(json.dumps(result, indent=2))


async def _run_cli():
    try:
        await main()
    finally:
        await close_session()


if __name__ == "__main__":
    asyncio.run(_run_cli())