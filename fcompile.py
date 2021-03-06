#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import re
import sys
import hashlib
import json
import os
import time
from collections import defaultdict
from argparse import ArgumentParser
from pathlib import Path
from itertools import product, islice
from math import nan
import asyncio
from asyncio import Queue, PriorityQueue

from typing import ( # noqa
    Dict, Any, DefaultDict, List, Iterator, Sequence, IO, Set, Tuple, Union,
    NamedTuple, NewType, Optional, TYPE_CHECKING, cast, Generator, TypeVar,
    Iterable
)

_T = TypeVar('_T')

Module = NewType('Module', str)
Source = NewType('Source', str)
Filename = Union[str, Source]
Hash = NewType('Hash', str)
Args = NewType('Args', Tuple[str, ...])

DEBUG = os.environ.get('DEBUG')
cachefile = '_fcompile_cache.json'


def parse_modules(f: IO[str]) -> Tuple[int, List[Module], Set[Module]]:
    """Parse a Fortran source file.

    Returns the number of lines, a list of modules defined in the file, and a
    set of modules that the file imports.
    """
    defined = []
    used = set()
    nlines = 0
    for line in f:
        nlines += 1
        line = line.lstrip()
        if not line:
            continue
        if line[0] == '!':
            continue
        word = line.split(' ', 1)[0].lower()
        if word == 'module':
            module = re.match(r'module\s+(\w+)\s*', line, re.IGNORECASE).group(1).lower()
            if module != 'procedure':
                defined.append(Module(module))
        elif word == 'use':
            module = re.match(r'use\s+(\w+)\s*', line, re.IGNORECASE).group(1).lower()
            used.add(Module(module))
    used.difference_update(defined)
    return nlines, defined, used


def get_priority(tree: Dict[_T, List[_T]]) -> Dict[_T, int]:
    """Calculate node priorities in a one-directional graph.

    The priority of a node is equal to 1 plus the sum of priorities of its
    children.
    """
    priority: Dict[_T, int] = {}

    def getset(node: _T) -> int:
        try:
            return priority[node]
        except KeyError:
            pass
        p = 1 + sum(getset(n) for n in tree[node])
        priority[node] = p
        return p
    for node in tree:
        getset(node)
    return priority


def get_ancestors(tree: Dict[_T, Set[_T]]) -> Dict[_T, Set[_T]]:
    """Obtain all ancestors for each node in a one-directional graph.

    Ancestors of a node are all its children and their ancestors.
    """
    ancestors: Dict[_T, Set[_T]] = {}

    def getset(node: _T) -> Set[_T]:
        try:
            return ancestors[node]
        except KeyError:
            pass
        ancs = set(tree[node])
        for n in tree[node]:
            ancs.update(getset(n))
        ancestors[node] = ancs
        return ancs
    for node in tree:
        getset(node)
    return ancestors


def get_hash(path: Path, tpl: Tuple = None) -> Hash:
    """Calculate SHA-1 hash of a file and optionally a tuple."""
    h = hashlib.new('sha1')
    if tpl is not None:
        h.update(repr(tpl).encode())
    with path.open('rb') as f:
        h.update(f.read())
    return Hash(h.hexdigest())


class TaskTree(NamedTuple):
    """A dependency tree of Fortran source files.

    Attributes:
    src_mods -- Maps source files to modules defined in them.
    mod_uses -- Maps modules to source files that import them.
    hashes -- Maps source and module files to their hashes.
    line_nums -- Maps source files to numbers of line.
    priority -- Maps source files to priorities.
    ancestors -- Maps source files to sets of ancestors.
    """
    src_mods: Dict[Source, List[Module]]
    mod_uses: DefaultDict[Module, List[Source]]
    hashes: Dict[Filename, Hash]
    line_nums: Dict[Source, int]
    priority: Dict[Source, int]
    ancestors: Dict[Source, Set[Source]]


class Task(NamedTuple):
    """A single compilation task.

    Attributes:
    source -- Path to the Fortran source file.
    args -- Arguments to run to compile the task. The source file will be
        appended. Example: ('gfortran', '-c', '-o', 'build/a.o')
    includes -- A list of Paths of include directories.
    """
    source: Path
    args: Args
    includes: List[Path]


