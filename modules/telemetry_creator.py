"""Run inline telemetry in a Canopy study and export its job reference."""

import json
from pathlib import Path

import canopy


def write_export(path: Path, config: dict, sim_version: str) -> None:
    """Write an inner config in the portal's JSON export envelope."""
    envelope = {"simVersion": sim_version, "customProperties": {}, "config": config}
    path.write_text(json.dumps(envelope, indent=1, allow_nan=False) + "\n", encoding="utf-8")


def telemetry_study_inputs(session, telemetry_type, available_types, sim_version):
    definition = session.study_types.get_study_type_definition("telemetry", sim_version)
    if not any(item.config_type == telemetry_type for item in definition.inputs):
        raise ValueError(f"Telemetry study does not accept a {telemetry_type!r} config")
    if not any(canopy.ensure_sim_type_string(s) == "Telemetry" for s in definition.sim_types):
        raise ValueError(f"Expected Telemetry simulation in study; got {definition.sim_types}")
    required = {telemetry_type} | {item.config_type for item in definition.inputs if item.is_required}
    if missing := required.difference(available_types):
        raise ValueError(f"Missing required Telemetry study inputs: {sorted(missing)}")
    return sorted(required)


async def solve_telemetry(session, configs, version, timeout_seconds, worksheet, *, telemetry: canopy.LocalConfig):
    """Run a single Telemetry job and return a lap-study reference to its results."""
    print("Submitting the Telemetry preparation study", flush=True)
    study = await canopy.create_study(session, "telemetry", "Prepare reference telemetry",
                                      [*configs.values(), telemetry], sim_version=version)
    print(f"Telemetry: {study}", flush=True)
    await worksheet.add_row("Telemetry", configs, study)
    done = await canopy.wait_for_study(session, study, timeout_seconds=timeout_seconds)
    if done.simulation_count != 1 or done.succeeded_simulation_count != 1:
        errors = "; ".join(done.data.error_messages or []) or "no study-level error message"
        raise RuntimeError(f"Telemetry study {study} did not produce one successful simulation: {errors}")
    return study, {"source": {
        "tenantId": session.authentication.tenant_id,
        "studyId": study,
        "jobIndex": 0,
    }}
