"""
==================
gprof_nn.bin.train
==================

This sub-module implements the 'train' sub-command of the
'gprof_nn' command line application, which trains
networks for the GPROF-NN retrieval algorithm.
"""
import argparse
import logging
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from gprof_nn import sensors, statistics
import gprof_nn.logging
from gprof_nn.retrieval import RetrievalDriver, RetrievalGradientDriver
from gprof_nn.definitions import (ALL_TARGETS,
                                  PROFILE_NAMES,
                                  CONFIGURATIONS,
                                  GPROF_NN_DATA_PATH)
from gprof_nn.data.training_data import (GPROF_NN_0D_Dataset,
                                         GPROF_NN_2D_Dataset)
from gprof_nn.models import (GPROF_NN_0D_DRNN,
                             GPROF_NN_0D_QRNN,
                             GPROF_NN_2D_DRNN,
                             GPROF_NN_2D_QRNN)


LOGGER = logging.getLogger(__name__)


def add_parser(subparsers):
    """
    Add parser for 'train' command to top-level parser. This function
    is called from the top-level parser defined in 'gprof_nn.bin'.

    Args:
        subparsers: The subparsers object provided by the top-level parser.
    """
    parser = subparsers.add_parser(
        "train",
        description=(
            """
            Trains a GPROF-NN 0D or 2D network.
            """
            )
    )

    # Input and output data
    parser.add_argument(
        'variant',
        metavar='kind',
        type=str,
        help="The type of GPROF-NN model to train: '0D' or '2D'"
    )
    parser.add_argument(
        'sensor',
        metavar='sensor',
        type=str,
        help="Name of the sensor for which to train the algorithm."
    )
    parser.add_argument(
        'configuration',
        metavar='[ERA5/GANAL]',
        type=str,
        help="The configuration for which the model is trained."
    )
    parser.add_argument(
        'training_data',
        metavar='training_data',
        type=str,
        help='Path to training data.'
    )
    parser.add_argument(
        'validation_data',
        metavar='validation_data',
        type=str,
        help='Path to validation data.'
    )
    parser.add_argument(
        'output',
        metavar='output',
        type=str,
        nargs=1,
        help='Where to store the model.'
    )

    # Model configuration
    parser.add_argument(
        '--type',
        metavar="network_type",
        type=str,
        nargs=1,
        help="The type of network: drnn, qrnn or qrnn_exp",
        default="qrnn_exp"
    )

    parser.add_argument(
        '--n_layers_body',
        metavar='n',
        type=int,
        nargs=1,
        default=6,
        help=("For GPROF-NN 0D: The number of hidden layers in the shared body"
              " of the network.")
    )

    parser.add_argument(
        "--n_neurons_body",
        metavar='n',
        type=int,
        nargs=1,
        default=256,
        help=("For GPROF-NN 0D and 2D: The number of neurons in the body.")
    )
    parser.add_argument(
        '--n_layers_head',
        metavar='n',
        type=int,
        nargs=1,
        default=2,
        help='For GPROF-NN 0D: How many layers in the heads of the model.'
    )
    parser.add_argument(
        '--n_neurons_head',
        metavar='n',
        type=int,
        nargs=1,
        default=128,
        help=('For GPROF-NN 0D and 2D: How many neurons in each head of the '
              'model.')
    )
    parser.add_argument(
        '--n_blocks',
        metavar='N',
        type=int,
        nargs=1,
        default=2,
        help=('For GPROF-NN 2D: The number of Xception  block per '
              ' downsampling stage of the model.')
    )
    parser.add_argument(
        '--activation',
        metavar='activation',
        type=str,
        nargs=1,
        default="ReLU",
        help='For GPROF-NN 0D: The activation function.'
    )
    parser.add_argument(
        '--residuals',
        metavar='residuals',
        type=str,
        nargs=1,
        default='simple',
        help='For GPROF-NN 0D: The type of residual connections to apply.'
    )
    parser.add_argument(
        '--n_epochs',
        metavar='n',
        type=int,
        nargs="*",
        default=[20, 20, 20],
        help=('For how many epochs to train the network. When multiple values '
              'are given the network is trained multiple times (warm restart).')
    )
    parser.add_argument(
        '--learning_rate',
        metavar='lr',
        type=float,
        nargs="*",
        default=[0.0005, 0.0005, 0.0001],
        help='The learning rates to use during training.'
    )

    # Other
    parser.add_argument(
        '--device', metavar="device", type=str, nargs=1,
        help="The name of the device on which to run the training"
    )
    parser.add_argument(
        '--targets', metavar="target_1 target_2", type=str, nargs="+",
        help="The target on which to train the network"
    )
    parser.add_argument(
        '--batch_size', metavar="n", type=int, nargs=1,
        help="The batch size to use for training."
    )
    parser.add_argument(
        '--permute', metavar="feature_index", type=int,
        help=("If provided, the input feature with the given index "
              "will be permuted.")
    )
    parser.set_defaults(func=run)


