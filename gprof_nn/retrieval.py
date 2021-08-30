"""
==================
gprof_nn.retrieval
==================

This module contains classes and functionality that drive the execution
of the retrieval.
"""
import logging
import math
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import xarray as xr

import torch
from torch import nn
import pandas as pd

from gprof_nn import sensors
import gprof_nn.logging
from gprof_nn.definitions import PROFILE_NAMES, ALL_TARGETS
from gprof_nn.data.training_data import GPROF_NN_0D_Dataset
from gprof_nn.data.preprocessor import PreprocessorFile, run_preprocessor


LOGGER = logging.getLogger(__name__)


###############################################################################
# Helper functions.
###############################################################################


def calculate_padding_dimensions(t):
    """
    Calculate list of PyTorch padding values to extend second-to-last and last
    dimensions of the tensor to multiples of 32.

    Args:
        t: The ``torch.Tensor`` to pad.

    Return
        A tuple ``(p_l_n, p_r_n, p_l_m, p_r_m)`` containing the
        left and right padding  for the second to last dimension
        (``p_l_m, p_r_m``) and for the last dimension (``p_l_n, p_r_n``).
    """
    shape = t.shape

    n = shape[-1]
    d_n = math.ceil(n / 32) * 32 - n
    p_l_n = d_n // 2
    p_r_n = d_n - p_l_n

    m = shape[-2]
    d_m = math.ceil(m / 32) * 32 - m
    p_l_m = d_m // 2
    p_r_m = d_m - p_l_m

    return (p_l_n, p_r_n, p_l_m, p_r_m)


def combine_input_data_2d(dataset, v_tbs="brightness_temperatures"):
    """
    Combine retrieval input data into input tensor format for convolutional
    retrieval.

    Args:
        dataset: ``xarray.Dataset`` containing the input variables.
        v_tbs: Name of the variable to load the brightness temperatures
            from.

    Return:
        Rank-4 input tensor containing the input data with features oriented
        along the first axis.
    """
    bts = dataset[v_tbs][:].data
    invalid = (bts > 500.0) + (bts < 0.0)
    bts[invalid] = np.nan
    # 2m temperature
    t2m = dataset["two_meter_temperature"][:].data[..., np.newaxis]
    # Total precipitable water.
    tcwv = dataset["total_column_water_vapor"][:].data[..., np.newaxis]
    # Surface type
    st = dataset["surface_type"][:].data
    n_types = 18
    shape = bts.shape[:-1]
    st_1h = np.zeros(shape + (n_types,), dtype=np.float32)
    for i in range(n_types):
        indices = st == (i + 1)
        st_1h[indices, i] = 1.0
    # Airmass type
    # Airmass type is defined slightly different from surface type in
    # that there is a 0 type.
    am = dataset["airmass_type"][:].data
    n_types = 4
    am_1h = np.zeros(shape + (n_types,), dtype=np.float32)
    for i in range(n_types):
        indices = am == i
        am_1h[indices, i] = 1.0
    am_1h[am < 0, 0] = 1.0

    input_data = np.concatenate([bts, t2m, tcwv, st_1h, am_1h], axis=-1)
    input_data = input_data.astype(np.float32)
    if input_data.ndim < 4:
        input_data = np.expand_dims(input_data, 0)
    input_data = np.transpose(input_data, (0, 3, 1, 2))
    return input_data


###############################################################################
# Retrieval driver
###############################################################################


GPROF_BINARY = "GPROF_BINARY"
L1C = "L1C"
NETCDF = "NETCDF"


