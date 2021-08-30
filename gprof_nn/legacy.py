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
import xarray as xr

import gprof_nn.logging
from gprof_nn.definitions import ALL_TARGETS
from gprof_nn.data.preprocessor import PreprocessorFile
from gprof_nn.data.retrieval import RetrievalFile
from gprof_nn.data.training_data import (GPROF_NN_0D_Dataset,
                                         write_preprocessor_file)


LOGGER = logging.getLogger(__name__)


EXECUTABLES = {
    "STANDARD": "GPROF_2020_V1",
    "SENSITIVITY": "GPROF_2020_V1_grads",
    "PROFILES": "GPROF_2020_V1_prof"

}


ANCILLARY_DATA = "/qdata1/pbrown/gpm/ancillary/"


SENSITIVITY_HEADER = \
"""
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
    Determin whether the legacy GPROF algorithm is available
    on the system.
    """
    return shutil.which(EXECUTABLES["STANDARD"]) is not None


def write_sensitivity_file(filename,
                           nedts=None):
    """
    Write sensitivity file for GPROF algorithm.
    """
    if nedts is None:
        nedts = DEFAULT_SENSITIVITIES
    formats = ["%3d"] + 15 * ["%6.2f"]
    np.savetxt(filename, nedts, fmt=formats, header=SENSITIVITY_HEADER)


def execute_gprof(working_directory,
                  input_file,
                  mode,
                  profiles,
                  nedts=None,
                  robust=False):
    """
    Execute legacy GPROF algorithm.

    Args:
        working_directory: The folder to use to store temporary files and
            execute the retrieval.
        mode: Whether to include gradients or profiles in the
            retrieval.
        profiles: Whether profiles should be retrieved.
        nedts: Array containing sensitivities for all channels.
        robust: Whether to raise errors encountered during execution.

    Return:
        'xarray.Dataset' containing the retrieval results.
    """
    if not mode.upper() in EXECUTABLES:
        raise ValueError(
            "'mode' must be one of 'STANDARD', 'SENSITIVITY' or 'PROFILES'"
        )
    executable = EXECUTABLES[mode]
    working_directory = Path(working_directory)
    output_file = working_directory / "output.bin"
    log_file = working_directory / "log"

    sensitivity_file = working_directory / "channel_sensitivities.txt"
    write_sensitivity_file(sensitivity_file, nedts=nedts)

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

    args = [executable,
            str(input_file),
            str(output_file),
            str(log_file),
            ANCILLARY_DATA,
            profiles]
    try:
        subprocess.run(args,
                       check=True,
                       capture_output=True,
                       cwd=working_directory)
    except subprocess.CalledProcessError as error:
        if robust:
            with open(log_file, "r") as log:
                log = log.read()
            LOGGER.error(
                "Running GPROF failed with the following log: %s",
                log
            )
            return None
        else:
            raise error
    results = RetrievalFile(output_file,
                            has_profiles=has_profiles,
                            has_sensitivity=has_sensitivity)
    return results.to_xarray_dataset()


def run_gprof_training_data(input_file,
                            mode,
                            profiles,
                            nedts=None):
    """
    Runs GPROF algorithm on training data in GPROF-NN format and includes
    truth values in the results.

    Args:
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
    input_data = GPROF_NN_0D_Dataset(input_file,
                                     shuffle=False,
                                     normalize=False,
                                     targets=ALL_TARGETS,
                                     batch_size=256 * 2048)

    results = []
    with TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        for batch in input_data:

            preprocessor_file = tmp / "input.pp"
            batch_input = input_data.to_xarray_dataset(batch=batch)
            write_preprocessor_file(batch_input, preprocessor_file)

            output_data = execute_gprof(tmp,
                                        preprocessor_file,
                                        mode,
                                        profiles,
                                        nedts,
                                        robust=True)

            if output_data is None:
                continue

            output_data = output_data.stack({"samples": ("scans", "pixels")})
            n = output_data.samples.size
            batch_input = batch_input[{"samples": slice(0, n)}]
            for k in ALL_TARGETS:
                if k in output_data.variables:
                    output_data[k + "_true"] = batch_input[k]

            results += [output_data]

    if not results:
        return None

    results = xr.concat(results, dim="samples")
    results.reset_index("samples")
    return results


def run_gprof_standard(input_file,
                       mode,
                       profiles,
                       nedts=None):
    """
    Runs GPROF algorithm on input from preprocessor.

    Args:
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
        results = execute_gprof(
            tmp,
            input_file,
            mode,
            profiles,
            nedts,
            robust=True
        )
    return results