class ModuleMultipleDefined(Exception):
    """Raised when a module is defined in multiple source files."""


class ModuleNotDefined(Exception):
    """Raised when a module used in some source file is not defined."""


def get_tree(tasks: Dict[Source, Task]) -> TaskTree:
    """Return a task tree given a dict of tasks."""
    src_mods: Dict[Source, List[Module]] = {}
    mod_defs: Dict[Module, Source] = {}
    src_deps: Dict[Source, Set[Module]] = {}
    hashes: Dict[Filename, Hash] = {}
    line_nums: Dict[Source, int] = {}
    for src, task in tasks.items():
        with task.source.open() as f:
            nlines, defined, used = parse_modules(f)
        src_mods[src] = defined
        src_deps[src] = used
        line_nums[src] = nlines
        hashes[src] = get_hash(task.source, task.args)
        for module in defined:
            if module in mod_defs:
                raise ModuleMultipleDefined(module, [mod_defs[module], src])
            else:
                mod_defs[module] = src
    for used in src_deps.values():
        used.discard(Module('iso_c_binding'))
    if Module('mpi') not in mod_defs:
        for used in src_deps.values():
            used.discard(Module('mpi'))
    for src, task in tasks.items():
        if task.includes:
            incdir: Path
            for incdir, module in product(task.includes, src_deps[src]):  # type: ignore
                if (incdir/(module + '.mod')).exists():
                    src_deps[src].remove(module)
    for mod in set(mod for mods in src_deps.values() for mod in mods):
        if mod not in mod_defs:
            raise ModuleNotDefined(mod)
    mod_uses: DefaultDict[Module, List[Source]] = defaultdict(list)
    for src, modules in src_deps.items():
        for module in modules:
            mod_uses[module].append(src)
    priority = get_priority({
        src: [t for m in mods for t in mod_uses[m]]
        for src, mods in src_mods.items()
    })
    ancestors = get_ancestors({
        src: set(mod_defs[m] for m in mods)
        for src, mods in src_deps.items()
    })
    return TaskTree(
        src_mods, mod_uses, hashes, line_nums, priority, ancestors
    )


def pprint(s: Any) -> None:
    """Clear a line and print."""
    sys.stdout.write('\x1b[2K\r{0}\n'.format(s))


clocks: List[Tuple[Source, float, int]] = []


def print_clocks() -> None:
    """Print clock information."""
    rows = list(islice(sorted(clocks, key=lambda x: -x[1]), 20))
    maxnamelen = max(len(r[0]) for r in rows)
    print(f'{"File":<{maxnamelen+2}}    {"Time [s]":<6}  {"Lines":<6}')
    for file, clock, nlines in rows:
        print(f'  {file:<{maxnamelen+2}}  {clock:>6.2f}  {nlines:>6}')


class CompilationError(Exception):
    """Raised when compilation ends with an error."""
    def __init__(self, source: Source, retcode: int) -> None:
        self.source = source
        self.retcode = retcode


if TYPE_CHECKING:
    TaskQueue = PriorityQueue[Tuple[int, Source, Args]]
    ResultQueue = Queue[Tuple[Source, int, float]]
else:
    TaskQueue, ResultQueue = None, None


n_running = 0


