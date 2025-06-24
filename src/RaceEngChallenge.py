import numpy as np
import canopy
import asyncio
from typing import List, Dict, Any
from copy import deepcopy
from canopy import AuthenticationData, Session, ConfigResult, StudyResult
from canopy.openapi import (
    WorksheetApi,
    StudyApi,
    NewStudyDataSource,
    NewWorksheetDataOutline,
    WorksheetRow,
    WorksheetStudyReference,
    WorksheetRowStudy,
    StudyPostStudyRequest,
    WorksheetConfig,
    WorksheetConfigReference,
    ConfigReferenceTenant,
    WorksheetPutWorksheetRequest
)

async def authenticate_canopy_sims_api(authentication_data: AuthenticationData) -> Session:
    # Create a session with the authentication data
    session = Session(authentication_data=authentication_data)
    # Try to authenticate the session. If failed, try againwith the same canopy_sims_authentication for upto 10 times.
    for _ in range(10):
        try:
            session.authentication.authenticate()
            print("Authenticated successfully!")
            break
        except Exception as e:
            print(f"Authentication failed: {e}")
            await asyncio.sleep(1)

    return session


async def reset_worksheet(
    session: Session,
    worksheet_id: str,
    row_names: List[str]
):
    """
    Resets a worksheet to contain only specified rows while preserving label definitions.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of worksheet to reset
        row_names: List of row names to preserve in the worksheet

    Returns:
        Updated worksheet object

    Example:
        await reset_worksheet(
            session=active_session,
            worksheet_id="38245ca29fa94a4c863dacfef6538c8b",
            row_names=["IndyCar Hybrid Ticket 2376", "FE Gen4 Loss Map"]
        )
    """
    # Authentication and API setup
    session.authentication.authenticate()
    tenant_id = session.authentication.tenant_id
    worksheet_api = WorksheetApi(session.async_client)

    # Get current worksheet state
    worksheet_result = await worksheet_api.worksheet_get_worksheet(tenant_id, worksheet_id)
    original_worksheet = worksheet_result.worksheet

    # Create filtered outline preserving label definitions
    filtered_outline = NewWorksheetDataOutline(
        rows=[row for row in original_worksheet.outline.rows if row.name in row_names],
        label_definitions=original_worksheet.outline.label_definitions
    )

    # Prepare and execute update
    return await worksheet_api.worksheet_put_worksheet(
        tenant_id,
        worksheet_id,
        WorksheetPutWorksheetRequest(
            name=original_worksheet.name,
            properties=original_worksheet.properties,
            outline=filtered_outline,
            notes=original_worksheet.notes
        )
    )


async def get_updated_worksheet_row_after_running_study_with_existing_row_configs(
    session: Session,
    tenant_id: str,
    original_row: WorksheetRow,
    sim_version: str,
    row_suffix: str,
    sim_types: List[str] = ["DynamicLap"]
) -> WorksheetRow:
    """Process individual worksheet row and create study"""
    # Create modified row
    modified_row = WorksheetRow(
        name=f"{original_row.name} {row_suffix}",
        configs=original_row.configs,
        study=original_row.study
    )

    # Build study configuration
    sim_config: Dict[str, Any] = {}
    sources: List[NewStudyDataSource] = []

    study = {
        'simTypes': sim_types,
        'simConfig': sim_config
    }
    
    # Process non-exploration configs
    for config in modified_row.configs:
        loaded_config = await canopy.load_config(session, config.reference.tenant.target_id)
        # Add exploration to the study dict if config is exploration
        if config.config_type == "exploration":
            study["exploration"] = loaded_config.document.data
        else:
            # Add other configs to sim_config
            sim_config[config.config_type] = loaded_config.document.data

        sources.append(NewStudyDataSource(
            config_type=config.config_type,
            user_id=loaded_config.document.user_id,
            config_id=config.reference.tenant.target_id,
            name=loaded_config.document.name
        ))

    # Get study API
    study_api = StudyApi(session.async_client)

    # Create study
    study_result = await study_api.study_post_study(
        tenant_id,
        StudyPostStudyRequest(
            name=modified_row.name,
            study=study,
            is_transient=False,
            study_type="dynamicLap",
            sources=sources,
            notes="",
            sim_version=sim_version
        )
    )

    # Add study reference
    return WorksheetRow(
        name=modified_row.name,
        configs=modified_row.configs,
        study=WorksheetRowStudy(
            reference=WorksheetStudyReference(
                tenant_id=tenant_id,
                target_id=study_result.study_id
            )
        )
    )