class RetrievalDriver:
    """
    The ``RetrievalDriver`` class implements the logic for running the GPROF-NN
    retrieval using different neural network models and writing output to
    different formats.
    """

    N_CHANNELS = 15

    def __init__(
            self,
            input_file,
            normalizer,
            model,
            ancillary_data=None,
            output_file=None
    ):
        """
        Create retrieval driver.

        Args:
            input_file: Path to the preprocessor or NetCDF file containing the
                input data for the retrieval.
            normalizer_file: The normalizer object to use to normalize the
                input data.
            model: The neural network to use for the retrieval.
        """
        from gprof_nn.data.preprocessor import PreprocessorFile

        input_file = Path(input_file)
        self.input_file = input_file
        self.normalizer = normalizer
        self.model = model
        self.ancillary_data = ancillary_data

        # Determine input format.
        suffix = input_file.suffix
        if suffix.endswith("pp"):
            self.input_format = GPROF_BINARY
        elif suffix.endswith("HDF5"):
            self.input_format = L1C
        else:
            self.input_format = NETCDF

        # Determine output format.
        if output_file is None:
            self.output_format = self.input_format
        else:
            if self.input_format in [NETCDF, L1C]:
                self.output_format = NETCDF
            else:
                output_file = Path(output_file)
                if suffix.lower().endswith("bin"):
                    self.output_format = GPROF_BINARY
                else:
                    self.output_format = NETCDF

        output_suffix = ".BIN"
        if self.output_format == NETCDF:
            output_suffix = ".nc"

        # Determine output filename.
        if output_file is None:
            self.output_file = Path(self.input_file.name.replace(suffix, output_suffix))
        elif Path(output_file).is_dir():
            self.output_file = Path(output_file) / self.input_file.name.replace(
                suffix, output_suffix
            )
        else:
            self.output_file = output_file

    def _load_input_data(self):
        """
        Load retrieval input data.

        Return:
            If the input data was successfully loaded the input data
            object is returned. ``None`` otherwise.
        """
        # Load input data.
        if self.input_format == GPROF_BINARY:
            LOGGER.info("Loading preprocessor input data from %s.", self.input_file)
            input_data = self.model.preprocessor_class(self.input_file, self.normalizer)
        elif self.input_format == L1C:
            sensor = getattr(self.model, "sensor", None)
            if sensor is None:
                sensor = sensors.GMI
            _, file = tempfile.mkstemp()
            try:
                LOGGER.info("Running preprocessor for input file %s.", self.input_file)
                run_preprocessor(
                    self.input_file, sensor, output_file=file, robust=False
                )
                input_data = self.model.preprocessor_class(file, self.normalizer)
            except subprocess.CalledProcessError:
                LOGGER.warning(
                    "Running the preprocessor failed. Skipping file %s.",
                    self.input_file,
                )
                return None
            finally:
                Path(file).unlink()
        else:
            input_data = self.model.netcdf_class(
                self.input_file, normalizer=self.normalizer
            )
        return input_data

    def _run(self, xrnn, input_data):
        """
        Batch-wise processing of retrieval input.

        Args:
            xrnn: A quantnn neural-network model.
            input_data: Iterable ``input_data`` object providing access to
                batched input data.

        Return:
            A dataset containing the concatenated retrieval results for all
            batches.
        """
        means = {}
        precip_1st_tercile = []
        precip_3rd_tercile = []
        pop = []

        with torch.no_grad():
            device = next(iter(xrnn.model.parameters())).device
            for i in range(len(input_data)):
                x = input_data[i]
                x = x.float().to(device)
                y_pred = xrnn.predict(x)
                if not isinstance(y_pred, dict):
                    y_pred = {"surface_precip": y_pred}

                y_mean = xrnn.posterior_mean(y_pred=y_pred)
                for k, y in y_pred.items():
                    means.setdefault(k, []).append(y_mean[k].cpu())
                    if k == "surface_precip":
                        t = xrnn.posterior_quantiles(
                            y_pred=y, quantiles=[0.333, 0.667], key=k
                        )
                        precip_1st_tercile.append(t[:, 0].cpu())
                        precip_3rd_tercile.append(t[:, 1].cpu())
                        p = xrnn.probability_larger_than(y_pred=y, y=1e-4, key=k)
                        pop.append(p.cpu())

        data = {}
        for k in means:
            y = np.concatenate([t.numpy() for t in means[k]])
            data[k] = (input_data.dimensions[k], y)

        if len(precip_1st_tercile) > 0:
            dims = input_data.dimensions["surface_precip"]
            data["precip_1st_tercile"] = (
                dims,
                np.concatenate([t.numpy() for t in precip_1st_tercile]),
            )
            data["precip_3rd_tercile"] = (
                dims,
                np.concatenate([t.numpy() for t in precip_3rd_tercile]),
            )
            pop = np.concatenate([t.numpy() for t in pop])
            data["pop"] = (dims, pop)
            data["most_likely_precip"] = data["surface_precip"]
            data["precip_flag"] = (dims, pop > 0.5)
        data = xr.Dataset(data)

        return input_data.finalize(data)

    def run(self):
        """
        Run retrieval and store results to file.

        Return:
            Name of the output file that the results have been written
            to.
        """
        input_data = self._load_input_data()
        if input_data is None:
            return None
        results = self._run(self.model, input_data)

        # Make sure output folder exists.
        folder = Path(self.output_file).parent
        folder.mkdir(parents=True, exist_ok=True)

        if self.output_format == GPROF_BINARY:
            return input_data.write_retrieval_results(
                self.output_file.parent, results, ancillary_data=self.ancillary_data
            )
        else:
            # Include some inputs in result.
            if hasattr(input_data, "data"):
                vars = [
                    "latitude",
                    "longitude",
                    "total_column_water_vapor",
                    "two_meter_temperature",
                    "surface_type",
                    "airmass_type",
                ]
                for var in vars:
                    results[var] = input_data.data[var]
            results.to_netcdf(self.output_file)
        return self.output_file