def run(args):
    """
    Run the training.

    Args:
        args: The namespace object provided by the top-level parser.
    """

    sensor = args.sensor
    configuration = args.configuration
    training_data = args.training_data[0]
    validation_data = args.validation_data[0]

    #
    # Determine sensor
    #

    sensor = sensor.strip().upper()
    sensor = getattr(sensors, sensor, None)
    if sensor is None:
        LOGGER.error(
            "Sensor '%s' is not supported.",
            args.sensor.strip().upper()
        )
        return 1

    variant = args.variant
    if variant.upper() not in ["0D", "2D"]:
        LOGGER.error(
            "'variant' should be one of ['0D', '2D']"
        )
        return 1

    #
    # Configuration
    #

    if configuration.upper() not in CONFIGURATIONS:
        LOGGER.error(
            "'configuration' should be one of $s.",
            CONFIGURATIONS
        )
        return 1

    # Check output path and define model name if necessary.
    output = Path(args.output[0])
    if output.is_dir() and not output.exists():
        LOGGER.error(
            "The output path '%s' doesn't exist.", output
        )
        return 1
    if not output.is_dir() and not output.parent.exists():
        LOGGER.error(
            "The output path '%s' doesn't exist.", output.parent
        )
        return 1
    if output.is_dir():
        network_name = (f"gprof_nn_{variant.lower()}_{configuration.lower()}_"
                       f"{sensor.name.lower()}.pckl")
        output = output / network_name

    training_data = args.training_data
    validation_data = args.validation_data


    if variant.upper() == "0D":
        run_training_0d(sensor,
                        configuration,
                        training_data,
                        validation_data,
                        output,
                        args)
    else:
        run_training_2d(sensor,
                        configuration,
                        training_data,
                        validation_data,
                        output,
                        args)


