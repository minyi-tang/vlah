"""Problem protocol (spec 11.2), shared statistics, and path-based problem loading.

Built-ins load from a checkout or distribution; explicit paths select worker copies.
"""

from __future__ import annotations

import atexit
import contextvars
import hashlib
import importlib
import importlib.util
import math
import multiprocessing
import os
import random
import shutil
import stat
import sys
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
IS_CHECKOUT = (PACKAGE_ROOT / "pyproject.toml").is_file()
PROBLEMS_ROOT = (REPO_ROOT if IS_CHECKOUT else PACKAGE_ROOT) / "problems"
# Run state (slow-eval data and system key, runs, precheck and extras output): gitignored, never on workers.
STATE_DIR = Path(
    os.environ.get("TB_STATE_DIR")
    or (
        REPO_ROOT / "private" / "testbed" / "var"
        if IS_CHECKOUT
        else Path.home() / ".vlah-private" / "testbed" / "var"
    )
)


def private_dirs() -> list[Path]:
    """Where hidden material may live (issue #10: never committed), in lookup order."""
    if (
        os.environ.get("TB_PRIVATE") == "none"
    ):  # behave as on CI or a worker: no hidden material
        return []
    out = [Path(os.environ["TB_PRIVATE"])] if os.environ.get("TB_PRIVATE") else []
    if IS_CHECKOUT:
        out.append(REPO_ROOT / "private" / "testbed")
    else:
        pass
    return out + [Path.home() / ".vlah-private" / "testbed"]


def hidden_root(problem_name: str) -> Path | None:
    """The problem's hidden material (seeds, reference, probes), or None where it is absent (CI, workers)."""
    for d in private_dirs():
        if (d / problem_name).is_dir():
            return d / problem_name
    return None


