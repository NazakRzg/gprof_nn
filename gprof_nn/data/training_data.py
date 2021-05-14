"""
===========================
gprof_nn.data.training_data
===========================

This module defines the dataset classes that provide access to
the training data for the GPROF-NN retrievals.
"""
from pathlib import Path
import logging

from netCDF4 import Dataset
import numpy as np
import torch
import xarray as xr

import quantnn
from quantnn.normalizer import MinMaxNormalizer, Normalizer
from quantnn.drnn import _to_categorical
from quantnn.utils import apply
import quantnn.quantiles as qq
import quantnn.density as qd

from gprof_nn.data.preprocessor import PreprocessorFile
from gprof_nn.data.bin import PROFILE_NAMES

LOGGER = logging.getLogger(__name__)
_DEVICE = torch.device("cpu")
if torch.cuda.is_available():
    _DEVICE = torch.device("cuda")

ALL_TARGETS = [
    "surface_precip",
    "convective_precip",
    "rain_water_path",
    "ice_water_path",
    "cloud_water_path",
    "total_column_water_vapor",
    "rain_water_content",
    "cloud_water_content",
    "snow_water_content",
    "latent_heat"
]

_THRESHOLDS = {
    "surface_precip": 1e-4,
    "convective_precip": 1e-4,
    "rain_water_path": 1e-4,
    "ice_water_path": 1e-4,
    "cloud_water_path": 1e-4,
    "total_column_water_vapor": 1e0,
    "rain_water_content": 1e-5,
    "cloud_water_content": 1e-5,
    "snow_water_content": 1e-5,
    "latent_heat": -99999,
}


def write_preprocessor_file(input_file,
                            output_file,
                            x=None,
                            n_samples=None,
                            template=None):
    """
    Extract sample from training data file and write to preprocessor format.

    Args:
        input_file: Path to the NetCDF4 file containing the training or test
            data.
        output_file: Path of the file to write the output to.
        n_samples: How many samples to extract from the training data file.
        template: Template preprocessor file use to determine the orbit header
             information. If not provided this data will be filled with dummy
             values.
    """
    data = xr.open_dataset(input_file)
    new_names = {
        "brightness_temps": "brightness_temperatures"
    }
    n_pixels = 2048
    if n_samples is None:
        n_samples = data.samples.size

    if n_samples < n_pixels:
        n_pixels = n_samples

    if n_samples < data.samples.size:
        indices = np.random.permutation(data.samples.size)[:n_samples]
    else:
        indices = slice(0, None)
    data = data[{"samples": indices}].rename(new_names)
    n_scans = data.samples.size // n_pixels
    n_scans += (data.samples.size % n_pixels) > 0

    new_dims = ["scans", "pixels", "channels"]
    new_dataset = {
        "scans": np.arange(n_scans),
        "pixels": np.arange(n_pixels),
        "channels": np.arange(15),
    }
    dims = ("scans", "pixels", "channels")
    shape = ((n_scans, n_pixels, 15))
    for k in data:
        da = data[k]
        n_dims = len(da.dims)
        s = (n_scans, n_pixels) + da.data.shape[1:]
        new_data = np.zeros(s)
        n = da.data.size
        new_data.ravel()[:n] = da.data.ravel()
        new_data.ravel()[n:] = np.nan
        dims = ("scans", "pixels") + da.dims[1:]
        new_dataset[k] = (dims, new_data)

    new_dataset["earth_incidence_angle"] = (
        ("scans", "pixels", "channels"),
        np.broadcast_to(data.attrs["nominal_eia"].reshape(1, 1, -1),
                        (n_scans, n_pixels, 15))
    )

    new_data = xr.Dataset(new_dataset)
    PreprocessorFile.write(output_file, new_data, template=template)



###############################################################################
# Single-pixel observations.
###############################################################################


