#!/usr/bin/env python3
import argparse
import json
import os
import shutil

import sys
from pathlib import Path
from tempfile import mkstemp

from covest.constants import DEFAULT_K
from covest.data import sample_reads as _sample_reads, count_reads_stats
from covest.utils import run

READS_FILE_NAME_TEMPLATE = 'reads{}'
CONFIG_FILENAME = 'config.json'
HISTOGRAM_FILENAME = 'reads.hist'
RUNSCRIPT_FILENAME = 'run'


def mkdir(dest_dir, force=False):
    if not force and dest_dir.exists():
        print('Path already exists. Use --force to use it.', file=sys.stderr)
        exit(1)
    try:
        dest_dir.mkdir()
    except FileExistsError:
        pass


def get_reads_data(src_file, dest_dir, link=True):
    print('Obtaining reads file...', file=sys.stderr)
    dest = dest_dir / READS_FILE_NAME_TEMPLATE.format(src_file.suffix)

    if dest.exists():
        dest.unlink()

    src = str(src_file.resolve())
    dst = str(dest)
    if link:
        os.symlink(src, dst)
    else:
        shutil.copy2(src, dst)
    return dest


def sample_reads(reads_file, config, sample_info):
    print('Sampling reads...', file=sys.stderr)
    if len(sample_info) == 2:
        tc, gs = sample_info
        if 'reads_size' in config:
            rs = config['reads_size']
            del config['reads_size']
        else:
            _, rs = count_reads_stats(str(reads_file))
        c = rs / gs
        factor = c / tc
        print(
            'Current coverage: {c}, target coverage: {tc}, genome size: {gs}, factor: {factor}'.format(
                c=c, tc=tc, gs=gs, factor=factor
            ),
            file=sys.stderr
        )
    elif len(sample_info) == 1:
        factor = sample_info[0]
        print(
            'Factor: {factor}'.format(factor=factor),
            file=sys.stderr
        )
    else:
        print('Please specify a valid sample_info.', file=sys.stderr)
        exit(1)

    fd, rf_sampled = mkstemp(prefix='reads', suffix='.fa')
    _sample_reads(str(reads_file), rf_sampled, factor)
    os.close(fd)

    if 'r' in config:
        del config['r']
    reads_file.unlink()
    shutil.move(rf_sampled, str(reads_file))

    return reads_file, config


def generate_config(reads_file, src_config_file=None):
    try:
        with open(str(src_config_file), 'r') as f:
            config = json.load(f)
    except Exception:
        config = dict()
    config['reads'] = str(reads_file.name)
    return config


def calculate_reads_stats(reads_file, config, reads_info=None):
    if reads_info:
        read_length, reads_size = reads_info
    else:
        print('Calculating read length and size...', file=sys.stderr)
        read_length, reads_size = count_reads_stats(str(reads_file))
        print('read length: %d, size: %d' % (read_length, reads_size), file=sys.stderr)

    config.update({
        'reads_size': reads_size,
        'r': read_length,
    })
    return config


def generate_histogram(reads_file, dest_dir, config, clean=False):
    print('Generating histogram...', file=sys.stderr)
    hist_file = dest_dir / HISTOGRAM_FILENAME
    jellyfish_count = 'jellyfish count -m {k} -s 500M -t 16 -C {infile} -o {infile}.jf'
    jellyfish_hist = 'jellyfish histo {infile}.jf -o {outfile}'

    params = {
        'k': DEFAULT_K,
        'infile': reads_file,
        'outfile': hist_file,
    }
    run(jellyfish_count.format(**params), shell=True, verbose=True)
    run(jellyfish_hist.format(**params), shell=True, verbose=True)

    config['k'] = DEFAULT_K
    config['hist'] = hist_file.name
    if clean:
        Path('{}.jf'.format(reads_file)).unlink()
    return hist_file, config


def create_run_script(run_script_filename, dest_dir, link=True):
    if run_script_filename.exists():
        dest = dest_dir / RUNSCRIPT_FILENAME
        if dest.exists():
            dest.unlink()
        src = str(run_script_filename.resolve())
        dst = str(dest)
        if link:
            os.symlink(src, dst)
        else:
            shutil.copy2(src, dst)
    else:
        print('File does not exist: {}'.format(run_script_filename), file=sys.stderr)
        exit(2)


def write_config(config, dest_dir):
    with open(str(dest_dir / CONFIG_FILENAME), 'w') as f:
        json.dump(config, f)


def pipeline(src_file, dest_dir, link=True, force=False, run_script_filename=None, sample=None, read_info=None,
             src_config_file=None, clean=False):
    mkdir(dest_dir, force=force)
    reads_file = get_reads_data(src_file, dest_dir, link=link)
    try:
        config = generate_config(reads_file, src_config_file)
        if sample is not None:
            reads_file, config = sample_reads(reads_file, config, sample_info=sample)
        if 'r' not in config or 'reads_size' not in config:
            config = calculate_reads_stats(reads_file, config, read_info)
        _, config = generate_histogram(reads_file, dest_dir, config, clean=clean)
        create_run_script(run_script_filename, dest_dir, link=link)
    finally:
        write_config(config, dest_dir)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Prepare experiment')
    parser.add_argument('source', type=Path, help='reads file')
    parser.add_argument('name', type=Path, help='directory')
    parser.add_argument('-s', '--sample', nargs='+', type=float,
                        help='Sample info: either target_coverage genome_size, or factor')
    parser.add_argument('-r', '--run-script', type=Path, default=Path(__file__).parent / 'run_covest.py',
                        help='Experiment runner: script for running the experiment')
    parser.add_argument('-i', '--info', nargs=2, help='Read info: avg_read_length, total_reads_size')
    parser.add_argument('-c', '--config-file', type=Path, nargs=1, help='config file')
    parser.add_argument('-f', '--force', action='store_true',
                        help='force use existing path')
    parser.add_argument('--copy', action='store_true', help='copy files instead of linking')
    parser.add_argument('--clean', action='store_true', help='clean jellyfish files')

    args = parser.parse_args()
    pipeline(
        args.source, args.name, link=not args.copy, force=args.force, sample=args.sample,
        run_script_filename=args.run_script, read_info=args.info, src_config_file=args.config_file,
        clean=args.clean,
    )