def mount_hidden(package: str, root: Path, dotted: str):
    """Import root/<a>/<b>.py as <package>.<a>.<b>, so hidden modules can use the problem's relative imports."""
    parts = dotted.split(".")
    for i in range(1, len(parts) + 1):
        name = package + "." + ".".join(parts[:i])
        if name in sys.modules:
            continue
        path = root.joinpath(*parts[:i])
        if path.is_dir():
            spec = importlib.util.spec_from_file_location(
                name, path / "__init__.py", submodule_search_locations=[str(path)]
            )
        else:
            spec = importlib.util.spec_from_file_location(name, path.with_suffix(".py"))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load hidden module {name} from {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return importlib.import_module(package + "." + dotted)


Result = dict[
    str, Any
]  # {'score': float|None, 'per_item': [{'id','value'}], 'extra': dict}
Agg = dict[str, Any]  # {'score','ci','n','per_seed','per_item','extra'}
Tier = Literal["fast", "slow"]
DATASETS = ("public", "dev", "audit", "judge")


class Problem(Protocol):
    name: str
    ingredients: list[str]
    public_seeds: list[int]

    def evaluate(
        self, candidate_dir: Path, tier: Tier, seed: int, timeout: bool = True
    ) -> Result: ...
    def aggregate(self, results: list[Result]) -> Agg: ...
    def constraint_ok(self, agg: Agg, baseline_agg: Agg) -> bool: ...
    def probes(self, candidate_dir: Path) -> dict[str, bool]: ...

    # Extensions used by the slow-eval service (not part of the worker-facing protocol).
    def seeds(self, dataset: str) -> list[int]: ...
    def evaluate_many(
        self, candidate_dir: Path, seeds: list[int], tier: Tier, timeout: bool = True
    ) -> list[Result]: ...
    def baseline_dir(self) -> Path: ...
    def reference_dir(self) -> Path: ...
    def knobs(self) -> dict: ...
    def validate_candidate(self, candidate_dir: Path) -> str | None: ...


# ---------------------------------------------------------------- loader

_LOADED: dict[str, Any] = {}


def resolve_problem_dir(name_or_path: str | Path) -> Path:
    p = Path(name_or_path)
    if p.is_dir() and (p / "problem.py").exists():
        return p.resolve()
    q = PROBLEMS_ROOT / str(name_or_path)
    if q.is_dir():
        return q.resolve()
    raise FileNotFoundError(f"problem not found: {name_or_path}")


def load_problem(name_or_path: str | Path) -> Problem:
    root = resolve_problem_dir(name_or_path)
    key = str(root)
    if key in _LOADED:
        return _LOADED[key]
    pkg = "tbproblem_" + root.name + "_" + hashlib.sha1(key.encode()).hexdigest()[:8]
    if pkg not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            pkg, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[pkg] = mod
        spec.loader.exec_module(mod)
    prob = importlib.import_module(pkg + ".problem").load(root)
    _LOADED[key] = prob
    return prob


# ---------------------------------------------------------------- stats


def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


def sd(xs):
    xs = list(xs)
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def se(xs):
    xs = list(xs)
    return sd(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0


def bootstrap_ci(
    xs, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0
) -> list[float]:
    """Percentile bootstrap CI of the mean; deterministic (fixed RNG)."""
    xs = list(xs)
    if not xs:
        return [float("nan"), float("nan")]
    if len(xs) == 1:
        return [xs[0], xs[0]]
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(
        sum(xs[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_boot)
    )
    lo = means[int(math.floor(alpha / 2 * n_boot))]
    hi = means[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]
    return [lo, hi]


def paired(cand_per_seed, base_per_seed) -> dict:
    """Per-seed paired difference cand - base: {mean, se, n}."""
    d = [a - b for a, b in zip(cand_per_seed, base_per_seed)]
    return {"mean": mean(d) if d else 0.0, "se": se(d), "n": len(d)}


def percentile(sorted_xs, q: float) -> float:
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_xs:
        return 0.0
    k = max(0, min(len(sorted_xs) - 1, int(math.ceil(q / 100.0 * len(sorted_xs))) - 1))
    return sorted_xs[k]


# ---------------------------------------------------------------- candidate hashing

MANIFEST_FILE_NAME = "manifest.yaml"


def iter_candidate_files(candidate_dir: Path):
    """Payload files as (POSIX relative path, path), sorted by the path string (TB 11.2, issue #10).
    Skipped: anything in a __pycache__ directory, *.pyc, and manifest.yaml at the top level only.
    """
    candidate_dir = Path(candidate_dir)
    selected = []
    for p in candidate_dir.rglob("*"):
        rel = p.relative_to(candidate_dir)
        if (
            not p.is_file()
            or "__pycache__" in rel.parts
            or p.suffix == ".pyc"
            or rel.as_posix() == MANIFEST_FILE_NAME
        ):
            continue
        selected.append((rel.as_posix(), p))
    return sorted(selected)


def copy_writable_file(source: Path, destination: Path) -> None:
    """Copy a template into a writable workspace, preserving its executable bits."""
    shutil.copy2(source, destination)
    destination.chmod(stat.S_IMODE(destination.stat().st_mode) | stat.S_IWUSR)


def content_hash(candidate_dir: Path) -> str:
    """Hash of the payload alone (no target): identifies content for caches and quarantine."""
    h = hashlib.sha256()
    for rel, p in iter_candidate_files(candidate_dir):
        h.update(rel.encode("utf-8") + b"\0" + p.read_bytes())
    return h.hexdigest()


def candidate_hash(candidate_dir: Path, target: str = "baseline@0") -> str:
    """TB 11.2 / D73, byte for byte the herd's vlah.core.types.candidates.candidate_hash: for each payload
    file, UTF-8 path + NUL + raw bytes; then the UTF-8 target baseline id; lowercase SHA-256 hex.
    """
    h = hashlib.sha256()
    for rel, p in iter_candidate_files(candidate_dir):
        h.update(rel.encode("utf-8") + b"\0" + p.read_bytes())
    h.update(target.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------- process pools (spawn-safe)

# One 24 h public seed is six site simulations, so six workers run one fast request at full
# width (issue #113).
FAST_POOL_WORKERS = 6
# Which pool evaluate_many submits to. Problems call pool() with no argument, so the slow-eval
# service switches a request onto the small fast pool with the fast_pool() context manager
# instead of threading a pool name through the problem protocol.
POOL_NAME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "TB_POOL_NAME", default="slow"
)
POOLS: dict[str, ProcessPoolExecutor] = {}


def call_problem(root: str, method: str, *args, **kwargs):
    """Top-level trampoline so pool workers can load a path-loaded problem."""
    return getattr(load_problem(root), method)(*args, **kwargs)


def pool() -> ProcessPoolExecutor:
    """The process pool named by POOL_NAME: "slow" (TB_WORKERS or every core) by default, "fast"
    (at most FAST_POOL_WORKERS) inside fast_pool(). A fast request must not queue behind a
    slow batch of 120 site tasks, so the two never share an executor."""
    name = POOL_NAME.get()
    if name not in POOLS:
        cpu_count = os.cpu_count() or 4
        if name == "fast":
            worker_count = min(FAST_POOL_WORKERS, cpu_count)
        else:
            worker_count = int(os.environ.get("TB_WORKERS", "0")) or cpu_count
        POOLS[name] = ProcessPoolExecutor(
            max_workers=worker_count,
            mp_context=multiprocessing.get_context("forkserver"),
        )
        atexit.register(POOLS[name].shutdown, wait=False, cancel_futures=True)
    else:
        pass
    return POOLS[name]


@contextmanager
def fast_pool() -> Iterator[None]:
    """Run the problem's evaluations in this thread on the fast pool for the duration of the block."""
    token = POOL_NAME.set("fast")
    try:
        yield
    finally:
        POOL_NAME.reset(token)
