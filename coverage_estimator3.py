#! /usr/bin/env python3
import sys
import itertools
from collections import defaultdict
from scipy.misc import comb, factorial
from scipy.optimize import minimize
import argparse
from inverse import inverse
import matplotlib.pyplot as plt
import numpy
from perf import running_time_decorator
from functools import lru_cache
import json
import random
# from utils import print_wrap as pw

# defaults
DEFAULT_K = 20
DEFAULT_READ_LENGTH = 100
ERROR_RATE = 0.03

# config
VERBOSE = True
# INF = 1e100
INF = float('inf')


def verbose_print(message):
    if not VERBOSE:
        return
    sys.stderr.write(message + "\n")


try:
    from bigfloat import BigFloat, exp, log, pow
except ImportError:
    from math import exp, log, pow
    verbose_print('BigFloats are not used!\nPrecision issues may occur.')
    BigFloat = lambda x: float(x)


def estimate_p(cc, alpha):
    return (cc * (alpha - 1)) / (alpha * cc - alpha - cc)


def get_trim(hist, precision=None):
    ss = float(sum(hist))
    s = 0.0
    trim = None
    for i, h in enumerate(hist):
        s += h
        r = s / ss
        if precision:
            r = round(r, precision)
        if r == 1 and trim is None:
            trim = i
    return trim


def load_dist(fname, autotrim=False, trim=None):
    unique_kmers = 0
    all_kmers = 0.0
    observed_ones = 0
    hist = defaultdict(int)
    max_hist = 0

    with open(fname, 'r') as f:
        for line in f:
            l = line.split()
            i = int(l[0])
            cnt = int(l[1])
            hist[i] = cnt
            max_hist = max(max_hist, i)
            if i == 1:
                observed_ones = cnt
            if i >= 2:
                unique_kmers += cnt
                all_kmers += i * cnt

    hist_l = [hist[b] for b in range(max_hist)]
    if autotrim:
        trim = get_trim(hist_l, autotrim)
        verbose_print('Trimming at: {}'.format(trim))
        hist_l = hist_l[:trim]
    elif trim is not None:
        hist_l = hist_l[:trim]
    return all_kmers, unique_kmers, observed_ones, hist_l


def compute_coverage_apx(all_kmers, unique_kmers, observed_ones, k, r):
    if unique_kmers == 0:
        return 0.0, 1.0
    total_unique_kmers = unique_kmers + observed_ones
    # compute coverage from hist >=2
    cov = all_kmers / unique_kmers
    # fix coverage
    fn = lambda cov: (cov - cov * exp(-cov)) / (1 - exp(-cov) - cov * exp(-cov))
    fix_coverage = inverse(fn)
    cov = fix_coverage(cov)
    # fix unique kmers
    unique_kmers /= (1.0 - exp(-cov) - cov * exp(-cov))
    # compute alpha (error read ratio)
    estimated_ones = unique_kmers * cov * exp(-cov)
    estimated_zeros = unique_kmers * exp(-cov)
    error_ones = max(0.0, observed_ones - estimated_ones)
    alpha = error_ones / (total_unique_kmers + estimated_zeros)
    # estimate probability of correct kmer and error rate
    estimated_p = max(0.0, estimate_p(cov, alpha))
    e = 1 - estimated_p ** (1.0 / k)
    # return corrected coverage and error estimate
    if estimated_p > 0:
        # function for conversion between kmer and base coverage
        kmer_to_read_coverage = lambda c: c * r / (r - k + 1)
        return float(kmer_to_read_coverage(cov / estimated_p)), float(e)
    else:
        return 0.0, float(e)


def tr_poisson(l, j):
    with numpy.errstate(over='raise'):
        try:
            if exp(l) == 1.0:  # precision fix
                return 0.0
            p1 = BigFloat(pow(l, j))
            p2 = BigFloat(factorial(j, exact=True))
            if l > 1e-8:
                p3 = BigFloat(exp(l) - 1.0)
            else:
                p3 = BigFloat(l)
            res = p1 / (p2 * p3)
            return res
        except (OverflowError, FloatingPointError):
            return 0.0


def safe_log(x):
    if x <= 0:
        return -INF
    return log(x)


