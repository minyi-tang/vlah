"""Write the outputs of one simulated scheduling sweep: job traces, state arrays, compressed results, a log.

Every output is seeded except the run log, which carries wall-clock times and a random run id the
way real tool output does, so replaying the command that wrote it does not reproduce it.
"""

import argparse
import time
import uuid
from pathlib import Path

import numpy as np
import pandas as pd

BYTES_PER_MEGABYTE = 1_000_000
TRACE_SHARE = 0.60
STATE_SHARE = 0.25
COMPRESSED_RESULT_SHARE = 0.10
FILES_PER_KIND = 8
SITE_COUNT = 6
TRACE_ROWS_PER_CHUNK = 200_000
FLOAT32_BYTES = 4


def write_traces(
    output_directory: Path, total_bytes: int, generator: np.random.Generator
) -> None:
    bytes_per_file = total_bytes // FILES_PER_KIND
    for file_index in range(FILES_PER_KIND):
        trace_path = output_directory / f"trace_{file_index:02d}.csv"
        first_job_id = 0
        with trace_path.open("w") as trace_file:
            while trace_file.tell() < bytes_per_file:
                jobs = pd.DataFrame(
                    {
                        "job_id": np.arange(
                            first_job_id, first_job_id + TRACE_ROWS_PER_CHUNK
                        ),
                        "site": generator.integers(0, SITE_COUNT, TRACE_ROWS_PER_CHUNK),
                        "submit_seconds": np.sort(
                            generator.uniform(0, 172_800, TRACE_ROWS_PER_CHUNK)
                        ).round(3),
                        "cores": generator.choice(
                            [1, 2, 4, 8, 16, 32], TRACE_ROWS_PER_CHUNK
                        ),
                        "runtime_seconds": generator.lognormal(
                            6, 1.5, TRACE_ROWS_PER_CHUNK
                        ).round(1),
                        "wait_seconds": generator.exponential(
                            900, TRACE_ROWS_PER_CHUNK
                        ).round(1),
                    }
                )
                jobs.to_csv(trace_file, header=first_job_id == 0, index=False)
                first_job_id += TRACE_ROWS_PER_CHUNK


def write_state_arrays(
    output_directory: Path, total_bytes: int, generator: np.random.Generator
) -> None:
    values_per_file = total_bytes // FILES_PER_KIND // FLOAT32_BYTES
    for file_index in range(FILES_PER_KIND):
        queue_lengths = (
            generator.standard_normal(values_per_file).cumsum().astype(np.float32)
        )
        np.save(output_directory / f"queue_lengths_{file_index:02d}.npy", queue_lengths)


def write_compressed_results(
    output_directory: Path, total_bytes: int, generator: np.random.Generator
) -> None:
    bytes_per_file = total_bytes // FILES_PER_KIND
    for file_index in range(FILES_PER_KIND):
        samples = np.frombuffer(generator.bytes(bytes_per_file), dtype=np.uint8)
        np.savez_compressed(
            output_directory / f"results_{file_index:02d}.npz", samples=samples
        )


def write_run_log(
    output_directory: Path, total_bytes: int, generator: np.random.Generator
) -> None:
    run_id = uuid.uuid4().hex
    with (output_directory / "sweep.log").open("w") as log_file:
        step = 0
        while log_file.tell() < total_bytes:
            site = int(generator.integers(0, SITE_COUNT))
            queue_length = int(generator.integers(0, 500))
            log_file.write(
                f"{time.time():.6f} run={run_id} step={step} site={site} queue_length={queue_length}\n"
            )
            step += 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument(
        "--megabytes",
        type=int,
        required=True,
        help="Approximate total size of all outputs",
    )
    parser.add_argument("--seed", type=int, required=True)
    parsed = parser.parse_args()
    total_bytes = parsed.megabytes * BYTES_PER_MEGABYTE
    log_share = 1 - TRACE_SHARE - STATE_SHARE - COMPRESSED_RESULT_SHARE
    generator = np.random.default_rng(parsed.seed)
    parsed.output_directory.mkdir(parents=True, exist_ok=True)
    write_traces(parsed.output_directory, int(total_bytes * TRACE_SHARE), generator)
    write_state_arrays(
        parsed.output_directory, int(total_bytes * STATE_SHARE), generator
    )
    write_compressed_results(
        parsed.output_directory, int(total_bytes * COMPRESSED_RESULT_SHARE), generator
    )
    write_run_log(parsed.output_directory, int(total_bytes * log_share), generator)


if __name__ == "__main__":
    main()