class GPROF0DDataset:
    """
    Dataset class providing an interface for the single-pixel GPROF
    training dataset mapping TBs and ancillary data to surface precip
    values and other target variables.

    Attributes:
        x: Rank-2 tensor containing the input data with
           samples along first dimension.
        y: The target values
        filename: The filename from which the data is loaded.
        target: The name of the variable used as target variable.
        batch_size: The size of data batches returned by __getitem__ method.
        normalizer: The normalizer used to normalize the data.
        shuffle: Whether or not the ordering of the data is shuffled.
        augment: Whether or not high-frequency observations are randomly set to
            missing to simulate observations at the edge of the swath.
    """

    def __init__(
        self,
        filename,
        target="surface_precip",
        normalize=True,
        transform_zeros=True,
        batch_size=512,
        normalizer=None,
        shuffle=True,
        augment=True,
    ):
        """
        Create GPROF 0D dataset.

        Args:
            filename: Path to the NetCDF file containing the 0D training data
                to load.
            target: String or list of strings specifying the names of the
                variables to use as retrieval targets.
            normalize: Whether or not to normalize the input data.
            transform_zeros: Whether or not to replace very small
                values with random values.
            batch_size: Number of samples in each training batch.
            shuffle: Whether or not to shuffle the training data.
            augment: Whether or not to randomly mask high-frequency channels.
        """
        self.filename = Path(filename)
        self.target = target
        self.transform_zeros = transform_zeros
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.augment = augment
        self._load_data()

        indices_1h = list(range(17, 38))
        if normalizer is None:
            self.normalizer = MinMaxNormalizer(self.x, exclude_indices=indices_1h)
        elif isinstance(normalizer, type):
            self.normalizer = normalizer(self.x, exclude_indices=indices_1h)
        else:
            self.normalizer = normalizer

        self.normalize = normalize
        if normalize:
            self.x = self.normalizer(self.x)

        if transform_zeros:
            self._transform_zeros()

        self.x = self.x.astype(np.float32)
        if isinstance(self.y, dict):
            self.y = {k: self.y[k].astype(np.float32) for k in self.y}
        else:
            self.y = self.y.astype(np.float32)

        self._shuffled = False
        if self.shuffle:
            self._shuffle()

    def __repr__(self):
        return f"GPROF0DDataset({self.filename.name}, n_batches={len(self)})"

    def __str__(self):
        return f"GPROF0DDataset({self.filename.name}, n_batches={len(self)})"

    def _transform_zeros(self):
        """
        Transforms target values that are zero to small, non-zero values.
        """
        if isinstance(self.y, dict):
            for k, y_k in self.y.items():
                threshold = _THRESHOLDS[k]
                indices = (y_k < threshold) * (y_k >= 0.0)
                y_k[indices] = np.random.uniform(
                    threshold * 0.01, threshold, indices.sum()
                )
        else:
            threshold = _THRESHOLDS[self.target]
            y = self.y
            indices = (y < threshold) * (y >= 0.0)
            y[indices] = np.random.uniform(threshold * 0.01, threshold, indices.sum())

    def _load_data(self):
        """
        Loads the data from the file into the ``x`` and ``y`` attributes.
        """
        with Dataset(self.filename, "r") as dataset:

            variables = dataset.variables
            n = dataset.dimensions["samples"].size

            #
            # Input data
            #

            # Brightness temperatures
            m = dataset.dimensions["channel"].size
            bts = dataset["brightness_temps"][:]

            invalid = (bts > 500.0) + (bts < 0.0)
            bts[invalid] = np.nan

            # Simulate missing high-frequency channels
            if self.augment:
                r = np.random.rand(bts.shape[0])
                bts[r > 0.8, 10:15] = np.nan

            # 2m temperature
            t2m = variables["two_meter_temperature"][:].reshape(-1, 1)
            # Total precitable water.
            tcwv = variables["total_column_water_vapor"][:].reshape(-1, 1)
            # Surface type
            st = variables["surface_type"][:]
            n_types = 18
            st_1h = np.zeros((n, n_types), dtype=np.float32)
            st_1h[np.arange(n), st.ravel() - 1] = 1.0
            # Airmass type
            am = variables["airmass_type"][:]
            n_types = 3
            am_1h = np.zeros((n, n_types), dtype=np.float32)
            am_1h[np.arange(n), np.maximum(am.ravel() - 1, 0)] = 1.0

            self.x = np.concatenate([bts, t2m, tcwv, st_1h, am_1h], axis=1)

            #
            # Output data
            #

            n = dataset.dimensions["samples"].size
            if isinstance(self.target, list):
                self.y = {}
                for l in self.target:
                    y = variables[l][:]
                    y[y < -400] = -9999
                    self.y[l] = y
            else:
                y = variables[self.target][:]
                y[y < -400] = -9999
                self.y = y

            LOGGER.info(
                "Loaded %s samples from %s", self.x.shape[0], self.filename.name
            )

    def save(self, filename):
        """
        Store dataset as NetCDF file.

        Args:
            filename: The name of the file to which to write the dataset.
        """
        if self.normalize:
            x = self.normalizer.invert(self.x)
        else:
            x = self.x
        y = self.y

        bts = x[:, :15]
        t2m = x[:, 15]
        tcwv = x[:, 16]
        st = np.where(x[:, 17: 17 + 18])[1]
        at = np.where(x[:, 17 + 18: 17 + 21])[1]

        dataset = xr.open_dataset(self.filename)

        dims = ("samples", "channel")
        new_dataset = {
            "brightness_temps": (dims, bts),
            "two_meter_temperature": (dims[:1], t2m),
            "total_column_water_vapor": (dims[:1], tcwv),
            "surface_type": (dims[:1], st),
            "airmass_type": (dims[:1], at),
        }
        if isinstance(self.y, dict):
            dims = ("samples", "levels")
            for k, v in self.y.items():
                new_dataset[k] = (dims[:len(v.shape)], v)
        else:
            new_dataset["surface_precip"] = (("samples",), self.y)
        new_dataset = xr.Dataset(new_dataset)
        new_dataset.attrs = dataset.attrs
        new_dataset.to_netcdf(filename)

    def _shuffle(self):
        if not self._shuffled:
            LOGGER.info("Shuffling dataset %s.", self.filename.name)
            indices = np.random.permutation(self.x.shape[0])
            self.x = self.x[indices, :]
            if isinstance(self.y, dict):
                self.y = {k: self.y[k][indices] for k in self.y}
            else:
                self.y = self.y[indices]
            self._shuffled = True

    def __getitem__(self, i):
        """
        Return element from the dataset. This is part of the
        pytorch interface for datasets.

        Args:
            i(int): The index of the sample to return
        """
        if i >= len(self):
            LOGGER.info("Finished iterating through dataset %s.", self.filename.name)
            raise IndexError()
        if i == 0:
            self._shuffle()
            if self.transform_zeros:
                self._transform_zeros()

        self._shuffled = False
        if self.batch_size is None:
            if isinstance(self.y, dict):
                return (
                    torch.tensor(self.x[[i], :]),
                    {k: torch.tensor(self.y[k][[i]]) for k in self.y},
                )

        i_start = self.batch_size * i
        i_end = self.batch_size * (i + 1)

        x = torch.tensor(self.x[i_start:i_end, :])
        if isinstance(self.y, dict):
            y = {k: torch.tensor(self.y[k][i_start:i_end]) for k in self.y}
        else:
            y = torch.tensor(self.y[i_start:i_end])

        return x, y

    def __len__(self):
        """
        The number of samples in the dataset.
        """
        if self.batch_size:
            n = self.x.shape[0] // self.batch_size
            return n
        else:
            return self.x.shape[0]

    def evaluate(self, xrnn, batch_size=16384, device=_DEVICE):
        """
        Run retrieval on test dataset and returns results as
        xarray Dataset.

        Args:
            model: The QRNN or DRNN model to evaluate.
            batch_size: The batch size to use for the evaluation.
            device: On which device to run the evaluation.

        Return:
            ``xarray.Dataset`` containing the predicted and reference values
            for the data in this dataset.
        """
        means = {}
        precip_1st_tercile = []
        precip_3rd_tercile = []
        pop = []

        with torch.no_grad():
            for i in range(len(self)):
                x, y = self[i]

                y_pred = xrnn.predict(x)
                if not isinstance(y_pred, dict):
                    y_pred = {"surface_precip": y_pred}

                y_mean = xrnn.posterior_mean(y_pred=y_pred)
                for k, y in y_pred.items():
                    means.setdefault(k, []).append(y_mean[k].cpu())
                    if k == "surface_precip":
                        t = xrnn.posterior_quantiles(
                            y_pred=y,
                            quantiles=[0.333, 0.667],
                            key=k
                        )
                        precip_1st_tercile.append(t[:, :1].cpu())
                        precip_3rd_tercile.append(t[:, 1:].cpu())
                        p = self.model.probability_larger_than(y_pred=y,
                                                               y=1e-4,
                                                               key=k)
                        pop.append(p.cpu())


        dims = ["samples", "levels"]
        data = {}
        for k in means:
            y = np.concatenate(means[k].numpy())
            if y.ndim == 1:
                y = y.reshape(-1, 221)
            else:
                y = y.reshape(-1, 221, 28)
            data[k] = (dims[:y.ndim], y)

        data["precip_1st_tercile"] = (
            dims[:2],
            np.concatenate(precip_1st_tercile.numpy()).reshape(-1, 221)
        )
        data["precip_3rd_tercile"] = (
            dims[:2],
            np.concatenate(precip_3rd_tercile.numpy()).reshape(-1, 221)
        )
        data["pop"] = (
            dims[:2],
            np.concatenate(pop.numpy()).reshape(-1, 221)
        )
        return xr.Dataset(data)

    def evaluate_sensitivity(self, model, batch_size=512, device=_DEVICE):
        """
        Run retrieval on dataset.
        """
        n_samples = self.x.shape[0]
        y_means = []
        y_trues = []
        grads = []
        surfaces = []
        airmasses = []
        dydxs = []

        st_indices = torch.arange(19).reshape(1, -1).to(device)
        am_indices = torch.arange(4).reshape(1, -1).to(device)
        i_start = 0
        model.model.to(device)

        loss = torch.nn.MSELoss()

        model.model.eval()

        for i in tqdm(range(n_samples // batch_size + 1)):
            i_start = i * batch_size
            i_end = i_start + batch_size
            if i_start >= n_samples:
                break

            model.model.zero_grad()

            x = torch.tensor(self.x[i_start:i_end]).float().to(device)
            x_l = x.clone()
            x_l[:, 0] -= 0.01
            x_r = x.clone()
            x_r[:, 0] += 0.01
            y = torch.tensor(self.y[i_start:i_end]).float().to(device)

            x.requires_grad = True
            y.requires_grad = True
            i_start += batch_size

            y_pred = model.predict(x)
            y_mean = model.posterior_mean(y_pred=y_pred).reshape(-1)
            y_mean_l = model.posterior_mean(x_l).reshape(-1)
            y_mean_r = model.posterior_mean(x_r).reshape(-1)
            dydx = (y_mean_r - y_mean_l) / 0.02
            torch.sum(y_mean).backward()

            y_means.append(y_mean.detach().cpu())
            y_trues.append(y.detach().cpu())
            grads.append(x.grad[:, :15].cpu())
            surfaces += [(x[:, 17:36] * st_indices).sum(1).cpu()]
            airmasses += [(x[:, 36:] * am_indices).sum(1).cpu()]
            dydxs += [dydx.cpu()]

        y_means = torch.cat(y_means, 0).detach().numpy()
        y_trues = torch.cat(y_trues, 0).detach().numpy()
        grads = torch.cat(grads, 0).detach().numpy()
        surfaces = torch.cat(surfaces, 0).detach().numpy()
        airmasses = torch.cat(airmasses, 0).detach().numpy()
        dydxs = torch.cat(dydxs, 0).detach().numpy()

        dims = ["samples"]

        data = {
            "gradients": (
                dims
                + [
                    "channels",
                ],
                grads,
            ),
            "surface_type": (dims, surfaces),
            "airmass_type": (dims, airmasses),
            "y_mean": (dims, y_means),
            "y_true": (dims, y_trues),
            "dydxs": (dims, dydxs),
        }
        return xr.Dataset(data)
