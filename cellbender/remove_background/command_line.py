"""Command-line tool functionality for remove-background."""

import torch
from cellbender.remove_background.data.dataset import Dataset
import cellbender.remove_background.data.transform as transform
from cellbender.remove_background.train import run_inference
import cellbender.remove_background.tests.test
from cellbender.command_line import AbstractCLI

import argparse
import logging
import os
import sys
from datetime import datetime


class CLI(AbstractCLI):
    """CLI implements AbstractCLI from the cellbender package."""

    def __init__(self):
        self.name = 'remove-background'
        self.args = None

    def get_name(self) -> str:
        return self.name

    def add_subparser_args(self, subparsers: argparse) -> argparse:
        """Add tool-specific arguments for remove-background.

        Args:
            subparsers: Parser object before addition of arguments specific to
                remove-background.

        Returns:
            parser: Parser object with additional parameters.

        """

        subparser = subparsers.add_parser(self.name,
                                          description="Remove background RNA "
                                                      "from count matrix.",
                                          help="Remove background ambient RNA "
                                               "and barcode-swapped reads from "
                                               "a count matrix, producing a "
                                               "new count matrix and "
                                               "determining which barcodes "
                                               "contain real cells.")

        subparser.add_argument("--input", nargs=None, type=str,
                               dest='input_file', default=None,
                               help="Data file on which to run tool, as either "
                                    "_raw_gene_barcode_matrices_h5.h5 file, or "
                                    "as the path containing the raw "
                                    "matrix.mtx, barcodes.tsv, and genes.tsv "
                                    "files.")
        subparser.add_argument("--output", nargs=None, type=str,
                               dest='output_file', default=None,
                               help="Name of output file, which will be HDF5.")
        subparser.add_argument("--expected_cells", nargs=None, type=int,
                               default=None,
                               dest="expected_cell_count",
                               help="Number of cells expected in the dataset.")
        subparser.add_argument("--additional_barcodes", nargs=None, type=int,
                               default=None, dest="additional_barcodes",
                               help="The number of additional droplets after "
                                    "the number of expected_cells "
                                    "that have any chance of containing cells.  "
                                    "The largest (expected_cells + "
                                    "additional_barcodes) droplets will have "
                                    "their cell probabilities inferred as an "
                                    "output.")
        subparser.add_argument("--model", nargs=None, type=str, default="full",
                               choices=["simple", "ambient",
                                        "swapping", "full"],
                               dest="model",
                               help="Which model is being used for count data. "
                                    " 'simple' does not model background or "
                                    "ambient RNA.  'ambient' assumes "
                                    "background RNA is incorporated into "
                                    "droplets.  'swapping' assumes background "
                                    "RNA comes from barcode swapping. "
                                    "'full' uses a combined ambient and "
                                    "swapping model.  Defaults to 'full'.")
        subparser.add_argument("--epochs", nargs=None, type=int, default=150,
                               dest="epochs",
                               help="Number of epochs to train.")
        subparser.add_argument("--cuda",
                               dest="use_cuda", action="store_true",
                               help="Including the flag --cuda will run the "
                                    "inference on a GPU.")
        subparser.add_argument("--low_count_threshold", type=int, default=15,
                               dest="low_count_threshold",
                               help="Droplets with UMI counts below this "
                                    "number are completely excluded from the "
                                    "analysis.  This can help identify the "
                                    "correct prior for empty droplet counts "
                                    "in the rare case where empty counts "
                                    "are extremely high (over 200).")
        subparser.add_argument("--training_fraction", nargs=None,
                               type=float, default=1.,
                               dest="training_fraction",
                               help="Fraction of the data to use for training.")
        subparser.add_argument("--z_dim", type=int, default=20,
                               dest="z_dim",
                               help="Dimension of latent variable z.")
        subparser.add_argument("--z_layers", nargs="+", type=int, default=[500],
                               dest="z_hidden_dims",
                               help="Dimension of hidden layers in the encoder "
                                    "for z.")
        subparser.add_argument("--d_layers", nargs="+", type=int,
                               default=[5, 2, 2],
                               dest="d_hidden_dims",
                               help="Dimension of hidden layers in the encoder "
                                    "for d.")
        subparser.add_argument("--p_layers", nargs="+", type=int,
                               default=[100, 10],
                               dest="p_hidden_dims",
                               help="Dimension of hidden layers in the encoder "
                                    "for p.")
        subparser.add_argument("--empty_drop_training_fraction",
                               type=float, default=0.5, nargs=None,
                               dest="fraction_empties",
                               help="Training detail: the fraction of the "
                                    "training data each epoch "
                                    "that is drawn (randomly sampled) from "
                                    "surely empty droplets.")
        subparser.add_argument("--blacklist_genes", nargs="+", type=int,
                               default=[],
                               dest="blacklisted_genes",
                               help="Integer indices of genes to ignore "
                                    "entirely.  In the output count matrix, "
                                    "the counts for these genes will be set "
                                    "to zero.")
        subparser.add_argument("--transform_counts", nargs=None, type=str,
                               default="identity",
                               choices=["identity", "log", "sqrt"],
                               dest="transform",
                               help="Transformation to apply to count data "
                                    "prior to running inference.  This should "
                                    "be set to 'identity'.")
        subparser.add_argument("--learning_rate", nargs=None,
                               type=float, default=1e-3,
                               dest="learning_rate",
                               help="Training detail: learning rate for "
                                    "inference (probably "
                                    "do not exceed 1e-3).")
        subparser.add_argument("--no_jit",
                               dest="use_jit", action="store_false",
                               help="Including the flag --no_jit will opt not "
                                    "to use a JIT compiler.  The JIT compiler "
                                    "provides a speed-up.")
        subparser.add_argument("--test",
                               dest="test", action="store_true",
                               help="Including the flag --test will run tests "
                                    "only, and disregard other input "
                                    "parameters.")

        return subparsers

    def validate_args(self, args):
        """Validate parsed arguments."""

        # If running tests, skip the rest of the validation.
        if args.test:
            self.args = args
            return args

        # Ensure that if there's a tilde for $HOME in the file path, it works.
        try:
            args.input_file = os.path.expanduser(args.input_file)
            args.output_file = os.path.expanduser(args.output_file)
        except TypeError:
            print("Please specify input and output files.")

        # Ensure write access to the save directory.
        file_dir, _ = os.path.split(args.output_file)  # Get the output directory
        if file_dir:
            assert os.access(file_dir, os.W_OK), \
                f"Cannot write to specified output directory {file_dir}"

        # If cell counts are specified, it should be positive.
        if args.expected_cell_count is not None:
            assert args.expected_cell_count > 0, \
                "expected_cells must be an integer greater than zero."

        # If additional barcodes are specified, they must be specified for
        # all files.
        if args.additional_barcodes is not None:
            assert args.additional_barcodes > 0, \
                "additional_barcodes must be an integer greater than zero."

        assert (args.fraction_empties > 0) and (args.fraction_empties < 1), \
            "fraction_empties must be between 0 and 1, exclusive.  This is " \
            "the fraction of each minibatch that is composed of empty droplets."

        assert args.learning_rate < 0.1, "learning_rate must be < 0.1"
        assert args.learning_rate > 0, "learning_rate must be > 0"

        assert args.training_fraction <= 1.0, "training_fraction must be <= 1"
        assert args.training_fraction > 0.0, "training_fraction must be > 0"

        # If cuda is requested, make sure it is available.
        if args.use_cuda:
            assert torch.cuda.is_available(), "Trying to use CUDA, " \
                                              "but CUDA is not available."
        else:
            # Warn the user in case the CUDA flag was forgotten by mistake.
            if torch.cuda.is_available():
                sys.stdout.write("Warning: CUDA is available, but will not be "
                                 "used.  Use the flag --cuda for "
                                 "significant speed-ups.\n\n")
                sys.stdout.flush()  # Write immediately

        # Ensure that z_hidden_dims are in encoder order.
        # (The same dimensions are used in reverse order for the decoder.)
        args.z_hidden_dims = sorted(args.z_hidden_dims, reverse=True)

        self.args = args

        return args

    def run(self, args):
        """Run the main tool functionality on parsed arguments."""

        # Run the tool.
        main(args)