def run_training_0d(sensor,
                    configuration,
                    training_data,
                    validation_data,
                    output,
                    args):
    """
    Run training for GPROF-NN 0D algorithm.

    Args:
        sensor: Sensor object representing the sensor for which to train
            an algorithm.
        configuration: String identifying the retrieval configuration.
        training_data: The path to the training data.
        validation_data: The path to the validation data.
        output: Path to which to write the resulting model.
        args: Namespace with the remaining command line arguments.
    """
    from quantnn.qrnn import QRNN
    from quantnn.normalizer import Normalizer
    from quantnn.data import DataFolder
    from quantnn.transformations import LogLinear
    from quantnn.models.pytorch.logging import TensorBoardLogger
    from quantnn.metrics import ScatterPlot
    import torch
    from torch import optim

    n_layers_body = args.n_layers_body[0]
    n_neurons_body = args.n_neurons_body[0]
    n_layers_head = args.n_layers_head[0]
    n_neurons_head = args.n_neurons_head[0]

    activation = args.activation[0]
    residuals = args.residuals[0]

    device = args.device[0]
    targets = args.targets
    network_type = args.type[0]
    batch_size = args.batch_size[0]
    permute = args.permute

    n_epochs = args.n_epochs
    lr = args.learning_rate

    if len(n_epochs) == 1:
        n_epochs = n_epochs * len(lr)
    if len(lr) == 1:
        lr = lr * len(n_epochs)

    #
    # Load training data.
    #

    dataset_factory = GPROF_NN_0D_Dataset
    normalizer_path = (GPROF_NN_DATA_PATH /
                       f"normalizer_{sensor.name.lower()}.pckl")
    normalizer = Normalizer.load(normalizer_path)
    kwargs = {
        "sensor": sensor,
        "batch_size": batch_size,
        "normalizer": normalizer,
        "targets": targets,
        "augment": True,
        "permute": permute
    }

    training_data = DataFolder(
        training_data,
        dataset_factory,
        kwargs=kwargs,
        queue_size=64,
        n_workers=4
    )

    kwargs = {
        "sensor": sensor,
        "batch_size": 4 * batch_size,
        "normalizer": normalizer,
        "targets": targets,
        "augment": False,
        "permute": permute,
    }
    validation_data = DataFolder(
        validation_data,
        dataset_factory,
        kwargs=kwargs,
        queue_size=64,
        n_workers=2
    )

    #
    # Create neural network model
    #

    if Path(output).exists():
        try:
            xrnn = QRNN.load(output)
            LOGGER.info(
                f"Continuing training of existing model {output}."
            )
        except Exception:
            xrnn = None

    if xrnn is None:
        LOGGER.info(
            f"Creating new model of type {network_type}."
        )
        if network_type == "drnn":
            xrnn = GPROF_NN_0D_DRNN(sensor,
                                    n_layers_body,
                                    n_neurons_body,
                                    n_layers_head,
                                    n_neurons_head,
                                    activation=activation,
                                    residuals=residuals,
                                    targets=targets)
        elif network_type == "qrnn_exp":
            transformation = {t: LogLinear() for t in targets}
            transformation["latent_heat"] = None
            xrnn = GPROF_NN_0D_QRNN(sensor,
                                    n_layers_body,
                                    n_neurons_body,
                                    n_layers_head,
                                    n_neurons_head,
                                    activation=activation,
                                    residuals=residuals,
                                    transformation=transformation,
                                    targets=targets)
        else:
            xrnn = GPROF_NN_0D_QRNN(sensor,
                                    n_layers_body,
                                    n_neurons_body,
                                    n_layers_head,
                                    n_neurons_head,
                                    activation=activation,
                                    residuals=residuals,
                                    targets=targets)
    model = xrnn.model
    xrnn.normalizer = normalizer

    #
    # Run training
    #

    n_epochs_tot = sum(n_epochs)
    logger = TensorBoardLogger(n_epochs_tot)
    logger.set_attributes({
        "sensor": sensor.name,
        "configuration": configuration,
        "n_layers_body": n_layers_body,
        "n_neurons_body": n_neurons_body,
        "n_layers_head": n_layers_head,
        "n_neurons_head": n_neurons_head,
        "activation": activation,
        "residuals": residuals,
        "targets": ", ".join(targets),
        "type": network_type,
        "optimizer": "adam"
        })
    metrics = ["MeanSquaredError", "Bias", "CalibrationPlot", "CRPS"]
    scatter_plot = ScatterPlot(log_scale=True)
    metrics.append(scatter_plot)

    for n, r in zip(n_epochs, lr):
        LOGGER.info(
            f"Starting training for {n} epochs with learning rate {r}"
        )
        optimizer = optim.Adam(model.parameters(), lr=r)
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, n)
        xrnn.train(training_data=training_data,
                   validation_data=validation_data,
                   n_epochs=n,
                   optimizer=optimizer,
                   scheduler=scheduler,
                   logger=logger,
                   metrics=metrics,
                   device=device,
                   mask=-9999)
        LOGGER.info(
            f"Saving training network to {output}."
        )
        xrnn.save(output)


