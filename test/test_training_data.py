"""
Tests for the Pytorch dataset classes used to load the training
data.
"""
from pathlib import Path

import numpy as np
import torch
import xarray as xr

from quantnn.qrnn import QRNN
from quantnn.normalizer import Normalizer
from quantnn.models.pytorch.xception import XceptionFpn

from gprof_nn import sensors
from gprof_nn.data.training_data import (GPROF0DDataset,
                                         TrainingObsDataset0D,
                                         GPROF2DDataset)


def test_gprof_0d_dataset_gmi():
    """
    Ensure that iterating over single-pixel dataset conserves
    statistics.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "training_data.nc"
    dataset = GPROF0DDataset(input_file,
                             batch_size=1,
                             augment=False,
                             targets=["surface_precip"])

    xs = []
    ys = []

    x_mean_ref = dataset.x.sum(axis=0)
    y_mean_ref = dataset.y["surface_precip"].sum(axis=0)

    for x, y in dataset:
        xs.append(x)
        ys.append(y["surface_precip"])

    xs = torch.cat(xs, dim=0)
    ys = torch.cat(ys, dim=0)

    x_mean = xs.sum(dim=0).detach().numpy()
    y_mean = ys.sum(dim=0).detach().numpy()

    assert np.all(np.isclose(x_mean, x_mean_ref, rtol=1e-3))
    assert np.all(np.isclose(y_mean, y_mean_ref, rtol=1e-3))


def test_gprof_0d_dataset_multi_target_gmi():
    """
    Ensure that iterating over single-pixel dataset conserves
    statistics.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "training_data.nc"
    dataset = GPROF0DDataset(
        input_file,
        targets=["surface_precip",
                 "latent_heat",
                 "rain_water_content"],
        batch_size=1,
        transform_zeros=False

    )

    xs = []
    ys = {}

    x_mean_ref = np.sum(dataset.x, axis=0)
    y_mean_ref = {k: np.sum(dataset.y[k], axis=0) for k in dataset.y}

    for x, y in dataset:
        xs.append(x)
        for k in y:
            ys.setdefault(k, []).append(y[k])

    xs = torch.cat(xs, dim=0)
    ys = {k: torch.cat(ys[k], dim=0) for k in ys}

    x_mean = np.sum(xs.detach().numpy(), axis=0)
    y_mean = {k: np.sum(ys[k].detach().numpy(), axis=0) for k in ys}

    assert np.all(np.isclose(x_mean, x_mean_ref, atol=1e-3))
    for k in y_mean_ref:
        assert np.all(np.isclose(y_mean[k], y_mean_ref[k], rtol=1e-3))


def test_gprof_0d_dataset_mhs():
    """
    Ensure that iterating over single-pixel dataset conserves
    statistics.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "gprof_nn_mhs_era5.nc"
    dataset = GPROF0DDataset(input_file,
                             batch_size=1,
                             augment=False,
                             targets=["surface_precip"],
                             sensor=sensors.MHS)

    xs = []
    ys = []

    x_mean_ref = dataset.x.sum(axis=0)
    y_mean_ref = dataset.y["surface_precip"].sum(axis=0)

    for x, y in dataset:
        xs.append(x)
        ys.append(y["surface_precip"])

    xs = torch.cat(xs, dim=0)
    ys = torch.cat(ys, dim=0)

    x_mean = xs.sum(dim=0).detach().numpy()
    y_mean = ys.sum(dim=0).detach().numpy()

    assert np.all(np.isclose(x_mean, x_mean_ref, rtol=1e-3))
    assert np.all(np.isclose(y_mean, y_mean_ref, rtol=1e-3))

    assert(np.all(np.isclose(x[:, 8:26].sum(-1),
                             1.0)))


def test_gprof_0d_dataset_multi_target_mhs():
    """
    Ensure that iterating over single-pixel dataset conserves
    statistics.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "gprof_nn_mhs_era5.nc"
    dataset = GPROF0DDataset(
        input_file,
        targets=["surface_precip",
                 "latent_heat",
                 "rain_water_content"],
        batch_size=1,
        transform_zeros=False,
        sensor=sensors.MHS
    )

    xs = []
    ys = {}

    x_mean_ref = np.sum(dataset.x, axis=0)
    y_mean_ref = {k: np.sum(dataset.y[k], axis=0) for k in dataset.y}

    for x, y in dataset:
        xs.append(x)
        for k in y:
            ys.setdefault(k, []).append(y[k])

    xs = torch.cat(xs, dim=0)
    ys = {k: torch.cat(ys[k], dim=0) for k in ys}

    x_mean = np.sum(xs.detach().numpy(), axis=0)
    y_mean = {k: np.sum(ys[k].detach().numpy(), axis=0) for k in ys}

    assert np.all(np.isclose(x_mean, x_mean_ref, atol=1e-3))
    for k in y_mean_ref:
        assert np.all(np.isclose(y_mean[k], y_mean_ref[k], rtol=1e-3))