async def get_updated_worksheet_row_after_running_study_with_given_exploration(
    session: Session,
    tenant_id: str,
    original_row: WorksheetRow,
    exploration_config: ConfigResult,
    sim_version: str,
    row_suffix: str,
    sim_types: List[str] = ["DynamicLap"]
) -> WorksheetRow:
    """Process individual worksheet row and create study"""
    # Create modified row
    modified_row = WorksheetRow(
        name=f"{original_row.name} {row_suffix}",
        configs=[c for c in original_row.configs if c.config_type != "exploration"],
        study=original_row.study
    )

    # Build study configuration
    sim_config: Dict[str, Any] = {}
    sources: List[NewStudyDataSource] = []
    
    # Process non-exploration configs
    for config in modified_row.configs:
        loaded_config = await canopy.load_config(session, config.reference.tenant.target_id)
        sim_config[config.config_type] = loaded_config.document.data
        sources.append(NewStudyDataSource(
            config_type=config.config_type,
            user_id=loaded_config.document.user_id,
            config_id=config.reference.tenant.target_id,
            name=loaded_config.document.name
        ))

    # Add exploration data
    sources.append(NewStudyDataSource(
        config_type="exploration",
        user_id=exploration_config.document.user_id,
        config_id=exploration_config.config_id,
        name=exploration_config.document.name
    ))

    # Add the exploration WorksheetConfig to the row
    exploration_worksheet_config = WorksheetConfig(
        config_type='exploration',
        reference=WorksheetConfigReference(
            tenant=ConfigReferenceTenant(
                tenant_id=tenant_id,
                target_id=exploration_config.config_id
            )
        ),
        inherit_reference=False
    )

    modified_row.configs.append(exploration_worksheet_config)

    # Get study API
    study_api = StudyApi(session.async_client)

    # Create study
    study_result = await study_api.study_post_study(
        tenant_id,
        StudyPostStudyRequest(
            name=modified_row.name,
            study={
                "simTypes": sim_types,
                "simConfig": sim_config,
                "exploration": exploration_config.document.data
            },
            is_transient=False,
            study_type="dynamicLap",
            sources=sources,
            notes="",
            sim_version=sim_version
        )
    )

    # Add study reference
    return WorksheetRow(
        name=modified_row.name,
        configs=modified_row.configs,
        study=WorksheetRowStudy(
            reference=WorksheetStudyReference(
                tenant_id=tenant_id,
                target_id=study_result.study_id
            )
        )
    )


async def get_worksheet_row_with_name_in_worksheet_with_id(
    session: Session,
    worksheet_id: str,
    worksheet_row_name: str
) -> WorksheetRow:
    """Get worksheet row by name.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        worksheet_row_name: Worksheet row name to retrieve

    Returns:
        WorksheetRow object if found, otherwise None
    """
    
    # Authenticate and initialize APIs
    session.authentication.authenticate()
    tenant_id = session.authentication.tenant_id
    worksheet_api = WorksheetApi(session.async_client)

    # Get worksheet data
    worksheet_result = await worksheet_api.worksheet_get_worksheet(tenant_id, worksheet_id)
    worksheet = worksheet_result.worksheet

    # Get the worksheet row by name
    worksheet_row = next((row for row in worksheet.outline.rows if row.name == worksheet_row_name), None)

    # Throw an error if the row is not found
    if worksheet_row is None:
        raise ValueError(f"Worksheet row '{worksheet_row_name}' not found in worksheet with ID '{worksheet_id}'.")
    
    return worksheet_row


async def check_worksheet_row_study_exists(
    worksheet_row: WorksheetRow
) -> bool:
    """Check if a worksheet row study exists.

    Args:
        worksheet_row: WorksheetRow object to check

    Returns:
        True if the study exists, False otherwise
    """
    
    # Check if the row has a study reference
    return worksheet_row.study is not None and worksheet_row.study.reference is not None


async def check_if_all_sims_in_study_succeeded(
    study: StudyResult,
) -> bool:
    """Check if all simulations in a study succeeded.

    Args:
        study: StudyResult object to check
    Returns:
        True if all simulations succeeded, False otherwise
    """
    # Check if all simulations succeeded
    return study.succeeded_simulation_count == study.simulation_count