class RetrievalGradientDriver(RetrievalDriver):
    """
    Speccialization of ``RetrievalDriver`` that retrieves only surface precipitation
    and its gradients with respect to the input variables.
    """

    N_CHANNELS = 15

    def __init__(
        self, input_file, normalizer, model, ancillary_data=None, output_file=None
    ):
        """
        Create retrieval driver.

        Args:
            input_file: Path to the preprocessor or NetCDF file containing the
                input data for the retrieval.
            normalizer_file: The normalizer object to use to normalize the
                input data.
            model: The neural network to use for the retrieval.
        """
        super().__init__(
            input_file,
            normalizer,
            model,
            ancillary_data=ancillary_data,
            output_file=output_file,
        )

    def _run(self, xrnn, input_data):
        """
        Batch-wise processing of retrieval input.

        Args:
            xrnn: A quantnn neural-network model.
            input_data: Iterable ``input_data`` object providing access to
                batched input data.

        Return:
            A dataset containing the concatenated retrieval results for all
            batches.
        """
        means = {}
        gradients = {}
        precip_1st_tercile = []
        precip_3rd_tercile = []
        pop = []

        device = next(iter(xrnn.model.parameters())).device
        for i in range(len(input_data)):
            x = input_data[i]
            x = x.float().to(device)
            x.requires_grad = True
            y_pred = xrnn.predict(x)
            if not isinstance(y_pred, dict):
                y_pred = {"surface_precip": y_pred}

            y_mean = xrnn.posterior_mean(y_pred=y_pred)
            grads = {}
            for k in y_pred:
                if k not in PROFILE_NAMES:
                    xrnn.model.zero_grad()
                    y_mean[k].backward(torch.ones_like(y_mean[k]),
                                       retain_graph=True)
                    grads[k] = x.grad

            for k, y in y_pred.items():
                means.setdefault(k, []).append(y_mean[k].cpu())
                if k in grads:
                    gradients.setdefault(k, []).append(grads[k].cpu())

        dims = input_data.scalar_dimensions
        dims_p = input_data.profile_dimensions

        data = {}
        for k in means:
            y = np.concatenate([t.detach().numpy() for t in means[k]])
            if k in PROFILE_NAMES:
                data[k] = (dims_p, y)
            else:
                data[k] = (dims, y)
        for k in gradients:
            y = np.concatenate([t.numpy() for t in gradients[k]])
            if k in PROFILE_NAMES:
                data[k + "_grad"] = (dims_p + ("inputs",), y)
            else:
                data[k + "_grad"] = (dims + ("inputs",), y)
        data = xr.Dataset(data)
        return input_data.finalize(data)


###############################################################################
# Netcdf Format
###############################################################################


class NetcdfLoader:
    """
    Base class for netcdf loader object that implements generic
    element access.
    """

    def __init__(self):
        """
        Create NetcdfLoader.
        """
        self.n_samples = 0
        self.batch_size = 1

    def __len__(self):
        """
        The number of batches in the dataset.
        """
        return math.ceil(self.n_samples / self.batch_size)

    def __getitem__(self, i):
        """
        Return batch of input data.

        Args:
            The batch index.

        Return:
            PyTorch tensor containing the batch of input data.
        """
        i_start = i * self.batch_size
        i_end = i_start + self.batch_size
        x = torch.tensor(self.input_data[i_start:i_end])
        return x


