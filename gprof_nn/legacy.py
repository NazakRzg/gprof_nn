"""
===============
gprof_nn.legacy
===============

This module provides an interface to run the legacy GPROF algorithm on
CSU systems.
"""
import logging
import subprocess
from tempfile import TemporaryDirectory
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
import xarray as xr

from gprof_nn.definitions import ALL_TARGETS
from gprof_nn.data.preprocessor import run_preprocessor
from gprof_nn.data.retrieval import RetrievalFile
from gprof_nn.data.training_data import (
    write_preprocessor_file,
    GPROF_NN_1D_Dataset,
    GPROF_NN_3D_Dataset,
)
from gprof_nn.sensors import CrossTrackScanner, ConicalScanner


LOGGER = logging.getLogger(__name__)


EXECUTABLES = {
    "STANDARD": "GPROF_2021_V1",
    "SENSITIVITY": "GPROF_2020_V1_grads",
    "PROFILES": "GPROF_2021_V1_profs",
}


EXECUTABLES_X = {
    "STANDARD": "GPROF_2020_V1x",
    "PROFILES": "GPROF_2020_V1x_profiles"
}


ANCILLARY_DATA = "/qdata1/pbrown/gpm/ancillary/"


SENSITIVITY_HEADER = """
==============================================
CHANNEL SENSITIVITY FILE GENERATED BY GPROF-NN
==============================================







Channel Sensitivity for GMI
Sfccode    10V    10H    19V    19H    22V    22H    37V    37H    89V    89H   150V   150H  183/1  183/3  183/7
----------------------------------------------------------------------------------------------------------------
"""


DEFAULT_SENSITIVITIES = np.load(
    Path(__file__).parent / "files" / "gmi_era5_sensitivities.npy"
)


def has_gprof():
    """
    Determine whether the legacy GPROF algorithm is available
    on the system.
    """
    return shutil.which(EXECUTABLES["STANDARD"]) is not None


def load_sensitivities(sensor, configuration):
    """
    Load channel sensitivities for a given sensor and configutation.

    Args:
        sensor: Sensor object representing the sensor for which to load
            the channel sensitivities.
        configuration: The configuration for which to load the channel
            sensitivities.

    Return:
        'numpy.ndarray' containing the channel sensitivities.
    """
    data_path = Path(__file__).parent / "files"
    filename = f"{sensor.name.lower()}_{configuration.lower()}" "_sensitivities.npy"
    sensitivities = np.load(data_path / filename)
    return sensitivities


def write_sensitivity_file(sensor, filename, nedts):
    """
    Write sensitivity file for GPROF algorithm.
    """
    formats = ["%3d"] + sensor.n_chans * ["%6.2f"]
    np.savetxt(filename, nedts, fmt=formats, header=SENSITIVITY_HEADER)


def execute_gprof(
    working_directory,
    sensor,
    configuration,
    input_file,
    mode="Standard",
    profiles=False,
    nedts=None,
    robust=False,
):
    """
    Execute legacy GPROF algorithm.

    Args:
        working_directory: The folder to use to store temporary files and
            execute the retrieval.
        sensor: The sensor from which the input data originates.
        configuration: Which configuration of the retrieval to run ('ERA5'
            or 'GANAL')
        mode: Whether to include gradients or profiles in the
            retrieval.
        profiles: Whether profiles should be retrieved.
        nedts: Array containing sensitivities for all channels.
        robust: Whether to raise errors encountered during execution.

    Return:
        'xarray.Dataset' containing the retrieval results.
    """
    # Determine the right executable.
    if isinstance(sensor, ConicalScanner):
        executables = EXECUTABLES
    elif isinstance(sensor, CrossTrackScanner):
        executables = EXECUTABLES_X
    else:
        raise ValueError("The provided sensor class is not supported.")
    if not mode.upper() in executables:
        raise ValueError(
            "'mode' must be one of 'STANDARD', 'SENSITIVITY' or 'PROFILES'"
        )
    executable = executables[mode]

    working_directory = Path(working_directory)
    output_file = working_directory / "output.bin"
    log_file = working_directory / "log"

    sensitivity_file = working_directory / "channel_sensitivities.txt"
    if nedts is None:
        nedts = load_sensitivities(sensor, configuration)
    write_sensitivity_file(sensor, sensitivity_file, nedts=nedts)

    if profiles:
        profiles = "1"
    else:
        profiles = "0"

    if mode.upper() == "SENSITIVITY":
        has_sensitivity = True
        has_profiles = False
    elif mode.upper() == "PROFILES":
        has_sensitivity = False
        has_profiles = True
        profiles = "1"
    else:
        has_sensitivity = False
        has_profiles = False

    args = [
        executable,
        str(input_file),
        str(output_file),
        str(log_file),
        ANCILLARY_DATA,
        profiles,
    ]
    try:
        subprocess.run(args, check=True, capture_output=True, cwd=working_directory)
    except subprocess.CalledProcessError as error:
        if robust:
            with open(log_file, "r") as log:
                log = log.read()
            LOGGER.error(
                "Running GPROF failed with the following log: %s\n%s\n%s",
                log,
                error.stdout,
                error.stderr,
            )
            return None
        raise error
    results = RetrievalFile(
        output_file, has_profiles=has_profiles, has_sensitivity=has_sensitivity
    )
    return results.to_xarray_dataset()