async def get_worksheet_row_study_with_all_succeeded_sims(
    session: Session,
    worksheet_id: str,
    worksheet_row_name: str,
    sim_type: str
) -> StudyResult:
    """Get the study of a worksheet row with all succeeded simulations.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        worksheet_row_name: Worksheet row name to analyse
        sim_type: Simulation type to check

    Returns:
        StudyResult object if all simulations succeeded
    """
    
    # Get the worksheet row
    worksheet_row = await get_worksheet_row_with_name_in_worksheet_with_id(
        session=session,
        worksheet_id=worksheet_id,
        worksheet_row_name=worksheet_row_name
    )

    # Check if the worksheet row exists
    b_worksheet_study_exists = await check_worksheet_row_study_exists(worksheet_row)
    if b_worksheet_study_exists is False:
        raise ValueError(f"Worksheet row '{worksheet_row_name}' does not have a study reference.")
    
    # Load the study
    study_id = worksheet_row.study.reference.target_id
    study = await canopy.load_study(session=session, study_id=study_id, sim_type=sim_type, include_study_full_document=True, include_job_metadata=True, include_job_scalar_results=True, include_job_vector_metadata=True)

    # Check if all simulations succeeded
    b_all_sims_succeeded = await check_if_all_sims_in_study_succeeded(study=study)
    if b_all_sims_succeeded is False:
        raise ValueError(f"Not all simulations in study '{study_id}' succeeded. Succeeded: {study.succeeded_simulation_count}, Total: {study.simulation_count}. Please re-run with different inputs and try again.")
    
    return study


async def display_jobs_with_scalar_value_above_threshold_of_worksheet_row_study(
    session: Session,
    worksheet_id: str,
    worksheet_row_name: str,
    sim_type: str,
    scalar_name: str,
    scalar_threshold: float
):
    """Analyzes the studies in the specified worksheet row and returns a summary.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        worksheet_row_name: Worksheet row name to analyse
        sim_type: Simulation type to check
        scalar_name: Name of the scalar to check
        scalar_threshold: Threshold value for the scalar
        """
    
    study = await get_worksheet_row_study_with_all_succeeded_sims(
        session=session,
        worksheet_id=worksheet_id,
        worksheet_row_name=worksheet_row_name,
        sim_type=sim_type
    )
    
    # Loop through the jobs and check ìf tDynamicLapQualityMetric is above the threshold
    for job in study.jobs:
        # Check if the scalar exists in the job's scalar data
        if scalar_name not in job.scalar_data:
            raise ValueError(f"Scalar '{scalar_name}' not found in job {job.document.document_id}.")
        # Return ids of the jobs that have tDynamicLapQualityMetric above the threshold
        if job.scalar_data.get(scalar_name) > scalar_threshold:
            print(f"Job ID: {job.document.document_id} has {scalar_name}: {job.scalar_data.get(scalar_name)}")


async def get_config_with_type_in_worksheet_row(
        session: Session,
        worksheet_id: str,
        worksheet_row_name: str,
        config_type: str,
        sim_version: str
)-> ConfigResult:    
    """Get a desired configuration in a worksheet row.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        worksheet_row_name: Worksheet row name to analyse
        """
    # Get the worksheet row
    worksheet_row = await get_worksheet_row_with_name_in_worksheet_with_id(
        session=session,
        worksheet_id=worksheet_id,
        worksheet_row_name=worksheet_row_name
    )

    # Get the config with the specified type
    config = next((c for c in worksheet_row.configs if c.config_type == config_type), None)

    if config is None:
        raise ValueError(f"Config with type '{config_type}' not found in worksheet row '{worksheet_row_name}'.")
    
    return await canopy.load_config(session, config.reference.tenant.target_id)


def get_value_at_path_in_config_data(
        config_data: Dict[str, Any],
        path: str
):
    """Get the value at the specified path in the configuration data.

    Args:
        config_data: Configuration data to check
        path: Path to check of the format 'xxx/xxxx/xxxxx...indicative of the keys in the dictionary, where each entry before a slash represents a level in the dictionary'
        """
    # Split the path into keys
    keys = path.split('/')

    # Iterate through the keys to find the value. Return None if any key is not found.
    current_data = config_data
    for key in keys:
        if key in current_data:
            current_data = current_data[key]
        else:
            return None
    # Return the value at the specified path
    return current_data