@lru_cache(maxsize=100)
def compute_probabilities(r, k, c, err, max_hist):
    # read to kmer coverage
    c = c * (r - k + 1) / r
    # lambda for kmers with s errors
    l_s = [c * (3 ** -s) * (1.0 - err) ** (k - s) * err ** s for s in range(k + 1)]
    # expected probability of kmers with s errors and coverage >= 1
    n_s = [comb(k, s) * (3 ** s) * (1.0 - exp(-l_s[s])) for s in range(k + 1)]
    sum_n_s = sum(n_s[t] for t in range(k + 1))

    if sum_n_s == 0:  # division by zero fix
        sum_n_s = 1
    # portion of kmers with s errors
    a_s = [n_s[s] / sum_n_s for s in range(k + 1)]
    # probability that unique kmer has coverage j (j > 0)

    p_j = [None] + [
        sum(a_s[s] * tr_poisson(l_s[s], j) for s in range(k + 1)) for j in range(1, max_hist)
    ]
    return p_j


def compute_loglikelihood(hist, r, k, c, err):
    if err < 0 or err >= 1 or c <= 0:
        return -INF
    p_j = compute_probabilities(r, k, c, err, len(hist))
    return float(sum(
        hist[j] * safe_log(p_j[j])
        for j in range(1, len(hist))
        if hist[j]
    ))


@lru_cache(maxsize=100)
def compute_repeat_table(r, k, c, err, hist_size, treshold_o=None):
    p_j = compute_probabilities(r, k, c, err, hist_size)
    p_oj = [
        [None], [None] + [p_j[j] for j in range(1, hist_size)]
    ]

    if treshold_o is None:
        treshold_o = hist_size

    for o in range(2, treshold_o):
        p = [[None]]
        for j in range(1, hist_size):
            res = 0.0
            for i in range(1, j):
                t = p_oj[1][i] * p_oj[o - 1][j - i]
                res += t
            p.append(res)
        p_oj.append(p)

    return p_oj


def compute_probabilities_with_repeats(r, k, c, err, q1, q2, q, hist_size, treshold=1e-8):
    def b_o(o):
        if o == 1:
            return q1
        elif o == 2:
            return (1 - q1) * q2
        else:
            return (1 - q1) * (1 - q2) * q * (1 - q) ** (o - 3)

    treshold_o = None
    if treshold is not None:
        for o in range(1, hist_size):
            if b_o(o) < treshold:
                treshold_o = o
                break

    p_oj = compute_repeat_table(r, k, c, err, hist_size, treshold_o)

    p_j = lambda j: sum(
        b_o(o) * p_oj[o][j]
        for o in range(
            1,
            min(j + 1, treshold_o) if treshold_o is not None else j + 1
        )
    )
    return p_j


def compute_loglikelihood_with_repeats(hist, r, k, c, err, q1, q2, q):
    if err < 0 or err >= 1 or c <= 0:
        return -INF
    p_j = compute_probabilities_with_repeats(r, k, c, err, q1, q2, q, len(hist))
    return float(sum(hist[j] * safe_log(p_j(j)) for j in range(1, len(hist)) if hist[j]))


@running_time_decorator
def compute_coverage(hist, r, k, guessed_c=10, guessed_e=0.05,
                     error_rate=None, orig_coverage=None,
                     use_grid=False, use_hillclimb=False):
    likelihood_f = lambda x: -compute_loglikelihood(
        hist, args.read_length, args.kmer_size, x[0], x[1]
    )
    x0 = [guessed_c, guessed_e]
    res = minimize(
        likelihood_f, x0,
        bounds=((0.0, None), (0.0, 1.0)),
        options={'disp': True}
    )
    cov, e = res.x

    if use_hillclimb:
        verbose_print('Starting hillclimbing search with guess: {}'.format(res.x))
        cov, e = minimize_hillclimbing(
            likelihood_f, [cov, e],
            bounds=((0.0, None), (0.0, 1.0)),
        )

    if use_grid:
        verbose_print('Starting grid search with guess: {}'.format(res.x))
        cov, e = minimize_grid(
            likelihood_f, [cov, e], bounds=((0.0, None), (0.0, 1.0))
        )

    output_data = {
        'guessed_coverage': guessed_c,
        'guessed_error_rate': guessed_e,
        'guessed_loglikelihood': -likelihood_f(x0),
        'estimated_error_rate': e,
        'estimated_coverage': cov,
        'estimated_loglikelihood': -likelihood_f([cov, e]),
    }

    if error_rate is not None:
        output_data['original_error_rate'] = error_rate

    if error_rate is not None and orig_coverage is not None:
        output_data['original_loglikelihood'] = -likelihood_f([orig_coverage, error_rate])

    print(json.dumps(
        output_data, sort_keys=True, indent=4, separators=(',', ': ')
    ))

    return cov, e


