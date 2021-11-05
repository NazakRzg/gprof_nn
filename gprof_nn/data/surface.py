"""
=====================
gprof_nn.data.surface
=====================

This module contains files to load surface type maps for different sensors.

"""
import gzip
import io
import struct
import subprocess
from pathlib import Path
from tempfile import mkstemp

import numpy as np
import pandas as pd
import scipy as sp
import xarray as xr
from gprof_nn.data.preprocessor import PREPROCESSOR_SETTINGS
from scipy.signal import convolve

LANDMASKS = {
    "AMSRE": "landmask51_32.bin",
    "AMSR2": "landmask42_32.bin",
    "GMI": "landmask32_32.bin",
    "SSMI": "landmask69_32.bin",
    "SSMIS": "landmask74_32.bin",
    "TMI": "landmask60_32.bin",
    "ATMS": "landmask34_16.bin",
    "MHS": "landmask34_16.bin",
}


def read_land_mask(sensor):
    """
    Read the land mask for a given sensor.

    Args:
        sensor: The sensor for which to read the land mask.

    Return:
        An xarray dataset containing the land mask with corresponding
        latitude and longitude coordinates.
    """
    ancdir = Path(PREPROCESSOR_SETTINGS["ancdir"])
    path = ancdir / LANDMASKS[sensor]
    data = np.fromfile(ancdir / LANDMASKS[sensor], dtype="i1")

    # Filename contains the resolution per degree
    resolution = int(path.stem.split("_")[-1])

    shape = (180 * resolution, 360 * resolution)

    lats = np.linspace(-90, 90, shape[0] + 1)[:-1]
    lons = np.linspace(-180, 180, shape[1] + 1)[:-1]

    # Meridional axis is mirrored
    data = data.reshape(shape)[::-1]

    dataset = xr.Dataset(
        {
            "latitude": (("latitude",), lats),
            "longitude": (("longitude",), lons),
            "mask": (("latitude", "longitude"), data),
        }
    )
    if resolution > 16:
        dataset = dataset[{
            "latitude": slice(0, None, 2),
            "longitude": slice(0, None, 2)
        }]
    return dataset


def read_autosnow(date, legacy=False):
    """
    Read autosnow mask for a given date.

    This function also adds in new values (5) to identify sea ice
    edge pixels.

    Args:
        date: The date for which to read the autosnow file.

    Return:
        An xarray dataset containing the autosnow mask with the
        with the postprocessing similar to the CSU preprocessor.
    """
    ingest_dir = Path(PREPROCESSOR_SETTINGS["ingestdir"])

    date = pd.Timestamp(date)

    N_LAT = 4500
    N_LON = 9000
    MISSING = 255

    filename = None
    for i in range(31):
        delta = pd.Timedelta(i, "D")
        search_date = date - delta
        year = search_date.year
        day = search_date.dayofyear
        year_s = f"{year:04}"
        day_s = f"{day:03}"
        path = ingest_dir / "autosnowV3" / year_s

        if legacy:
            filename = path / f"autosnow_global.v003.{year_s}{day_s}.Z"
        else:
            filename = path / f"gmasi_snowice_reproc_v003_{year_s}{day_s}.Z"

        if filename.exists():
            break
    print(filename)

    if filename is None:
        raise Exception(f"Couldn't find autosnow file for date {date}")

    if filename.suffix == ".Z":
        _, tmp = mkstemp()
        tmp = Path(tmp)
        try:
            with open(tmp, "wb") as buffer:
                subprocess.run(["gunzip", "-c", filename], stdout=buffer, check=True)
            data = np.fromfile(tmp, "i1")
        finally:
            tmp.unlink()
    else:
        data = np.fromfile(filename, "i1")

    data = data.reshape(N_LAT, N_LON)

    # Mark invalid values
    mask = (data < 0) + (data > 3)
    data[mask] = MISSING

    for i in range(1, 10):

        if np.all(data != MISSING):
            break

        # South-East direction
        valid = mask[:-i, :-i]
        missing_mask = valid == MISSING
        source = mask[i:, i:]
        replace = missing_mask * (source != MISSING)
        valid[replace] = source[replace]

        # North-East direction
        valid = mask[i:, :-i]
        missing_mask = valid == MISSING
        source = mask[:-i, i:]
        replace = missing_mask * (source != MISSING)
        valid[replace] = source[replace]

        # South-West direction
        valid = mask[:-i, i:]
        missing_mask = valid == MISSING
        source = mask[i:, :-i]
        replace = missing_mask * (source != MISSING)
        valid[replace] = source[replace]

        # North-East direction
        valid = mask[i:, i:]
        missing_mask = valid == MISSING
        source = mask[:-i, :-i]
        replace = missing_mask * (source != MISSING)
        valid[replace] = source[replace]

    # Replace 3 that have too few neighbors
    k = np.ones((3, 3))
    k[1, 1] = 0
    sums = convolve(data, k, mode="valid")
    source = data[1:-1, 1:-1]
    source[(source == 3) * (sums <= 12)] = 0

    lats = np.linspace(-90, 90, N_LAT + 1)[:-1]
    lons = np.linspace(-180, 180, N_LON + 1)[:-1]

    # Extend sea ice with sea ice edge class.
    k = np.ones((11, 11), dtype=np.int8)
    sea_ice = (data == 3).astype(np.int8)
    sea_ice_ext = convolve(sea_ice, k, mode="same")

    replace = (data == 0) * (sea_ice_ext > 0)
    data[replace] = 5

    # Meridional axis is mirrored
    data = data[::-1, :]

    dataset = xr.Dataset(
        {
            "latitude": (("latitude",), lats),
            "longitude": (("longitude",), lons),
            "snow": (("latitude", "longitude"), data),
        }
    )

    return dataset