def get_config_data_derived_from_base_config_data_after_modifying_paths_with_new_values(
    base_config_data: Dict[str, Any],
    paths_and_values: Dict[str, Any],
)-> str:
    """Get a new configuration after modifying the specified paths.

    Args:
        session: Authenticated Canopy session object
        base_config_data: Base configuration data to modify
        paths_and_values: Dictionary of paths and their new values
        name_of_config_to_be_created: Name of the new configuration to be created
        sim_version: Simulation version
    """
    # Check if base_config_data is None
    if base_config_data is None:
        raise ValueError("Base configuration data is None. Please provide valid configuration data.")
    
    # Create a deep copy of the base config data
    new_config_data = deepcopy(base_config_data)

    # Loop through the paths and values and set the new values if the path is valid
    for path, value in paths_and_values.items():
        keys = path.split('/')
        current_data = new_config_data
        for key in keys[:-1]:
            if key not in current_data:
                raise ValueError(f"Path '{path}' not found in base configuration data.")
            current_data = current_data[key]
        # If value is None, remove the key
        if value is None:
            del current_data[keys[-1]]
        else:
            # Set the new value at the specified path
            if keys[-1] not in current_data:
                raise ValueError(f"Path '{path}' not found in base configuration data.")    
            current_data[keys[-1]] = value

    # Return the new configuration data
    return new_config_data
    

async def get_config_id_derived_from_base_config_data_after_modifying_paths_with_new_values(
        session: Session,
        base_config_data: Dict[str, Any],
        paths_and_values: Dict[str, Any],
        name_of_config_to_be_created: str,
        config_type: str,
        sim_version: str
) -> str:
    """Get a new configuration after modifying the specified paths.

    Args:
        session: Authenticated Canopy session object
        base_config_data: Base configuration data to modify
        paths_and_values: Dictionary of paths and their new values
        name_of_config_to_be_created: Name of the new configuration to be created
        config_type: Configuration type
        sim_version: Simulation version
    """
    # Get the base config data
    new_config_data = get_config_data_derived_from_base_config_data_after_modifying_paths_with_new_values(
        base_config_data=base_config_data,
        paths_and_values=paths_and_values
    )

    # Create a new configuration with the modified data
    return await canopy.create_config(
        session=session,
        config_type=config_type,
        name=name_of_config_to_be_created,
        config_data=new_config_data,
        sim_version=sim_version
    )


def get_config_data_derived_from_base_config_data_after_copying_from_paths_in_different_config_data(
    base_config_data: Dict[str, Any],
    copied_config_data: Dict[str, Any],
    paths_to_be_copied: List[str]
):
    """Get a new configuration after copying some paths with a different configuration.

    Args:
        session: Authenticated Canopy session object
        base_config_data: Base configuration data to modify
        copied_config_data: Copied configuration data to modify
        paths_to_be_copied: List of paths to be copied
        name_of_config_to_be_created: Name of the new configuration to be created
        config_type: Configuration type
        sim_version: Simulation version
        """
    # Check if base_config_data and copied_config_data are None
    if base_config_data is None or copied_config_data is None:
        raise ValueError("Base configuration data or copied configuration data is None. Please provide valid configuration data.")
    # Check if paths_to_be_copied is None
    if paths_to_be_copied is None:
        raise ValueError("Paths to be copied is None. Please provide valid paths.")
    
    # Create a paths and values dict to hold the paths and their new values from the swapped config
    paths_and_values = {}
    # Loop through the paths and set the new values from the swapped config
    for path in paths_to_be_copied:
        # Get the value at the specified path in the base config data
        current_value = get_value_at_path_in_config_data(base_config_data, path)
        if current_value is None:
            raise ValueError(f"Path '{path}' not found in base configuration data.")
        # Get the value at the specified path in the swapped config data
        new_value = get_value_at_path_in_config_data(copied_config_data, path)
        if new_value is None:
            raise ValueError(f"Path '{path}' not found in swapped configuration data.")
        # Set the new value at the specified path
        paths_and_values[path] = new_value

    # return the base config data with the swapped config data
    return get_config_data_derived_from_base_config_data_after_modifying_paths_with_new_values(
        base_config_data=base_config_data,
        paths_and_values=paths_and_values
    )
    