class NetcdfLoader0D(NetcdfLoader):
    """
    Data loader for running the GPROF-NN 0D retrieval on input data
    in NetCDF data format.
    """

    def __init__(self, filename, normalizer, batch_size=16 * 1024):
        """
        Create loader for input data in NetCDF format that provides input
        data for the GPROF-NN 0D retrieval.

        Args:
            filename: The name of the NetCDF file containing the input data.
            normalizer: The normalizer object to use to normalize the input
                data.
            batch_size: How many observations to combine into a single
                input batch.
        """
        super().__init__()
        self.filename = filename
        self.normalizer = normalizer
        self.batch_size = batch_size

        input_data = xr.open_dataset(self.filename)
        sensor = input_data.attrs["sensor"]
        sensor = getattr(sensors, sensor)
        self.sensor = sensor

        if "scans" in input_data.dims:
            self.kind = "standard"
        else:
            self.kind = "bin"

        self.input_data = sensor.load_data_0d(self.filename)
        self.n_samples = self.input_data.shape[0]

        self.scalar_dimensions = ("samples",)
        self.profile_dimensions = ("samples", "layers")
        self.dimensions = {
            t: ("samples", "layers") if t in PROFILE_NAMES else ("samples")
            for t in ALL_TARGETS
        }

    def _load_data(self):
        """
        Load data from training data NetCDF format into the
        'input_data' attribute of the object.
        """
        dataset = xr.open_dataset(self.filename)

        #
        # Load data into input vector
        #

        bts = dataset["brightness_temperatures"][:].data
        invalid = (bts > 500.0) + (bts < 0.0)
        bts[invalid] = np.nan
        # 2m temperature
        t2m = dataset["two_meter_temperature"][:].data[..., np.newaxis]
        # Total precipitable water.
        tcwv = dataset["total_column_water_vapor"][:].data[..., np.newaxis]
        # Surface type
        st = dataset["surface_type"][:].data
        n_types = 18
        shape = bts.shape[:3]
        st_1h = np.zeros(shape + (n_types,), dtype=np.float32)
        for i in range(n_types):
            indices = st == (i + 1)
            st_1h[indices, i] = 1.0
        # Airmass type
        # Airmass type is defined slightly different from surface type in
        # that there is a 0 type.
        am = dataset["airmass_type"][:].data
        n_types = 4
        am_1h = np.zeros(shape + (n_types,), dtype=np.float32)
        for i in range(n_types):
            indices = am == i
            am_1h[indices, i] = 1.0
        am_1h[am < 0, 0] = 1.0

        input_data = np.concatenate([bts, t2m, tcwv, st_1h, am_1h], axis=-1)
        input_data = input_data.astype(np.float32)

        if input_data.ndim > 2:
            self.kind = "standard"
        else:
            self.kind = "bin"

        input_data = self.normalizer(input_data.reshape(-1, 39))
        self.input_data = input_data

    def finalize(self, data):
        """
        Reshape retrieval results into shape of input data.
        """
        if self.kind == "standard":

            invalid = np.all(self.input_data[:, : self.sensor.n_chans] <= -1.5, axis=-1)
            for v in ALL_TARGETS:
                data[v].data[invalid] = np.nan

            samples = np.arange(data.samples.size // (221 * 221))
            scans = np.arange(221)
            pixels = np.arange(221)
            names = ("samples_t", "scans", "pixels")
            index = pd.MultiIndex.from_product((samples, scans, pixels), names=names)
            data = data.assign(samples=index).unstack("samples")
            data = data.rename_dims({"samples_t": "samples"})

            input_data = xr.open_dataset(self.filename)
            vars = [
                "latitude",
                "longitude",
                "total_column_water_vapor",
                "two_meter_temperature"
            ]
            for var in vars:
                data[var] = input_data[var]

        return data


class NetcdfLoader2D(NetcdfLoader):
    """
    Data loader for running the GPROF-NN 2D retrieval on input data
    in NetCDF data format.
    """

    def __init__(self, filename, normalizer, batch_size=8):
        super().__init__()
        self.filename = filename
        self.normalizer = normalizer
        self.batch_size = batch_size

        self._load_data()
        self.n_samples = self.input_data.shape[0]

        self.scalar_dimensions = ("samples", "scans", "pixels")
        self.profile_dimensions = ("samples", "layers", "scans", "pixels")
        self.dimensions = {
            t: ("samples", "layers", "scans", "pixels")
            if t in PROFILE_NAMES
            else ("samples", "scans", "pixels")
            for t in ALL_TARGETS
        }

    def _load_data(self):
        """
        Load data from training data NetCDF format into 'input' data
        attribute.
        """
        dataset = xr.open_dataset(self.filename)
        input_data = combine_input_data_2d(dataset)
        self.input_data = input_data
        self.padding = calculate_padding_dimensions(input_data)

    def __getitem__(self, i):
        """
        Return batch of input data.

        Args:
            The batch index.

        Return:
            PyTorch tensor containing the batch of input data.
        """
        x = super().__getitem__(i)
        return torch.nn.functional.pad(x, self.padding, mode="replicate")

    def finalize(self, data):
        """
        Reshape retrieval results into shape of input data.
        """
        data = data[
            {
                "scans": slice(self.padding[0], -self.padding[1]),
                "pixels": slice(self.padding[2], -self.padding[3]),
            }
        ]
        if "layers"  in data.dims:
            dims = ["samples", "scans", "pixels", "layers"]
            data = data.transpose(*dims)
        return data


###############################################################################
# Preprocessor format
###############################################################################


class PreprocessorLoader0D:
    """
    Interface class to run the GPROF-NN retrieval on preprocessor files.
    """

    def __init__(self, filename, normalizer, batch_size=1024 * 8):
        """
        Create preprocessor loader.

        Args:
            filename: Path to the preprocessor file from which to load the
                input data.
            normalizer: The normalizer object to use to normalize the input
                data.
            scans_per_batch: How scans should be combined into a single
                batch.
        """
        self.filename = filename
        preprocessor_file = PreprocessorFile(filename)
        self.sensor = preprocessor_file.sensor
        self.data = preprocessor_file.to_xarray_dataset()
        self.normalizer = normalizer
        self.n_scans = self.data.scans.size
        self.n_pixels = self.data.pixels.size
        self.scans_per_batch = batch_size // self.n_pixels

        self.scalar_dimensions = ("samples",)
        self.profile_dimensions = ("samples", "layers")
        self.dimensions = {
            t: ("samples", "layers") if t in PROFILE_NAMES else ("samples",)
            for t in ALL_TARGETS
        }

    def __len__(self):
        """
        The number of batches in the preprocessor file.
        """
        return math.ceil(self.n_scans / self.scans_per_batch)

    def __getitem__(self, i):
        """
        Return batch of retrieval inputs as PyTorch tensor.

        Args:
            i: The index of the batch.

        Return:
            PyTorch Tensor ``x`` containing the normalized inputs.
        """
        n_chans = self.sensor.n_chans
        n_inputs = self.sensor.n_inputs

        i_start = i * self.scans_per_batch
        i_end = min(i_start + self.scans_per_batch, self.n_scans)

        n = (i_end - i_start) * self.data.pixels.size
        x = np.zeros((n, n_inputs), dtype=np.float32)

        tbs = self.data["brightness_temperatures"].data[i_start:i_end]
        tbs = tbs.reshape(-1, n_chans)
        t2m = self.data["two_meter_temperature"].data[i_start:i_end]
        t2m = t2m.reshape(-1)
        tcwv = self.data["total_column_water_vapor"].data[i_start:i_end]
        tcwv = tcwv.reshape(-1)
        st = self.data["surface_type"].data[i_start:i_end]
        st = st.reshape(-1)
        at = np.maximum(self.data["airmass_type"].data[i_start:i_end], 0.0)
        at = at.reshape(-1)

        x[:, :n_chans] = tbs
        x[:, :n_chans][x[:, :n_chans] < 0] = np.nan

        i_anc = n_chans
        if isinstance(self.sensor, sensors.CrossTrackScanner):
            va = self.data["earth_incidence_angle"].data[i_start:i_end]
            x[:, n_chans] = va.ravel()
            i_anc = n_chans + 1

        x[:, i_anc] = t2m
        x[:, i_anc][t2m < 0] = np.nan

        x[:, i_anc + 1] = tcwv
        x[:, i_anc + 1][tcwv < 0] = np.nan

        for j in range(18):
            x[:, i_anc + 2 + j][st == j + 1] = 1.0

        for j in range(4):
            x[:, i_anc + 2 + 18 + j][at == j] = 1.0

        x = self.normalizer(x)
        return torch.tensor(x)

    def finalize(self, data):
        """
        Transform retrieval results into format of input data. Recreates
        the scan and pixel dimensions that are lost due to the batch-wise
        processing of the retrieval.

        Args:
            data: 'xarray.Dataset' containing the retrieval results
                produced by evaluating the GPROF-NN model on the input
                data from the loader.

        Return:
            The data in 'data' modified to match the format of the input
            data.
        """
        scans = np.arange(data.samples.size // self.n_pixels)
        pixels = np.arange(self.n_pixels)
        names = ("scans", "pixels")
        index = pd.MultiIndex.from_product((scans, pixels), names=names)
        data = data.assign(samples=index).unstack("samples")
        if "layers"  in data.dims:
            dims = ["scans", "pixels", "layers"]
            data = data.transpose(*dims)

        tbs = self.data["brightness_temperatures"].data
        invalid = np.all(tbs < 0, axis=-1)
        for v in ALL_TARGETS:
            if v in data.variables:
                data[v].data[invalid] = np.nan

        return data

    def write_retrieval_results(self, output_path, results, ancillary_data=None):
        """
        Write retrieval results to file.

        Args:
            output_path: The folder to which to write the output.
            results: ``xarray.Dataset`` containing the retrieval results.
            ancillary_data: The folder containing the profile clusters.

        Return:
            The filename of the retrieval output file.
        """
        preprocessor_file = PreprocessorFile(self.filename)
        return preprocessor_file.write_retrieval_results(
            output_path, results, ancillary_data=ancillary_data
        )


class PreprocessorLoader2D:
    """
    Interface class to run the GPROF-NN 2D retrieval on preprocessor files.
    """

    def __init__(self, filename, normalizer):
        """
        Create preprocessor loader.

        Args:
            filename: Path to the preprocessor file from which to load the
                input data.
            normalizer: The normalizer object to use to normalize the input
                data.
        """
        self.filename = filename
        self.normalizer = normalizer

        preprocessor_file = PreprocessorFile(filename)
        self.data = preprocessor_file.to_xarray_dataset()
        self.n_scans = self.data.scans.size
        self.n_pixels = self.data.pixels.size

        input_data = combine_input_data_2d(self.data)
        self.input_data = self.normalizer(input_data)
        self.padding = calculate_padding_dimensions(input_data)

        self.scalar_dimensions = ("samples", "scans", "pixels")
        self.profile_dimensions = ("samples", "layers", "scans", "pixels")
        self.dimensions = {
            t: ("samples", "layers", "scans", "pixels")
            if t in PROFILE_NAMES
            else ("samples", "scans", "pixels")
            for t in ALL_TARGETS
        }

    def __len__(self):
        """
        The number of batches in the preprocessor file.
        """
        return 1

    def __getitem__(self, i):
        """
        Return batch of retrieval inputs as PyTorch tensor.

        Args:
            i: The index of the batch.

        Return:
            PyTorch Tensor ``x`` containing the normalized inputs.
        """
        x = torch.tensor(self.input_data)
        return nn.functional.pad(x, self.padding, mode="replicate")

    def finalize(self, data):
        """
        Reshape retrieval results into shape of input data.
        """
        data = data[
            {
                "samples": 0,
                "scans": slice(self.padding[2], -self.padding[3]),
                "pixels": slice(self.padding[0], -self.padding[1]),
            }
        ]
        if "layers"  in data.dims:
            dims = ["scans", "pixels", "layers"]
            data = data.transpose(*dims)
        return data

    def write_retrieval_results(self, output_path, results, ancillary_data=None):
        """
        Write retrieval results to file.

        Args:
            output_path: The folder to which to write the output.
            results: ``xarray.Dataset`` containing the retrieval results.
            ancillary_data: The folder containing the profile clusters.

        Return:
            The filename of the retrieval output file.
        """
        preprocessor_file = PreprocessorFile(self.filename)
        return preprocessor_file.write_retrieval_results(
            output_path, results, ancillary_data=ancillary_data
        )


###############################################################################
# Simulator format
###############################################################################


class SimulatorLoader(NetcdfLoader):
    """
    Interface class to augment training data with simulated Tbs.
    """

    def __init__(self, filename, normalizer, batch_size=8):
        super().__init__()
        self.filename = filename
        self.normalizer = normalizer
        self.batch_size = batch_size

        sensor_name = xr.open_dataset(filename).attrs["sensor"]
        sensor = getattr(sensors, sensor_name, None)
        if sensor is None:
            raise ValueError(f"Sensor {sensor_name} isn't yet supported.")
        self.sensor = sensor

        self._load_data()
        self.n_samples = self.input_data.shape[0]

        if sensor.n_angles > 1:
            dims_tbs = ("samples", "angles", "channels", "scans", "pixels")
            dims_bias = ("samples", "channels", "scans", "pixels")
            self.dimensions = {
                "simulated_brightness_temperatures": dims_tbs,
                "brightness_temperature_biases": dims_bias,
            }
        else:
            dims = ("samples", "channels", "scans", "pixels")
            self.dimensions = {
                "simulated_brightness_temperatures": dims,
                "brightness_temperature_biases": dims,
            }

    def _load_data(self):
        """
        Load data from training data NetCDF format into 'input' data
        attribute.
        """
        dataset = xr.open_dataset(self.filename)
        dataset = dataset[{"samples": dataset.source == 0}]
        if self.sensor == sensors.GMI:
            input_data = combine_input_data_2d(dataset, v_tbs="brightness_temperatures")
        else:
            input_data = combine_input_data_2d(
                dataset, v_tbs="brightness_temperatures_gmi"
            )
        self.input_data = self.normalizer(input_data)
        self.padding = calculate_padding_dimensions(input_data)

    def __getitem__(self, i):
        """
        Return batch of input data.

        Args:
            The batch index.

        Return:
            PyTorch tensor containing the batch of input data.
        """
        x = super().__getitem__(i)
        return torch.nn.functional.pad(x, self.padding, mode="replicate")

    def finalize(self, data):
        """
        Copy predicted Tbs and biases into input file and return.
        """
        data = data[
            {
                "scans": slice(self.padding[0], -self.padding[1]),
                "pixels": slice(self.padding[2], -self.padding[3]),
            }
        ]

        if self.sensor.n_angles > 1:
            data = data.transpose("samples", "scans", "pixels", "angles", "channels")
        else:
            data = data.transpose("samples", "scans", "pixels", "channels")

        input_data = xr.load_dataset(self.filename)

        n_samples = input_data.samples.size
        n_scans = data.scans.size
        n_pixels = data.pixels.size
        n_channels = data.channels.size

        if self.sensor.n_angles > 1:
            dims = ("samples", "scans", "pixels", "angles", "channels")
            n_angles = data.angles.size
            input_data["simulated_brightness_temperatures"] = (
                dims,
                np.nan * np.zeros((n_samples, n_scans, n_pixels, n_angles, n_channels),
                                  dtype=np.float32),
            )
            input_data["brightness_temperature_biases"] = (
                ("samples", "scans", "pixels", "channels"),
                np.nan * np.zeros((n_samples, n_scans, n_pixels, n_channels),
                                  dtype=np.float32),
            )
        else:
            dims = ("samples", "scans", "pixels", "channels")
            input_data["simulated_brightness_temperatures"] = (
                dims,
                np.nan * np.zeros((n_samples, n_scans, n_pixels, n_channels),
                                  dtype=np.float32),
            )
            input_data["brightness_temperature_biases"] = (
                ("samples", "scans", "pixels", "channels"),
                np.nan * np.zeros((n_samples, n_scans, n_pixels, n_channels),
                                  dtype=np.float32),
            )
        index = 0
        for i in range(n_samples):
            if input_data.source[i] == 0:
                input_data["simulated_brightness_temperatures"][i] = data[
                    "simulated_brightness_temperatures"
                ][index]
                input_data["brightness_temperature_biases"][i] = data[
                    "brightness_temperature_biases"
                ][index]
                index += 1
        return input_data