def read_emissivity_classes():
    """
    Read maps of emissivity classes for all 12 months.

    Return:
        An 'xarray.Dataset' containing the emissivity class maps.
    """
    ancdir = Path(PREPROCESSOR_SETTINGS["ancdir"])

    data = []
    for month in range(1, 13):
        path = ancdir / f"emiss_class_{month:02}.dat"
        data.append(np.fromfile(path, "i2"))
    data = np.stack(data)

    N_LON = 720
    N_LAT = 359

    data = data.reshape(12, N_LAT, N_LON)

    for i in range(4):
        data_c = data.copy()
        if np.all(data > 0):
            break
        for offs_lat in [-1, 0, 1]:
            for offs_lon in [-1, 0, 1]:
                lat_slice = slice(max(-offs_lat, 0), N_LAT - offs_lat)
                lon_slice = slice(max(-offs_lon, 0), N_LON - offs_lon)
                dest = data_c[:, lat_slice, lon_slice]

                lat_slice = slice(max(offs_lat, 0), N_LAT + offs_lat)
                lon_slice = slice(max(offs_lon, 0), N_LON + offs_lon)
                source = data[:, lat_slice, lon_slice]

                replace = (dest == 0) * (source != 0)
                dest[replace] = source[replace]
        data = data_c

    lats = np.linspace(-90, 90, N_LAT + 1)[1:]
    lons = np.linspace(-180, 180, N_LON + 1)
    lons = 0.5 * (lons[1:] + lons[:-1])

    # Meridional axis is mirrored
    data = data[:, ::-1, :]

    dataset = xr.Dataset(
        {
            "latitude": (("latitude",), lats),
            "longitude": (("longitude",), lons),
            "month": (("month",), np.arange(1, 13)),
            "emissivity": (("month", "latitude", "longitude"), data),
        }
    )
    return dataset