@running_time_decorator
def compute_coverage_repeats(hist, r, k, guessed_c=10, guessed_e=0.05,
                             guessed_q1=0.5, guessed_q2=0.5, guessed_q=0.5,
                             error_rate=None, orig_coverage=None,
                             use_grid=False, use_hillclimb=True):

    likelihood_f = lambda x: -compute_loglikelihood_with_repeats(
        hist, args.read_length, args.kmer_size, x[0], x[1], x[2], x[3], x[4],
    )
    x0 = [guessed_c, guessed_e, guessed_q1, guessed_q2, guessed_q]

    res = minimize(
        likelihood_f, x0,
        bounds=((0.0, None), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)),
        options={'disp': True}
    )
    cov, e, q1, q2, q = res.x

    if use_hillclimb:
        verbose_print('Starting hillclimbing search with guess: {}'.format(res.x))
        cov, e, q1, q2, q = minimize_hillclimbing(
            likelihood_f, [cov, e, q1, q2, q],
            bounds=((0.0, None), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)),
        )

    if use_grid:
        verbose_print('Starting grid search with guess: {}'.format(res.x))
        cov, e, q1, q2, q = minimize_grid(
            likelihood_f, [cov, e, q1, q2, q],
            bounds=((0.0, None), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)),
        )

    output_data = {
        'guessed_coverage': guessed_c,
        'guessed_error_rate': guessed_e,
        'guessed_loglikelihood': -likelihood_f(x0),
        'estimated_error_rate': e,
        'estimated_coverage': cov,
        'estimated_loglikelihood': -likelihood_f([cov, e, q1, q2, q]),
        'estimated_q1': q1,
        'estimated_q2': q2,
        'estimated_q': q,
    }

    if error_rate is not None:
        output_data['original_error_rate'] = error_rate

    if error_rate is not None and orig_coverage is not None:
        output_data['original_loglikelihood'] = -likelihood_f(
            [orig_coverage, error_rate, q1, q2, q]
        )

    print(json.dumps(
        output_data, sort_keys=True, indent=4, separators=(',', ': ')
    ))

    return cov, e


def plot_probs(r, k, hist, est_c, est_e, guess_c, guess_e, orig_c=None, orig_e=None):
    hs = float(sum(hist))
    hp = [f / hs for f in hist]
    ep_f = compute_probabilities(r, k, est_c, est_e)
    gp_f = compute_probabilities(r, k, guess_c, guess_e)
    if orig_c is not None and orig_e is not None:
        op_f = compute_probabilities(r, k, orig_c, orig_e)
    else:
        op_f = lambda _: 0
    ep = [0] + [ep_f(j) for j in range(1, len(hist))]
    gp = [0] + [gp_f(j) for j in range(1, len(hist))]
    op = [0] + [op_f(j) for j in range(1, len(hist))]
    plt.plot(
        range(len(hp)), hp, 'ko',
        label='hist',
        ms=8,
    )
    plt.plot(
        range(len(ep)), ep, 'ro',
        label='est: C:{:.3f} E:{:.3f}'.format(est_c, est_e),
        ms=6,
    )
    plt.plot(
        range(len(gp)), gp, 'go',
        label='guess: C:{:.3f} E:{:.3f}'.format(guess_c, guess_e),
        ms=5,
    )
    plt.plot(
        range(len(op)), op, 'co',
        label='orig: C:{:.3f} E:{:.3f}'.format(orig_c, orig_e),
        ms=4,
    )
    plt.legend()
    plt.show()


@running_time_decorator
def minimize_grid(fn, initial_guess, bounds=None, oprions=None):
    def generate_grid(args, step, max_depth):
        def generate_grid_single(var):
            return (
                var * step ** d
                for d in range(-max_depth, max_depth + 1) if d != 0
            )

        def filter_bounds(var_grid, i):
            if bounds is None or len(bounds) <= i or len(bounds[i]) != 2:
                return var_grid
            low, high = bounds[i]
            return (
                var for var in var_grid
                if (low is None or var >= low) and (high is None or var <= high)
            )

        var_grids = [
            list(filter_bounds(generate_grid_single(var), i))
            for i, var in enumerate(args)
        ]
        return itertools.product(*var_grids)

    min_val = fn(initial_guess)
    min_args = initial_guess
    step = 1.1
    grid_depth = 3
    diff = 1
    n_iter = 0
    verbose_print('Grid size: {}'.format(sum(
        [1 for _ in generate_grid(min_args, step, grid_depth)]
    )))
    try:
        while (diff > 0.1 or step > 1.001):
            n_iter += 1
            diff = 0.0
            for args in generate_grid(min_args, step, grid_depth):
                val = fn(args)
                if val < min_val:
                    diff += min_val - val
                    min_val = val
                    min_args = args
            if diff < 1.0:
                step = 1 + (step - 1) * 0.75
            verbose_print('GS_{}: d:{} s:{}'.format(n_iter, diff, step))
    except KeyboardInterrupt:
        verbose_print('Grid search interrupted')

    verbose_print('Number of iterations in grid search:{}'.format(n_iter))
    return min_args


