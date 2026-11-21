"""Validate telemetry and prepare a Canopy study-job reference for correlation."""

from dataclasses import dataclass
import json
from pathlib import Path

import canopy


def write_export(path: Path, config: dict, sim_version: str) -> None:
    """Write an inner config in the portal's JSON export envelope."""
    envelope = {"simVersion": sim_version, "customProperties": {}, "config": config}
    path.write_text(json.dumps(envelope, indent=1, allow_nan=False) + "\n", encoding="utf-8")


def telemetry_study_inputs(session, available_types, sim_version):
    """Return required Telemetry config types and report any that are missing."""
    definition = session.study_types.get_study_type_definition("telemetry", sim_version)
    required = {"telemetry"} | {item.config_type for item in definition.inputs if item.is_required}
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


@dataclass(frozen=True)
class TelemetryPreparation:
    """Validated telemetry and any inputs needed to turn it into a job reference."""

    telemetry: canopy.LocalConfig
    study_id: str | None
    required_inputs: tuple[str, ...] = ()

    @classmethod
    def validate(cls, session, sources, version):
        """Check telemetry and its study dependencies before worksheet setup."""
        telemetry = sources["telemetry"]
        data = telemetry.raw_data
        if not isinstance(data, dict):
            raise ValueError("inputs.telemetry must contain a telemetry config object")
        if "channels" in data:
            if not isinstance(data["channels"], list) or not data["channels"]:
                raise ValueError("inputs.telemetry: channels must be a nonempty array")
            inputs = tuple(kind for kind in telemetry_study_inputs(session, sources, version)
                           if kind != "telemetry")
            return cls(telemetry, None, inputs)

        source = data.get("source")
        study_id = source.get("studyId") if isinstance(source, dict) else None
        if not isinstance(study_id, str) or not study_id.strip():
            raise ValueError("inputs.telemetry must contain channels or a reference with source.studyId")
        return cls(telemetry, study_id)

    async def prepare(self, worksheet, configs, version, timeout_seconds, output) -> tuple[str, str]:
        """Export the job reference and return its config ID and study ID."""
        session = worksheet.session
        study_id = self.study_id
        if study_id is None:
            study_id, reference = await solve_telemetry(
                session, {kind: configs[kind] for kind in self.required_inputs},
                version, timeout_seconds, worksheet, telemetry=self.telemetry)
            name, config_id = f"{self.telemetry.name} (reference)", None
        else:
            reference = self.telemetry.raw_data
            name, config_id = self.telemetry.name, self.telemetry.config_id

        write_export(output / "telemetry.json", reference, version or session.tenant_sim_version.get())
        if not config_id:
            config_id = await worksheet.upload("telemetry", name, reference, version)
        return config_id, study_id