def run_remove_background(args):
    """The full script for the command line tool to remove background RNA.

    Args:
        args: Inputs from the command line, already parsed using argparse.

    Note: Returns nothing, but writes output to a file(s) specified from
        command line.

    """

    # Set up the count data transformation.
    if args.transform == "identity":
        trans = transform.IdentityTransform()
    elif args.transform == "log":
        trans = transform.LogTransform(scale_factor=1.)
    elif args.transform == "sqrt":
        trans = transform.SqrtTransform(scale_factor=1.)
    else:
        raise NotImplementedError(f"transform_counts was set to "
                                  f"{args.transform}, "
                                  f"which is not implemented.")

    # Load dataset, run inference, and write the output to a file.

    # Send logging messages to stdout as well as a log file.
    file_dir, file_base = os.path.split(args.output_file)
    file_name = os.path.splitext(os.path.basename(file_base))[0]
    log_file = os.path.join(file_dir, file_name + ".log")
    logging.basicConfig(level=logging.INFO,
                        format="cellbender:remove-background: %(message)s",
                        filename=log_file,
                        filemode="w")
    console = logging.StreamHandler()
    formatter = logging.Formatter("cellbender:remove-background: "
                                  "%(message)s")
    console.setFormatter(formatter)  # Use the same format for stdout.
    logging.getLogger('').addHandler(console)  # Log to stdout and a file.

    # Log the command as typed by user.
    logging.info("Command:\n" + ' '.join(['cellbender', 'remove-background']
                                         + sys.argv[2:]))

    # Log the start time.
    logging.info(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    logging.info("Running remove-background")

    # Load data from file and choose barcodes and genes to analyze.
    try:
        dataset_obj = Dataset(transformation=trans,
                              input_file=args.input_file,
                              expected_cell_count=
                              args.expected_cell_count,
                              num_transition_barcodes=
                              args.additional_barcodes,
                              fraction_empties=args.fraction_empties,
                              model_name=args.model,
                              gene_blacklist=args.blacklisted_genes,
                              low_count_threshold=args.low_count_threshold)
    except OSError:
        logging.error(f"OSError: Unable to open file {args.input_file}.")
        return

    # Instantiate latent variable model and run full inference procedure.
    inferred_model = run_inference(dataset_obj, args)

    # Write outputs to file.
    try:
        dataset_obj.save_to_output_file(args.output_file,
                                        inferred_model,
                                        save_plots=True)

        logging.info("Completed remove-background.")
        logging.info(datetime.now().strftime('%Y-%m-%d %H:%M:%S\n'))

    # The exception allows user to end inference prematurely with CTRL-C.
    except KeyboardInterrupt:

        # If partial output has been saved, delete it.
        full_file = args.output_file

        # Name of the filtered (cells only) file.
        file_dir, file_base = os.path.split(full_file)
        file_name = os.path.splitext(os.path.basename(file_base))[0]
        filtered_file = os.path.join(file_dir,
                                     file_name + "_filtered.h5")

        if os.path.exists(full_file):
            os.remove(full_file)

        if os.path.exists(filtered_file):
            os.remove(filtered_file)

        logging.info("Keyboard interrupt.  Terminated without saving.\n")


def main(args):
    """Take command-line input, parse arguments, and run tests or tool."""

    # Figure out whether we are just running tests.
    testing = args.test

    if testing:

        # Run tests.
        cellbender.remove_background.tests.test.main()

    else:

        # Run the full tool.
        run_remove_background(args)