@running_time_decorator
def minimize_hillclimbing(fn, initial_guess, bounds=None, oprions=None, iterations=1000):
    def generate_grid(args, step, max_depth):
        def generate_grid_single(var):
            return (
                var * step ** d
                for d in range(-max_depth, max_depth + 1) if d != 0
            )

        def filter_bounds(var_grid, i):
            if bounds is None or len(bounds) <= i or len(bounds[i]) != 2:
                return var_grid
            low, high = bounds[i]
            return (
                var for var in var_grid
                if (low is None or var >= low) and (high is None or var <= high)
            )

        r = random.randrange(len(args))
        var_grids = [
            list(filter_bounds(generate_grid_single(var), i)) if i == r else [args[i]]
            for i, var in enumerate(args)
        ]
        return itertools.product(*var_grids)

    min_val = fn(initial_guess)
    min_args = initial_guess
    step = 1.1
    grid_depth = 3
    n_iter = 0
    verbose_print('Grid size: {}'.format(sum(
        [1 for _ in generate_grid(min_args, step, grid_depth)]
    )))
    try:
        while n_iter < iterations:
            modified = False
            n_iter += 1
            for args in generate_grid(min_args, step, grid_depth):
                val = fn(args)
                if val < min_val:
                    modified = True
                    min_val = val
                    min_args = args
            verbose_print('HC_{} m:{}'.format(n_iter, modified))
    except KeyboardInterrupt:
        verbose_print('Hill climb search interrupted')

    verbose_print('Number of iterations in grid search:{}'.format(n_iter))
    return min_args


@running_time_decorator
def main(args):
    orig_error_rate = args.error_rate if 'error_rate' in args else None
    orig_coverage = args.coverage if 'coverage' in args else None
    all_kmers, unique_kmers, observed_ones, hist = load_dist(
        args.input_histogram, autotrim=args.autotrim, trim=args.trim
    )
    if args.ll_only:
        ll = compute_loglikelihood(
            hist, args.read_length, args.kmer_size, orig_coverage, orig_error_rate,
        )
        print('Loglikelihood:', ll)
    else:
        verbose_print('Estimating coverage for {}'.format(args.input_histogram))
        cov, e = compute_coverage_apx(
            all_kmers, unique_kmers, observed_ones,
            args.kmer_size, args.read_length
        )
        verbose_print('Initial guess: c: {} e: {} ll:{}'.format(cov, e, compute_loglikelihood(
            hist, args.read_length, args.kmer_size, cov, e
        )))

        # We were unable to guess cov and e, try to estimate from some fixed valid data instead
        if cov == 0 and e == 1:
            cov = 1
            e = 0.5

        cov_est = compute_coverage_repeats if args.repeats else compute_coverage
        cov2, e2 = cov_est(
            hist, args.read_length, args.kmer_size, cov, e,
            error_rate=orig_error_rate, orig_coverage=orig_coverage,
            use_grid=args.grid, use_hillclimb=args.hillclimbing,
        )
        if args.plot:
            plot_probs(
                args.read_length, args.kmer_size, hist,
                cov2, e2, cov, e, orig_coverage, orig_error_rate,
            )

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Simulate reads form random genome with errors')
    parser.add_argument('input_histogram', help='Input histogram')
    parser.add_argument('-k', '--kmer-size', type=int,
                        default=DEFAULT_K, help='Kmer size')
    parser.add_argument('-r', '--read-length', type=int,
                        default=DEFAULT_READ_LENGTH, help='Read length')
    parser.add_argument('-e', '--error-rate', type=float, help='Error rate')
    parser.add_argument('-c', '--coverage', type=float, help='Coverage')
    parser.add_argument('--plot', action='store_true', help='Plot probabilities')
    parser.add_argument('-rp', '--repeats', action='store_true', help='Estimate vith repeats')
    parser.add_argument('-ll', '--ll-only', action='store_true',
                        help='Only compute log likelihood')
    parser.add_argument('-t', '--trim', type=int, help='Trim histogram at this value')
    parser.add_argument('-at', '--autotrim', type=int, nargs='?', const=False,
                        help='Trim histogram at this value')
    parser.add_argument('-g', '--grid', action='store_true', default=False,
                        help='Use grid search')
    parser.add_argument('-hc', '--hillclimbing', action='store_true', default=False,
                        help='Use hill climbing')

    args = parser.parse_args()
    main(args)