async def get_config_id_derived_from_base_config_data_after_copying_from_paths_in_different_config_data(
        session: Session,
        base_config_data: Dict[str, Any],
        copied_config_data: Dict[str, Any],
        paths_to_be_copied: List[str],
        name_of_config_to_be_created: str,
        config_type: str,
        sim_version: str
) -> str:
    """Get a new configuration after copying some paths with a different configuration.

    Args:
        session: Authenticated Canopy session object
        base_config_data: Base configuration data to modify
        copied_config_data: Copied configuration data to modify
        paths_to_be_copied: List of paths to be copied
        name_of_config_to_be_created: Name of the new configuration to be created
        config_type: Configuration type
        sim_version: Simulation version
    """
    # Get the base config data
    new_config_data = get_config_data_derived_from_base_config_data_after_copying_from_paths_in_different_config_data(
        base_config_data=base_config_data,
        copied_config_data=copied_config_data,
        paths_to_be_copied=paths_to_be_copied
    )

    # Create a new configuration with the modified data
    return await canopy.create_config(
        session=session,
        config_type=config_type,
        name=name_of_config_to_be_created,
        config_data=new_config_data,
        sim_version=sim_version
    )


async def get_study_id_in_worksheet_row(
        session: Session,
        worksheet_id: str,
        worksheet_row_name: str
) -> str:
    """Get the study ID in a worksheet row.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        worksheet_row_name: Worksheet row name to analyse
    """
    # Get the worksheet row
    worksheet_row = await get_worksheet_row_with_name_in_worksheet_with_id(
        session=session,
        worksheet_id=worksheet_id,
        worksheet_row_name=worksheet_row_name
    )

    # Check if the worksheet row exists
    b_worksheet_study_exists = await check_worksheet_row_study_exists(worksheet_row)
    if b_worksheet_study_exists is False:
        # Print a message if the study does not exist and return None
        print(f"Worksheet row '{worksheet_row_name}' does not have a study reference.")
        return None
    
    # Get the study ID
    study_id = worksheet_row.study.reference.target_id

    return study_id


async def get_stats_of_study_with_id(
        session: Session,
        study_id: str
):
    """Print the stats of a study with a given ID.

    Args:
        session: Authenticated Canopy session object
        study_id: ID of the target study
    """
    # Load the study
    study = await canopy.load_study(session, study_id, include_study_full_document=True)

    stats = {
        "study_id": study.document.document_id,
        "study_name": study.document.name,
        "study_type": study.document.data["studyType"],
        "study_state": study.document.data["studyState"],
        "simulation_count": study.simulation_count,
        "succeeded_simulation_count": study.succeeded_simulation_count
    }

    return stats