def read_mountain_mask():
    """
    Read the mountain mask used to determine surface types
    17 and 18.

    Return:
        xarray.Dataset containing the mountain mask.
    """
    ancdir = Path(PREPROCESSOR_SETTINGS["ancdir"])
    path = ancdir / "k3classes_0.1deg.asc.bin"

    with open(path, "rb") as buffer:
        buffer.read(4)
        n_lons, n_lats = np.fromfile(buffer, dtype="i4", count=2)
        n_elem = n_lons * n_lats
        buffer.read(8)
        data = np.fromfile(buffer, dtype="i", count=n_elem)

    shape = (n_lats, n_lons)
    lats = np.linspace(-90, 90, shape[0] + 1)[:-1]
    lons = np.linspace(-180, 180, shape[1] + 1)[:-1]
    data = data.reshape(shape)

    data = np.concatenate([data[:, shape[1] // 2:], data[:, :shape[1] // 2]],
                          axis=-1)
    dataset = xr.Dataset({
        "latitude": (("latitude", ), lats),
        "longitude": (("longitude",), lons),
        "mask": (("latitude", "longitude"), data)
    })
    return dataset


def combine_surface_types(
        land,
        snow,
        emiss,
        mtn,
        month):
    """
    Combines land, snow and emissivity maps to derive the CSU
    surface classficiation.

    This code was ported from Fortran.

    Args:
        land: Array containing the land/sea map
        snow: Array containing the autosnow classes
        emiss: Array containing the emissivity classes for each month.
        mtn: The mountain mask to determine surface types 17 and 18.
        month: Index of the month.

    Returns:

        Array of the same shape as 'land' containing mapping each pixel
        to one of the 18 CSU surface classes.
    """
    sfc = np.zeros(land.shape, dtype="i4")

    # Very little land -> Ocean
    mask = (land >= 0) * (land <= 2)
    sfc[mask] = 10
    # Coast 1
    mask = (land > 2) * (land <= 25)
    sfc[mask] = 30
    # Coast 2
    mask = (land > 25) * (land <= 75)
    sfc[mask] = 31
    # Coast 3
    mask = (land > 75) * (land <= 95)
    sfc[mask] = 32
    mask = land > 95
    sfc[mask] = 20

    # Ocean
    mask = sfc == 10
    sfc[mask] = 1

    # Sea ice and sea-ice boundary
    # Snow over Ocean
    mask = (sfc == 1) * (snow == 2)
    sfc[mask] = 2
    # Autosnow sea ice
    mask = snow == 3
    sfc[mask] = 2
    mask = snow == 5
    sfc[mask] = 16

    #
    # Snow
    #

    land = sfc == 20
    mask_auto_snow = (snow == 2) + (snow == 3)
    mask_emiss_snow = (emiss[month] >= 6) * (emiss[month] <= 9)

    # Both autosnow and emissivities predict snow
    mask = land * mask_auto_snow * mask_emiss_snow
    sfc[mask] = emiss[month][mask] + 2

    # Only autosnow predicts snow
    mask_emiss_no_snow = (((emiss[month] >= 1) * (emiss[month] <= 5)) +
                          (emiss[month] == 10))
    mask = land * mask_auto_snow * mask_emiss_no_snow
    sfc[mask] = 10

    # Antartica
    mask = land * mask_auto_snow * (emiss[month] == 0)
    sfc[mask] = 8

    #
    # No (auto) snow
    #

    mask_emiss_land = (land * ~mask_auto_snow *
                       (emiss[month] >= 1) * (emiss[month] <= 5))
    sfc[mask_emiss_land] = emiss[month][mask_emiss_land] + 2

    # If autosnow says snow but emissivity does not, look for latest
    # non-snow emissivity class.
    latest_emiss = emiss[month].copy()
    for i in range(1, 12):
        mask_emiss_snow = (latest_emiss >= 6) * (latest_emiss <= 9)
        mask = land * ~mask_auto_snow * mask_emiss_snow
        if not np.any(mask):
            break
        m = ((month - i) % 12)

        replace = mask * ((emiss[m] < 6) + (emiss[m] == 10))
        latest_emiss[replace] = emiss[m][replace]
        print(m, mask.sum())

    mask_emiss_snow = (latest_emiss >= 6) * (latest_emiss <= 9)
    mask = land * ~mask_auto_snow * mask_emiss_snow
    latest_emiss[mask] = 9

    mask_emiss_snow = (emiss[month] >= 6) * (emiss[month] <= 9)
    mask = land * ~mask_auto_snow * mask_emiss_snow
    sfc[mask] = latest_emiss[mask] + 2

    # Standing water
    mask = land * ~mask_auto_snow * (emiss[month] == 10)
    sfc[mask] = 12

    # Land class without emission -> Ocean
    mask = land * ~mask_auto_snow * (emiss[month] == 0)
    sfc[mask] = 1

    # Any remaining values become inland water
    mask = sfc == 20
    sfc[mask] = 12

    # Coast
    mask = sfc == 30
    sfc[mask] = 13
    mask = sfc == 31
    sfc[mask] = 14
    mask = sfc == 32
    sfc[mask] = 15

    # Handle snow and sea ice at in coastal areas
    coast = (sfc >= 13) * (sfc <= 15)
    mask = coast * (snow == 2)
    sfc[mask] = 10
    mask = coast * (snow == 3)
    sfc[mask] = 2

    # Mountain rain and snow
    mask = (mtn >= 1) * (sfc >= 3) * (sfc <= 7)
    sfc[mask] = 17
    mask = (mtn >= 1) * (sfc >= 8) * (sfc <= 11)
    sfc[mask] = 18

    return sfc


def get_surface_type_map(sensor, date):
    date = pd.Timestamp(date)
    month = date.month
    land = read_land_mask(sensor)
    snow = read_autosnow(date)
    emiss = read_emissivity_classes()
    mtn = read_mountain_mask()

    # Combine with autosnow and emissivity and assemble final
    # classification.
    snow = snow.interp(
        {"latitude": land.latitude, "longitude": land.longitude}, "nearest"
    ).snow.data
    emiss = emiss.interp(
        {"latitude": land.latitude, "longitude": land.longitude}, "nearest"
    ).emissivity.data
    mtn = mtn.interp(
        {"latitude": land.latitude, "longitude": land.longitude}, "nearest"
    ).mask.data

    surface_types = combine_surface_types(
        land.mask.data, snow, emiss, mtn, month
    )
    return surface_types


def get_surface_types(sensor, date, latitude, longitude):
    date = pd.Timestamp(date)
    month = date.month - 1
    land = read_land_mask(sensor)
    snow = read_autosnow(date)
    emiss = read_emissivity_classes()
    mtn = read_mountain_mask()

    shape = latitude.shape
    latitude = latitude.ravel()
    longitude = longitude.ravel()
    mask = longitude > 180
    longitude[mask] = longitude[mask] - 360
    mask = longitude < -180
    longitude[mask] = longitude[mask] + 360

    CELLS_PER_DEGREE = 16
    LAT_MAX = 180 * CELLS_PER_DEGREE
    LON_MAX = 360 * CELLS_PER_DEGREE
    lat_grid = np.linspace(-90, 90, 180 * CELLS_PER_DEGREE + 1)
    lon_grid = np.linspace(-180, 180, 360 * CELLS_PER_DEGREE + 1)

    inds_lat = np.clip(np.digitize(latitude, lat_grid), 1, LAT_MAX) - 1
    inds_lon = np.clip(np.digitize(longitude, lon_grid), 1, LON_MAX) - 1

    LAT_MAX_SNOW = 4500
    LON_MAX_SNOW = 9000
    inds_lat_snow = np.clip(
        np.trunc(inds_lat / 0.64).astype(np.int16), 0, LAT_MAX_SNOW - 1)
    inds_lon_snow = np.clip(np.trunc(inds_lon / 0.64).astype(np.int16), 0, LON_MAX_SNOW - 1)

    LAT_MAX_EMISS = 359
    LON_MAX_EMISS = 720
    inds_lat_emiss = np.clip(inds_lat // 8, 1, LAT_MAX_EMISS) - 1
    inds_lon_emiss = np.clip(inds_lon // 8, 0, LON_MAX_EMISS - 1)

    LAT_MAX_MTN = mtn.latitude.size
    LON_MAX_MTN = mtn.longitude.size
    lat_grid = np.linspace(-90, 90, LAT_MAX_MTN + 1)
    lon_grid = np.linspace(-180, 180, LON_MAX_MTN + 1)
    inds_lat_mtn = np.clip(np.digitize(latitude, lat_grid), 1, LAT_MAX_MTN) - 1
    inds_lon_mtn = np.clip(np.digitize(longitude, lon_grid), 1, LON_MAX_MTN) - 1

    land = land.mask.data[inds_lat, inds_lon].reshape(shape)
    snow = snow.snow.data[inds_lat_snow, inds_lon_snow].reshape(shape)
    emiss = emiss.emissivity.data[:, inds_lat_emiss, inds_lon_emiss]
    emiss = emiss.reshape((12,) + shape)
    mtn = mtn.mask.data[inds_lat_mtn, inds_lon_mtn].reshape(shape)

    surface_types = combine_surface_types(
        land, snow, emiss, mtn, month
    )
    return surface_types