def test_observation_dataset_0d():
    """
    Test loading of observations data from MHS training data.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "gprof_nn_mhs_era5.nc"
    input_data = xr.load_dataset(input_file)
    dataset = TrainingObsDataset0D(
        input_file,
        batch_size=1,
        transform_zeros=False,
        sensor=sensors.MHS,
        normalize=False,
        shuffle=False
    )

    x, y = dataset[0]
    x = x.detach().numpy()
    y = y.detach().numpy()

    assert x.shape[1] == 19
    assert y.shape[1] == 5

    sp = input_data["surface_precip"].data
    valid = np.all(sp >= 0, axis=-1)
    st = input_data["surface_type"].data[valid]
    st_x = np.where(x[0, 1:])[0][0] + 1
    assert st[0] == st_x


def test_profile_variables():
    """
    Ensure profile variables are available everywhere except over sea ice
    or snow.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "training_data.nc"

    PROFILE_TARGETS = [
        "rain_water_content",
        "snow_water_content",
        "cloud_water_content",
        "latent_heat"
    ]
    dataset = GPROF0DDataset(
        input_file, targets=PROFILE_TARGETS, batch_size=1
    )

    for t in PROFILE_TARGETS:
        x = dataset.x
        y = dataset.y[t]

        st = np.where(x[:, 17:35])[1]
        indices = (st >= 8) * (st <= 11)


def test_gprof_2d_dataset():
    """
    Ensure that iterating over 2D dataset conserves
    statistics.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "training_data.nc"
    dataset = GPROF2DDataset(input_file,
                             batch_size=1,
                             augment=False,
                             transform_zeros=True)

    xs = []
    ys = []

    x_mean_ref = dataset.x.sum(axis=0)
    y_mean_ref = dataset.y.sum(axis=0)

    for x, y in dataset:
        xs.append(x)
        ys.append(y)

    xs = torch.cat(xs, dim=0)
    ys = torch.cat(ys, dim=0)

    x_mean = xs.sum(dim=0).detach().numpy()
    y_mean = ys.sum(dim=0).detach().numpy()

    y_mean = y_mean[np.isfinite(y_mean)]
    y_mean_ref = y_mean_ref[np.isfinite(y_mean_ref)]

    assert np.all(np.isclose(x_mean, x_mean_ref, atol=1e-3))
    assert np.all(np.isclose(y_mean, y_mean_ref, atol=1e-3))


def test_gprof_2d_dataset_profiles():
    """
    Ensure that loading of profile variables works.
    """
    path = Path(__file__).parent
    input_file = path / "data" / "training_data.nc"
    dataset = GPROF2DDataset(input_file,
                             batch_size=1,
                             augment=False,
                             transform_zeros=True,
                             target=[
                                 "rain_water_content",
                                 "snow_water_content",
                                 "cloud_water_content"
                             ])

    xs = []
    ys = {}

    x_mean_ref = dataset.x.sum(axis=0)
    y_mean_ref = {}
    for k in dataset.target:
        y_mean_ref[k] = dataset.y[k].sum(axis=0)

    for x, y in dataset:
        xs.append(x)
        for k in y:
            ys.setdefault(k, []).append(y[k])

    xs = torch.cat(xs, dim=0)
    for k in dataset.target:
        ys[k] = torch.cat(ys[k], dim=0)

    x_mean = xs.sum(dim=0).detach().numpy()
    y_mean = {}
    for k in dataset.target:
        y_mean[k] = ys[k].sum(dim=0).detach().numpy()

    for k in dataset.target:
        y_mean[k] = y_mean[k][np.isfinite(y_mean[k])]
        y_mean_ref[k] = y_mean_ref[k][np.isfinite(y_mean_ref[k])]

    assert np.all(np.isclose(x_mean, x_mean_ref, atol=1e-3))
    for k in dataset.target:
        assert np.all(np.isclose(y_mean[k], y_mean_ref[k], atol=1e-3))