async def run_worksheet_row_study_with_configs_having_ids(
    session: Session,
    worksheet_id: str,
    source_row_name: str,
    sim_version: str,
    row_suffix: str,
    new_row_name: str = None,
    config_ids: List[str] = [],
    excluded_config_types: List[str] = [],
    sim_types: List[str] = ["DynamicLap"],
    study_type: str = "dynamicLap",
    notes: str = ""
):
    """Run a study with new configurations in a worksheet row.

    Args:
        session: Authenticated Canopy session object
        worksheet_id: ID of the target worksheet
        source_row_name: Worksheet row name used as the source for configs to be modified
        sim_version: Simulation version
        row_suffix: Suffix to append to modified row names
        config_ids: List of configuration IDs to execute in the new study
        excluded_config_types: List of configuration types to exclude from the new study
        sim_types: List of simulation types to run
        study_type: Type of study to run
        notes: Notes to add to the study
        """

    # Placeholder for the result
    result: Dict[str, str] = {}

    # Authenticate and initialize APIs
    session.authentication.authenticate()
    tenant_id = session.authentication.tenant_id
    worksheet_api = WorksheetApi(session.async_client)

    # Get worksheet data
    worksheet_result = await worksheet_api.worksheet_get_worksheet(tenant_id, worksheet_id)
    worksheet = worksheet_result.worksheet
    
    # If multiple rows with the same name exist, throw an error
    if len([row for row in worksheet.outline.rows if row.name == source_row_name]) > 1:
        raise ValueError(f"Multiple rows with the name '{source_row_name}' found in worksheet with ID '{worksheet_id}'. Please provide a unique row name.")
    
    # Loop through the worksheet rows and get the source row, throw an error if not found
    source_row = next((row for row in worksheet.outline.rows if row.name == source_row_name), None)
    if source_row is None:
        raise ValueError(f"Worksheet row '{source_row_name}' not found in worksheet with ID '{worksheet_id}'.")

    # Get the new row name
    if new_row_name is None:
        # If no new row name is provided, use the source row name with the suffix
        new_row_name = f"{source_row.name} {row_suffix}"

    # Get the new study name
    new_study_name = new_row_name
        
    # Create a placeholder for the new configs
    new_configs_list = []

    # Loop through the config_ids and load the configs
    for config_id in config_ids:
        # Load the new config
        new_config = await canopy.load_config(session, config_id)
        # Create a new worksheet config for the new config
        new_worksheet_config = WorksheetConfig(
            config_type=new_config.document.sub_type,
            reference=WorksheetConfigReference(
                tenant=ConfigReferenceTenant(
                    tenant_id=session.authentication.tenant_id,
                    target_id=config_id
                )
            ),
            inherit_reference=False
        )
        new_configs_list.append(new_worksheet_config)

    # Go through the source row configs and add them if the type is not in any of the config objects in new_configs_list
    for config in source_row.configs:
        # Check if the config type is already in the new configs list
        if config.config_type not in [c.config_type for c in new_configs_list]:
            # Load the source config
            loaded_config = await canopy.load_config(session, config.reference.tenant.target_id)
            # Create a new worksheet config for the source config
            new_worksheet_config = WorksheetConfig(
                config_type=loaded_config.document.sub_type,
                reference=WorksheetConfigReference(
                    tenant=ConfigReferenceTenant(
                        tenant_id=session.authentication.tenant_id,
                        target_id=config.reference.tenant.target_id
                    )
                ),
                inherit_reference=False
            )
            new_configs_list.append(new_worksheet_config)

    # Check if there are no type duplicates in the new configs
    config_types = [config.config_type for config in new_configs_list]
    if len(config_types) != len(set(config_types)):
        raise ValueError(f"Duplicate configuration types found in the new configs: {config_types}.")
    
    # Check if there are any excluded config types in the new configs and remove them
    for config in new_configs_list:
        if config.config_type in excluded_config_types:
            new_configs_list.remove(config)
    
    # Build study configuration
    sim_config: Dict[str, Any] = {}
    sources: List[NewStudyDataSource] = []

    # Process non-exploration configs
    for config in new_configs_list:
        loaded_config = await canopy.load_config(session, config.reference.tenant.target_id)
        sources.append(NewStudyDataSource(
            config_type=config.config_type,
            user_id=loaded_config.document.user_id,
            config_id=config.reference.tenant.target_id,
            name=loaded_config.document.name
        ))
        # Add to sim_config only if the config type is not exploration
        if config.config_type != "exploration":
            sim_config[config.config_type] = loaded_config.document.data

    # Create study configuration
    study = {
        'simTypes': sim_types,
        'simConfig': sim_config
    }

    # If a config of type exploration is in the new configs list, add it to the study dict
    exploration_config = next((config for config in new_configs_list if config.config_type == "exploration"), None)
    if exploration_config is not None:
        # Load the exploration config
        loaded_exploration_config = await canopy.load_config(session, exploration_config.reference.tenant.target_id)
        # Add exploration data to the study configuration
        study["exploration"] = loaded_exploration_config.document.data

    # Get the study API
    study_api = StudyApi(session.async_client)

    # Get the tenant ID
    tenant_id = session.authentication.tenant_id

    # Create study
    study_result = await study_api.study_post_study(
        tenant_id,
        StudyPostStudyRequest(
            name=new_study_name,
            study=study,
            is_transient=False,
            study_type=study_type,
            sources=sources,
            notes=notes,
            sim_version=sim_version
        )
    )

    # Create a new row with the new configs and study reference
    new_row = WorksheetRow(
        name=new_row_name,
        configs=new_configs_list,
        study=WorksheetRowStudy(
            reference=WorksheetStudyReference(
                tenant_id=tenant_id,
                target_id=study_result.study_id
            )
        )
    )

    # Update worksheet outline
    worksheet.outline.rows.append(new_row)
    
    # Commit changes
    worksheet_result = await worksheet_api.worksheet_put_worksheet(
        tenant_id,
        worksheet_id,
        WorksheetPutWorksheetRequest(
            name=worksheet.name,
            properties=worksheet.properties,
            outline=worksheet.outline,
            notes=worksheet.notes
        )
    )

    # Return the study ID
    return study_result.study_id