def run_gprof_training_data(
    sensor,
    configuration,
    input_file,
    mode,
    profiles,
    nedts=None,
    preserve_structure=False,
):
    """
    Runs GPROF algorithm on training data in GPROF-NN format and includes
    truth values in the results.

    Args:
        sensor: The sensor for which to run GPROF.
        configuration: The configuration with which to run GPROF ('ERA5'
             or 'GANAL'.
        input_file: Path to the NetCDF file containing the validation
            data.
        mode: The mode in which to run GPROF ('STANDARD', 'SENSITIVITY'
            or 'PROFILES')
        profiles: Whether to retrieve profiles.
        nedts: If provided should be an array containing the channel
            sensitivities to use for the retrieval.

    Return:
        'xarray.Dataset' containing the retrieval results.
    """
    targets = ALL_TARGETS + ["latitude", "longitude"]
    if preserve_structure:
        input_data = GPROF_NN_3D_Dataset(
            input_file,
            shuffle=False,
            normalize=False,
            augment=False,
            targets=targets,
            sensor=sensor,
            batch_size=16
        )
    else:
        input_data = GPROF_NN_1D_Dataset(
            input_file,
            shuffle=False,
            normalize=False,
            augment=False,
            targets=targets,
            sensor=sensor,
            batch_size=256 * 2048,
        )

    results = []
    with TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        for batch in input_data:

            preprocessor_file = tmp / "input.pp"
            batch_input = input_data.to_xarray_dataset(batch=batch)
            new_dataset = write_preprocessor_file(batch_input, preprocessor_file)
            if new_dataset is not None:
                batch_input = new_dataset

            output_data = execute_gprof(
                tmp,
                sensor,
                configuration,
                preprocessor_file,
                mode=mode,
                profiles=profiles,
                nedts=nedts,
                robust=True,
            )

            if output_data is None:
                continue


            if not preserve_structure:
                if "scans" in batch_input.dims:
                    batch_input = batch_input.stack({"samples": ("scans", "pixels")})
                output_data = output_data.stack({"samples": ("scans", "pixels")})
                n_samples = output_data.samples.size
                batch_input = batch_input[{"samples": slice(0, n_samples)}]
            else:
                scans = batch_input.scans.data
                pixels = batch_input.pixels.data
                samples = np.arange(output_data.scans.size // scans.size)
                index = pd.MultiIndex.from_product(
                    (samples, scans),
                    names=('samples', 'new_scans')
                )
                output_data = output_data.assign(scans=index).unstack("scans")
                output_data = output_data.rename({"new_scans": "scans"})

            for k in ALL_TARGETS:
                if k in output_data.variables:
                    output_data[k + "_true"] = batch_input[k]

            results += [output_data]

    if not results:
        return None

    results = xr.concat(results, dim="samples")
    results = results.reset_index("samples")
    return results


def run_gprof_standard(sensor, configuration, input_file, mode, profiles, nedts=None):
    """
    Runs GPROF algorithm on input from preprocessor.

    Args:
        sensor: The sensor for which to run GPROF.
        configuration: The configuration with which to run GPROF ('ERA5'
            or 'GANAL')
        input_file: Path to the NetCDF file containing the validation
            data.
        mode: The mode in which to run GPROF ('STANDARD', 'SENSITIVITY'
            or 'PROFILES')
        profiles: Whether to retrieve profiles.
        nedts: If provided should be an array containing the channel
            sensitivities to use for the retrieval.

    Return:
        'xarray.Dataset' containing the retrieval results.
    """
    with TemporaryDirectory() as tmp:

        if Path(input_file).suffix == ".HDF5":
            output_file = Path(tmp) / "input.pp"
            run_preprocessor(input_file, sensor, output_file=output_file, robust=False)
            input_file = output_file

        results = execute_gprof(
            tmp, sensor, configuration, input_file,
            mode=mode, profiles=profiles, nedts=nedts, robust=True
        )
    return results