def run_training_2d(sensor,
                    configuration,
                    training_data,
                    validation_data,
                    output,
                    args):
    """
    Run training for GPROF-NN 2D algorithm.

    Args:
        sensor: Sensor object representing the sensor for which to train
            an algorithm.
        configuration: String identifying the retrieval configuration.
        training_data: The path to the training data.
        validation_data: The path to the validation data.
        output: Path to which to write the resulting model.
        args: Namespace with the remaining command line arguments.
    """
    from quantnn.qrnn import QRNN
    from quantnn.normalizer import Normalizer
    from quantnn.data import DataFolder
    from quantnn.transformations import LogLinear
    from quantnn.models.pytorch.logging import TensorBoardLogger
    from quantnn.metrics import ScatterPlot
    import torch
    from torch import optim

    n_blocks = args.n_blocks[0]
    n_neurons_body = args.n_neurons_body[0]
    n_layers_head = args.n_layers_head[0]
    n_neurons_head = args.n_neurons_head[0]

    device = args.device[0]
    targets = args.targets
    network_type = args.type[0]
    batch_size = args.batch_size[0]

    #
    # Load training data.
    #

    dataset_factory = GPROF_NN_2D_Dataset
    normalizer_path = (GPROF_NN_DATA_PATH /
                       f"normalizer_{sensor.name.lower()}.pckl")
    normalizer = Normalizer.load(normalizer_path)
    kwargs = {
        "batch_size": batch_size,
        "normalizer": normalizer,
        "targets": targets,
        "augment": True
    }
    training_data = DataFolder(
        training_data,
        dataset_factory,
        queue_size=64,
        kwargs=kwargs,
        n_workers=6)

    kwargs = {
        "batch_size": 4 * batch_size,
        "normalizer": normalizer,
        "targets": targets,
        "augment": False
    }
    validation_data = DataFolder(
        validation_data,
        dataset_factory,
        queue_size=64,
        kwargs=kwargs,
        n_workers=2
    )

    ###############################################################################
    # Prepare in- and output.
    ###############################################################################

    #
    # Create neural network model
    #

    if network_type == "drnn":
        xrnn = GPROF_NN_2D_DRNN(sensor,
                                n_blocks,
                                n_neurons_body,
                                n_layers_head,
                                n_neurons_head,
                                targets=targets)
    elif network_type == "qrnn_exp":
        transformation = {}
        for target in ALL_TARGETS:
            if target in PROFILE_NAMES:
                transformation[target] = None
            else:
                transformation[target] = LogLinear()
        xrnn = GPROF_NN_2D_QRNN(sensor,
                                n_blocks,
                                n_neurons_body,
                                n_layers_head,
                                n_neurons_head,
                                transformation=transformation,
                                targets=targets)
    else:
        xrnn = GPROF_NN_2D_QRNN(sensor,
                                n_blocks,
                                n_neurons_body,
                                n_layers_head,
                                n_neurons_head,
                                targets=targets)
    model = xrnn.model
    model.normalizer = normalizer

    ###############################################################################
    # Run the training.
    ###############################################################################

    n_epochs = 70
    logger = TensorBoardLogger(n_epochs)
    logger.set_attributes({
        "n_blocks": n_blocks,
        "n_neurons_body": n_neurons_body,
        "n_layers_head": n_layers_head,
        "n_neurons_head": n_neurons_head,
        "targets": ", ".join(targets),
        "type": network_type,
        "optimizer": "adam"
        })

    metrics = ["MeanSquaredError", "Bias", "CalibrationPlot", "CRPS"]
    scatter_plot = ScatterPlot(log_scale=True)
    metrics.append(scatter_plot)

    n_epochs = 10
    optimizer = optim.Adam(model.parameters(), lr=0.001)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, n_epochs)
    xrnn.train(training_data=training_data,
               validation_data=validation_data,
               n_epochs=n_epochs,
               optimizer=optimizer,
               scheduler=scheduler,
               logger=logger,
               metrics=metrics,
               device=device,
               mask=-9999)
    xrnn.save(output)

    n_epochs = 20
    optimizer = optim.Adam(model.parameters(), lr=0.0005)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, n_epochs)
    xrnn.train(training_data=training_data,
               validation_data=validation_data,
               n_epochs=n_epochs,
               optimizer=optimizer,
               scheduler=scheduler,
               logger=logger,
               metrics=metrics,
               device=device,
               mask=-9999)
    xrnn.save(output)

    n_epochs = 20
    optimizer = optim.Adam(model.parameters(), lr=0.0005)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, n_epochs)
    xrnn.train(training_data=training_data,
               validation_data=validation_data,
               n_epochs=n_epochs,
               optimizer=optimizer,
               scheduler=scheduler,
               logger=logger,
               metrics=metrics,
               device=device,
               mask=-9999)
    xrnn.save(output)

    n_epochs = 20
    optimizer = optim.Adam(model.parameters(), lr=0.0001)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, n_epochs)
    xrnn.train(training_data=training_data,
               validation_data=validation_data,
               n_epochs=n_epochs,
               optimizer=optimizer,
               scheduler=scheduler,
               logger=logger,
               metrics=metrics,
               device=device,
               mask=-9999)
    xrnn.save(output)