async def get_scalar_results_with_given_ids_from_jobs_in_study_with_id(
        session: Session,
        study_id: str,
        sim_type: str,
        scalar_ids: List[str],
        excluded_job_ids: List[str] = []
) -> List[Dict[str, List[Dict[str, Any]]]]:
    """Get scalar results from jobs in a study.

    Args:
        session: Authenticated Canopy session object
        study_id: ID of the study to analyse
        study_result: StudyResult object to check
        sim_type: Simulation type to check
        scalar_ids: List of scalar IDs to check against
        excluded_job_ids: List of job IDs to exclude from the search

    Returns:
        List of scalar results for the specified job IDs that looks like:
        [
            {
                'job_id': job_id,
                'inputs': [
                    { 'path': path_to_parameter,
                      'value': value_of_parameter
                    }
                ],
                'outputs': [
                    { 'id': scalar_id,
                      'value': scalar_value
                    }
                ]
            },
            ...
        ]

    """

    # Load the study with scalar data
    study_result = await canopy.load_study(
        session=session, 
        study_id=study_id,
        sim_type=sim_type,
        include_study_full_document=True, 
        include_job_metadata=True, 
        include_job_scalar_results=True, 
        include_job_vector_metadata=True
    )

    study_scalar_results = []
    # Loop through the jobs and get the scalar values
    for job in study_result.jobs:
        # Only process jobs that are not in the excluded_job_ids list
        if job.document.document_id not in excluded_job_ids:
            # Check if the scalar data is not empty
            if job.scalar_data is not None:
                # Loop through the scalar ids list and get the scalar values
                for scalar_id in scalar_ids:
                    # Check if the scalar exists in the job's scalar data
                    if scalar_id in job.scalar_data:
                        # If the job ID is not in the list of dicts in the study_scalar_results, add it and the relevant changes to the inputs
                        if job.document.document_id not in [s['job_id'] for s in study_scalar_results]:
                            # Get the changes from the job's result
                            changes = job.result.study_job.data.get('changes', [])
                            # Check if the changes exist
                            if changes:
                                inputs = [
                                    {
                                        "path": ivar['path'],
                                        "value": float(ivar['value'])
                                    } for ivar in changes
                                ]
                            else:
                                inputs = []
                            # Add the job ID and the relevant changes to the inputs
                            study_scalar_results.append(
                                {
                                    'job_id': job.document.document_id,
                                    'inputs': inputs,
                                    'outputs': []
                                }
                            )                   
                        # Get the scalar value
                        scalar_value = job.scalar_data.get(scalar_id)
                        # Add the scalar value to the scalar value dict
                        for s in study_scalar_results:
                            if s['job_id'] == job.document.document_id:
                                # Add the scalar value to the outputs list
                                s['outputs'].append(
                                    {
                                        'id': scalar_id,
                                        'value': float(scalar_value)
                                    }
                                )

    return study_scalar_results


async def get_scalar_data_from_jobs_in_study_with_id(
        session: Session,
        study_id: str,
        sim_type: str,
        scalar_limits: List[Dict[str, Any]] = []
) -> Dict[str, List[Dict[str, Any]]]:
    """Get scalar values from jobs in a study.

    Args:
        session: Authenticated Canopy session object
        study_id: ID of the study to analyse
        sim_type: Simulation type to check
        scalar_limits: List of scalar limits to check against
    """
    
    # Load the study with scalar data
    study = await canopy.load_study(
        session=session, 
        study_id=study_id,
        sim_type=sim_type,
        include_study_full_document=True, 
        include_job_metadata=True, 
        include_job_scalar_results=True, 
        include_job_vector_metadata=True
    )

    # Create a list of scalar values to check against the limits
    scalar_values: Dict[str, List[Dict[str, Any]]] = {}
    # Loop through the jobs and get the scalar values
    for job in study.jobs:
        # Initialize the scalar values dict for this job
        scalar_values[job.document.document_id] = []
        # Loop through the scalar limits list and get the scalar values
        for limit in scalar_limits:
            # If the limit has a 'min', then use it or set to -inf if not provided
            min_limit = limit.get('min', -np.inf)
            # If the limit has a 'max', then use it or set to inf if not provided
            max_limit = limit.get('max', np.inf)  
            # Check if the scalar exists in the job's scalar data and print a warning if not
            if limit['id'] not in job.scalar_data:
                print(f"Warning: Scalar '{limit['id']}' not found in job {job.document.document_id}.")
                continue
            else:
                # Get the scalar value
                scalar_value = job.scalar_data.get(limit['id'])
                # Add the scalar value, min and max limits to the scalar value dict
                scalar_values[job.document.document_id].append(
                    {
                        'scalar': limit['id'],
                        'scalar_value': float(scalar_value),
                        'min_limit': min_limit,
                        'max_limit': max_limit
                    }
                )

    return scalar_values


