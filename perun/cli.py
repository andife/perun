"""Command line api definition.

Uses click https://click.palletsprojects.com/en/8.1.x/ to manage complex cmdline configurations.
"""
from pathlib import Path
from typing import Optional, List
import click

from perun.storage import ExperimentStorage, LocalStorage


@click.group()
def cli():
    """Entry point for the perun command line interface."""


@cli.command()
@click.argument("exp_hdf5", type=click.Path(exists=True))
def postprocess(exp_hdf5: str):
    """
    Apply post-processing to EXP_HDF5 experiment file.

    EXP_HDF5 is an hdf5 file generated by perun after monitoring a script, containing data gathered from hardware devices.
    """
    from mpi4py import MPI
    import perun

    expPath = Path(exp_hdf5)
    expStrg = ExperimentStorage(expPath, MPI.COMM_WORLD)
    perun.postprocessing(expStrg)
    expStrg.close()


@cli.command()
@click.argument("exp_hdf5", type=click.Path(exists=True))
@click.option(
    "-f",
    "--format",
    type=click.Choice(["txt", "yaml", "yml", "json"]),
    default="txt",
    help="report print format",
)
def report(exp_hdf5: str, format: str):
    """Print consumption report from EXP_HDF5 on the command line on the desired format.

    EXP_HDF5 is an hdf5 file generated by perun after monitoring a script, containing data gathered from hardware devices.
    """
    from mpi4py import MPI
    import perun

    expPath = Path(exp_hdf5)
    expStrg = ExperimentStorage(expPath, MPI.COMM_WORLD)
    print(perun.report(expStrg, format=format))
    expStrg.close()


@cli.command(context_settings={"ignore_unknown_options": True})
@click.argument("script", type=click.Path(exists=True))
@click.argument("script_args", nargs=-1)
@click.option(
    "-f", "--frequency", type=float, default=1.0, help="sampling frequency (in Hz)"
)
@click.option(
    "--format",
    type=click.Choice(["txt", "yaml", "yml", "json"]),
    default="txt",
    help="report print format",
)
@click.option(
    "-o",
    "--outdir",
    type=click.Path(exists=False, dir_okay=True, file_okay=False),
    default="./",
    help="experiment data output directory",
)
def monitor(
    script: str, script_args: tuple, frequency: float, outdir: str, format: str
):
    """
    Gather power consumption from hardware devices while SCRIPT [SCRIPT_ARGS] is running.

    SCRIPT is a path to the python script to monitor, run with arguments SCRIPT_ARGS.
    """
    import perun
    from perun import log
    import sys
    from mpi4py import MPI
    from multiprocessing import Event, Process, Queue

    comm = MPI.COMM_WORLD
    start_event = Event()
    stop_event = Event()

    # Setup script arguments
    filePath: Path = Path(script)
    outPath: Path = Path(outdir)
    log.debug(f"Script path: {filePath}")
    argIndex = sys.argv.index(str(filePath))
    sys.argv = sys.argv[argIndex:]
    log.debug(f"Script args: { sys.argv }")

    # Get node devices
    log.debug(f"Backends: {perun.backends}")
    lDeviceIds: List[str] = perun.getDeviceConfiguration(comm, perun.backends)

    for backend in perun.backends:
        backend.close()

    log.debug(f"Rank {comm.rank} - lDeviceIds : {lDeviceIds}")

    # If assigned devices, start subprocess
    if len(lDeviceIds) > 0:
        queue: Queue = Queue()
        perunSP = Process(
            target=perun.perunSubprocess,
            args=[queue, start_event, stop_event, lDeviceIds, frequency],
        )
        perunSP.start()
        start_event.wait()

    # Sync everyone
    comm.barrier()

    # Start script
    try:
        with open(str(filePath), "r") as scriptFile:
            exec(scriptFile.read(), {"__name__": "__main__"})
    except Exception as e:
        log.error("Failed to open file ", filePath)
        log.error(e)
        stop_event.set()
        return

    stop_event.set()

    lStrg: Optional[LocalStorage]
    # Obtain perun subprocess results
    if len(lDeviceIds) > 0:
        perunSP.join()
        perunSP.close()
        lStrg = queue.get()
    else:
        lStrg = None

    # Sync
    comm.barrier()

    # Save raw data to hdf5

    if comm.rank == 0:
        if not outPath.exists():
            outPath.mkdir(parents=True)

    scriptName = filePath.name.replace(filePath.suffix, "")
    resultPath = outPath / f"{scriptName}.hdf5"
    log.debug(f"Result path: {resultPath}")
    expStrg = perun.ExperimentStorage(resultPath, comm)
    if lStrg:
        log.debug("Creating new experiment")
        expId = expStrg.addExperimentRun(lStrg.toDict())
        expStrg.saveDeviceData(expId, lStrg)
    else:
        expId = expStrg.addExperimentRun(None)

    # Post post-process
    comm.barrier()
    perun.postprocessing(expStorage=expStrg)
    comm.barrier()
    if comm.rank == 0:
        print(perun.report(expStrg, expIdx=expId, format=format))

    comm.barrier()
    expStrg.close()


if __name__ == "__main__":
    cli()