async def scheduler(tasks: Dict[Source, Task],
                    task_queue: TaskQueue,
                    result_queue: ResultQueue,
                    tree: TaskTree,
                    hashes: Dict[Filename, Hash],
                    changed_files: List[Source]) -> None:
    """Schedule tasks and handle compiled tasks."""
    start = time.time()
    n_all_lines = sum(tree.line_nums[src] for src in changed_files)
    n_lines = 0
    waiting = set(changed_files)
    scheduled: Set[Source] = set()
    while True:
        blocking = waiting | scheduled
        for src in list(waiting):
            if not (blocking & tree.ancestors[src]):
                hashes.pop(src, None)  # if compilation gets interrupted
                task_queue.put_nowait((
                    -tree.priority[src],
                    src,
                    Args(tasks[src].args + (str(tasks[src].source),))
                ))
                scheduled.add(src)
                waiting.remove(src)
        elapsed = time.time()-start
        lines_done = n_lines/n_all_lines
        eta = elapsed/(lines_done or nan)
        sys.stdout.write(
            f' Progress: {len(waiting)} waiting, {len(scheduled)} scheduled, {n_running+1} running, '
            f'{n_lines}/{n_all_lines} lines ({100*n_lines/n_all_lines:.1f}%), '
            f'Elapsed/ETA: {elapsed:.1f}/{eta:.1f} s\r'
        )
        sys.stdout.flush()
        if not blocking:
            break
        src, retcode, clock = await result_queue.get()
        if retcode != 0:
            raise CompilationError(src, retcode)
        clocks.append((src, clock, tree.line_nums[src]))
        hashes[src] = tree.hashes[src]
        n_lines += tree.line_nums[src]
        scheduled.remove(src)
        pprint(f'Compiled {src}.')
        for mod in tree.src_mods[src]:
            modfile = mod + '.mod'
            modhash = get_hash(Path(modfile))
            if modhash != hashes.get(modfile):
                hashes[modfile] = modhash
                for src in tree.mod_uses[mod]:
                    assert src not in scheduled
                    hashes.pop(src, None)
                    if src not in waiting:
                        n_all_lines += tree.line_nums[src]
                        waiting.add(src)


async def worker(task_queue: TaskQueue, result_queue: ResultQueue) -> None:
    """Compile tasks from a queue."""
    global n_running
    while True:
        _, taskname, args = await task_queue.get()
        proc = await asyncio.create_subprocess_exec(*args)
        n_running += 1
        now = time.time()
        retcode = await proc.wait()
        n_running -= 1
        result_queue.put_nowait((taskname, retcode, time.time()-now))


def build(tasks: Dict[Source, Task], dry: bool = False, njobs: int = 1) -> None:
    """Build tasks.

    This is the main entry point.
    """
    print('Scanning files...')
    tree = get_tree(tasks)
    try:
        with open(cachefile) as f:
            hashes: Dict[Filename, Hash] = json.load(f)['hashes']
    except (ValueError, FileNotFoundError):
        hashes = {}
    changed_files = [src for src in tasks if tree.hashes[src] != hashes.get(src)]
    print(f'Changed files: {len(changed_files)}/{len(tasks)}.')
    if not changed_files or dry:
        return
    task_queue: TaskQueue = PriorityQueue()
    result_queue: ResultQueue = Queue()
    loop = asyncio.get_event_loop()
    workers = [
        loop.create_task(worker(task_queue, result_queue))
        for _ in range(njobs)
    ]
    try:
        loop.run_until_complete(
            scheduler(tasks, task_queue, result_queue, tree, hashes, changed_files)
        )
    except CompilationError as e:
        print(f'error: Compilation of {e.source} returned {e.retcode}.')
        raise
    except Exception as e:
        print()
        raise
    else:
        print()
    finally:
        for tsk in workers:
            tsk.cancel()
        with open(cachefile, 'w') as f:
            json.dump({'hashes': hashes}, f)
        if DEBUG:
            print_clocks()


def read_tasks(f: IO[str] = sys.stdin) -> Dict[Source, Task]:
    """Read tasks from a file."""
    return {
        Source(k): Task(
            Path(t['source']),
            Args(tuple(t['args'])),
            [Path(inc) for inc in t.get('includes', [])]
        )
        for k, t in json.load(f).items()
    }


def parse_cli() -> Dict[str, Any]:
    """Handle the command-line interface."""
    cpu_count = os.cpu_count()//2 or 1  # type: ignore
    parser = ArgumentParser(usage='fcompile [options] <CONFIG.json')
    arg = parser.add_argument
    arg('-j', '--jobs', type=int, default=cpu_count, dest='njobs',
        help=f'number of parallel workers [default: {cpu_count}]')
    arg('--dry', action='store_true', help='scan files and exit')
    return vars(parser.parse_args())


if __name__ == '__main__':
    try:
        build(read_tasks(), **parse_cli())
    except (KeyboardInterrupt, CompilationError):
        raise SystemExit(1)