def get_disqualified_jobs_data_in_study_with_id_due_to_scalar_limit_violations(
    scalar_data: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Get disqualified jobs in a study due to scalar limit violations.

    Args:
        scalar_data: Dictionary of scalar data for each job in the study
        """
    
    # Create a dictionary to hold the disqualified jobs
    disqualified_jobs: Dict[str, List[Dict[str, Any]]] = {}

    # Loop through the scalar data and check for limit violations
    for job_id, scalars in scalar_data.items():
        # Loop through the scalars and check for limit violations
        for scalar in scalars:
            # Check if the scalar value is outside the limits
            if 'min_limit' in scalar or 'max_limit' in scalar:
                if scalar['scalar_value'] < scalar['min_limit'] or scalar['scalar_value'] > scalar['max_limit']:
                    #If job_id is not in the disqualified jobs dict, add it
                    if job_id not in disqualified_jobs:
                        disqualified_jobs[job_id] = []
                    # Add the scalar to the disqualified jobs dict
                    disqualified_jobs[job_id].append(
                        {
                            'scalar': scalar['scalar'],
                            'scalar_value': scalar['scalar_value'],
                            'min_limit': scalar.get('min_limit', None),
                            'max_limit': scalar.get('max_limit', None)
                        }
                    )

    return disqualified_jobs
    

async def get_job_with_minimum_scalar_value(
        session: Session,
        study_id: str,
        sim_type: str,
        scalar_name: str,
        excluded_job_ids: List[str] = []
):
    """Get the job with the minimum scalar value in a study.

    Args:
        session: Authenticated Canopy session object
        study_id: ID of the study to analyse
        sim_type: Simulation type to check
        scalar_name: Name of the scalar to check
        exclude_job_ids: List of job IDs to exclude from the search

    Returns:
        JobResult object with the minimum scalar value
    """
    
    # Load the study with scalar data
    study = await canopy.load_study(
        session=session, 
        study_id=study_id,
        sim_type=sim_type,
        include_study_full_document=True, 
        include_job_metadata=True, 
        include_job_scalar_results=True, 
        include_job_vector_metadata=True
    )

    # Initialize variables to track the minimum scalar value and corresponding job
    min_scalar_value = float('inf')
    min_scalar_job_id = None

    # Loop through the jobs and get the scalar values
    for job in study.jobs:
        # Skip excluded jobs
        if job.document.document_id not in excluded_job_ids:
            print(f"Checking job {job.document.document_id} for scalar {scalar_name}...")
            # Check if the scalar exists in the job's scalar data
            if scalar_name in job.scalar_data:
                # Get the scalar value
                scalar_value = job.scalar_data.get(scalar_name)
                print(f"Job ID: {job.document.document_id}, Scalar Value: {scalar_value}")
                # Update minimum scalar value and corresponding job if necessary
                if float(scalar_value) < min_scalar_value:
                    min_scalar_value = float(scalar_value)
                    min_scalar_job_id = job.document.document_id
    
    return min_scalar_job_id, min_scalar_value


async def create_monte_carlo_exploration_config_from_swept_parameter_data(
        session: Session,
        name: str,
        sim_version: str,
        swept_parameter_data: List[Dict[str, Any]],
        n_monte_carlo_points: int = 1000
) -> str:
    """Create a Monte Carlo exploration config from swept parameter data.

    Args:
        session: Authenticated Canopy session object
        name: Name of the exploration config
        sim_version: Simulation version
        swept_parameter_data: List of dictionaries containing swept parameter data

    Returns:
        ID of the created exploration config
    """
    # Create the config data from the swept parameter data
    config_data = {
        "design": {
            "name": "Monte Carlo",
            "numberOfPoints": n_monte_carlo_points,
            "ranges": []
        }
    }

    for data in swept_parameter_data:
        # Check if the 'path', 'min' and 'max' keys are present in the data
        if 'path' not in data or 'min' not in data or 'max' not in data:
            raise ValueError(f"Missing required keys in swept parameter data: {data}")
        range_data = {
            "dimensionType": "interpolation",
            "distribution": "uniform",
            "parallelSubRanges": [
                {
                "parameterPath": data['path'],
                "valueType": "absolute",
                "valueStart": data['min'],
                "valueEnd": data['max'],
                "interpolationType": "linear"
                }
            ]
        }
        # Add the range data to the config data
        config_data['design']['ranges'].append(range_data)

    # Create a new exploration config
    exploration_config = await canopy.create_config(
        session=session,
        config_type="exploration",
        name=name,
        config_data=config_data,
        sim_version=sim_version
    )

    return exploration_config