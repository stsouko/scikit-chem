#! /usr/bin/env python
#
# Copyright (C) 2016 Rich Lewis <rl403@cam.ac.uk>
# License: 3-clause BSD

"""
## skchem.cross_validation.similarity_threshold

Similarity threshold dataset partitioning functionality.
"""

import logging

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from scipy.spatial.distance import pdist, cdist, squareform
from scipy.sparse import triu, dok_matrix
from scipy.optimize import minimize_scalar
from sklearn.manifold import TSNE, MDS

import multiprocessing
from functools import partial, wraps

from .. import features

LOGGER = logging.getLogger(__name__)


def returns_pairs(func):
    """ Wraps a function that returns a ((i, j), sim) list to return a
    dataframe. """

    @wraps(func)
    def inner(*args, **kwargs):
        pairs = func(*args, **kwargs)
        return pd.DataFrame([(p[0][0], p[0][1], p[1]) for p in pairs],
                            columns=['i', 'j', 'sim']).sort_values('sim')
    return inner


def _above_minimum(args, inp, metric, threshold, size):
    """ finds pairs above a minimum similarity in chunks """
    i, j = slice(*args[0]), slice(*args[1])
    x_i, x_j = inp[i], inp[j]
    sim_mat = 1 - cdist(x_i, x_j, metric=metric)
    if i == j:
        sim_mat = np.triu(sim_mat, k=1)
    sim_mat[sim_mat <= threshold] = 0
    sparse_mat = dok_matrix((size, size), dtype=float)
    sparse_mat[i, j] = sim_mat
    return list(sparse_mat.items())


class SimThresholdSplit(object):

    def __init__(self,  min_threshold=0.45, largest_cluster_fraction=0.1,
                 fper='morgan', similarity_metric='jaccard',
                 memory_optimized=True, n_jobs=1, block_width=1000,
                 verbose=False):

        """ Threshold similarity split for chemical datasets.

        This class implements a splitting technique that will pool compounds
        with similarity above a theshold into the same splits.  The threshold
        value is decided by specifying the maximum number of compounds to pool
        into a cluster, as the density of compounds varies with dataset.

        Machine learning techniques should be able to extrapolate outside of a
        molecular series, or scaffold, however random splits will result in
        some 'easy' test sets that are either *identical* or in the same
        molecular series or share a significant scaffold with training set
        compounds.

        This splitting technique reduces or eliminates (depending on the
        threshold set) this effect, making the problem harder.

        Args:

            min_threshold (float):
                The minimum similarity threshold.  Lower will be slower.

            largest_cluster_fraction (float):
                The fraction of the total dataset the largest cluster can be.
                This decided the final similarity threshold.

            fper (str or skchem.Fingerprinter):
                The fingerprinting technique to use to generate the similarity
                matrix.

            similarity_metric (str or callable):
                The similarity metric to use.

            memory_optimized (bool):
                Whether to use the memory optimized implementation.

            n_jobs (int):
                If memory_optimized is True, how many processes to run it over.

            block_width (int):
                If memory_optimized, what block length to use.  This is the
                width of the submatrices that are calculated at a time.

        Notes:
            The splits will not always be exactly the size requested, due to
            the constraint and requirement to maintain random shuffling.
        """

        self.index = self.fps = self._n_jobs = self._n_instances_ = None
        self.pairs_ = self.threshold_ = None

        if isinstance(fper, str):
            fper = features.get(fper)

        self.fper = fper
        self.similarity_metric = similarity_metric
        self.memory_optimized = memory_optimized
        self.n_jobs = n_jobs
        self._block_width = block_width
        self.min_threshold = min_threshold
        self.largest_cluster = largest_cluster_fraction

        if self.fper:
            self.fper.verbose = verbose

    def fit(self, inp, pairs=None):

        """ Fit the cross validator to the data.
        Args:
             inp (pd.Series or pd.DataFrame or np.array):
                - `pd.Series` of `Mol` instances
                - `pd.DataFrame` with `Mol` instances as a `structure` row.
                - `pd.DataFrame` of fingerprints if `fper` is `None`
                - `pd.DataFrame` of sim matrix if `similarity_metric` is `None`
                - `np.array` of sim matrix if `similarity_metric` is `None`

            pairs (list<tuple<tuple(i, j), k>>):
                An optional precalculated list of pairwise distances.
        """

        self.n_instances_ = len(inp)
        self.pairs_ = pairs

        if isinstance(inp, (pd.Series, pd.DataFrame)):
            self.index = inp.index
        else:
            self.index = pd.RangeIndex(len(inp), name='batch')

        if self.similarity_metric is None:
            # we were passed a similarity matrix directly
            self.pairs_ = self._pairs_from_sim_mat(inp)

        elif self.fper is None:
            # we were passed fingerprints directly
            self.fps = inp
            if self.pairs_ is None:
                self.pairs_ = self._pairs_from_fps(inp)

        else:
            # we were passed Mol
            if self.pairs_ is None:
                self.fps = self.fper.transform(inp)
                self.pairs_ = self._pairs_from_fps(self.fps)

        self.threshold_ = self._optimal_thresh()

        return self

    @property
    def n_jobs(self):
        """ The number of processes to use to calculate the distance matrix.

         -1 for all available. """
        return self._n_jobs

    @n_jobs.setter
    def n_jobs(self, val):
        if val == -1:
            self._n_jobs = multiprocessing.cpu_count()
        else:
            self._n_jobs = val

    @property
    def block_width(self):
        """ The width of the subsets of features.

         Note:
             Only used in parallelized. """

        return self._block_width

    @block_width.setter
    def block_width(self, val):
        assert val <= self.n_instances_, 'The block width should be less than '
        'or equal to the number of instances'
        self._block_width = val

    @property
    def n_instances_(self):
        """ The number of instances that were used to fit the object. """
        return self._n_instances_

    @n_instances_.setter
    def n_instances_(self, val):
        assert val >= self._block_width, 'The block width should be less than '
        'or equal to the number of instances'
        self._n_instances_ = val

    def _cluster_cumsum(self, shuffled=True):

        nums = self.clusters.value_counts()
        if shuffled:
            nums = nums.ix[np.random.permutation(nums.index)].cumsum()
        return nums

    def split(self, ratio):

        """ Return splits of the data with thresholded similarity according to a
        specified ratio.

        Args:
            ratio (tuple[ints]):
                the ratio to use.
        Returns:
            generator[pd.Series]:
                Generator of boolean split masks for the reqested splits.

        Example:
            >>> ms = skchem.data.Diversity.read_frame('structure') # doctest: +SKIP
            >>> st = SimThresholdSplit(fper='morgan', # doctest: +SKIP
            ...                        similarity_metric='jaccard')
            >>> st.fit(ms) # doctest: +SKIP
            >>> train, valid, test = st.split(ratio=(70, 15, 15)) # doctest: +SKIP

        """

        ratio = self._split_sizes(ratio)
        nums = self._cluster_cumsum()
        res = pd.Series(np.nan, index=nums.index, name='split')

        for i in range(len(ratio)):
            lower = 0 if i == 0 else sum(ratio[:i])
            upper = ratio if i == len(ratio) else sum(ratio[:i + 1])
            res[nums[(nums > lower) & (nums <= upper)].index] = i

        res = res.sort_index()
        res = self.clusters.to_frame().join(res, on='clusters')['split']
        return (res == i for i in range(len(ratio)))

    def k_fold(self, n_folds):

        """ Returns k-fold cross-validated folds with thresholded similarity.

        Args:
            n_folds (int):
                The number of folds to provide.

        Returns:
            generator[(pd.Series, pd.Series)]:
                The splits in series.
        """

        folds = self.split((1,) * n_folds)
        return ((~fold, fold) for fold in folds)

    def _split_sizes(self, ratio):
        """ Calculate the sizes of the splits """

        tot = sum(ratio)
        return [self.n_instances_ * rat / tot for rat in ratio]

    @staticmethod
    @returns_pairs
    def _pairs_from_sim_mat(sim_mat):
        sim_mat = triu(sim_mat, k=1).todok()
        return list(sim_mat.items())

    @returns_pairs
    def _pairs_from_fps(self, fps):
        """ Pairs from fps. """
        if self.memory_optimized:
            pairs = self._pairs_from_fps_mem_opt(fps)
        else:
            pairs = self._pairs_from_fps_mem_intensive(fps)

        return pairs

    def _pairs_from_fps_mem_intensive(self, fps):
        """ Fast single process, memory intensive implementation of pairs. """
        LOGGER.debug('Generating pairs using memory intensive technique.')
        dist_mat = squareform(pdist(fps, self.similarity_metric))
        sim_mat = 1 - dist_mat # similarity is 1 - distance
        sim_mat[sim_mat <= self.min_threshold] = 0
        return self._pairs_from_sim_mat(sim_mat)

    def _pairs_from_fps_mem_opt(self, fps):

        """ Fast, multi-processed and memory efficient pairs."""

        def slice_generator(low, high, width, end=False):
            """ Checkerboard indices for the upper triangle of a matrix. """
            while low < high:
                res = (low, low + width if low + width < high else high)
                if end:
                    yield res
                else:
                    gen = slice_generator(low, high, width, end=True)
                    # py2 compat
                    # yield from ((res, j) for j in
                    # slice_generator(low, high, width, end=True))
                    for slice_ in ((res, j) for j in gen):
                        yield slice_
                low += width

        size = len(fps)

        fps = fps.values
        f = partial(_above_minimum, inp=fps, threshold=self.min_threshold,
                    metric=self.similarity_metric, size=size)
        slices = slice_generator(0, len(fps), self.block_width)

        if self.n_jobs == 1:
            # single processed
            LOGGER.debug('Generating pairs using memory optimized technique.')
            return sum((f(slice_) for slice_ in slices), [])
        else:
            # multiprocessed
            LOGGER.debug('Generating pairs using memory optimized technique '
                         'with %s processes', self.n_jobs)
            # py2 compat
            # with multiprocessing.Pool(self.n_jobs) as p:
            #    return sum(p.map(f, [(i, j) for i, j in slices]), [])
            p = multiprocessing.Pool(self.n_jobs)
            res = sum(p.map(f, [(i, j) for i, j in slices]), [])
            p.close()
            return res

    def _cluster(self, pairs):
        """ Assign instances to clusters. """

        LOGGER.debug('Generating clusters with %s close pairs', len(pairs))
        clustered = np.arange(self.n_instances_)

        for i, j in pairs.values.tolist(): # faster as list
            i_clust, j_clust = clustered[i], clustered[j]
            if i_clust < j_clust:
                clustered[clustered == j_clust] = i_clust
            else:
                clustered[clustered == i_clust] = j_clust
        return clustered

    def _optimal_thresh(self):
        """ Calculate the optimal threshold for the given max pair density. """
        def f(threshold):
            pairs = self.pairs_.loc[self.pairs_.sim > threshold, ('i', 'j')]
            res = pd.Series(self._cluster(pairs))
            return np.abs(res.value_counts().max() -
                          self.largest_cluster * self.n_instances_)

        self.threshold_ = minimize_scalar(f,
                                          bounds=(self.min_threshold, 1),
                                          method='bounded').x

        LOGGER.info('Optimal threshold: %s', self.threshold_)
        gd_prs = self.pairs_.loc[self.pairs_.sim > self.threshold_, ('i', 'j')]
        self.clusters = pd.Series(self._cluster(gd_prs),
                                  index=self.index,
                                  name='clusters')
        return self.threshold_

    def visualize_similarities(self, subsample=5000, ax=None):

        """ Plot a histogram of similarities, with the threshold plotted.

        Args:
            subsample (int):
                For a large dataset, subsample the number of compounds to
                consider.
            ax (matplotlib.axis):
                Axis to make the plot on.
        Returns:
            matplotlib.axes
        """

        if not ax:
            ax = plt.gca()

        if subsample and len(self.fps) > subsample:
            fps = self.fps.sample(subsample)
        else:
            fps = self.fps

        dists = 1 - squareform(pdist(fps, self.similarity_metric))
        dists = (dists - np.identity(dists.shape[0])).flatten()
        hist = ax.hist(dists, bins=50)
        ax.vlines(self.threshold_, 0, max(hist[0]))
        ax.set_xlabel('similarity')
        return ax

    def visualize_space(self, dim_reducer='tsne', dim_red_kw=None,
                        subsample=5000, ax=None, plt_kw=None):

        """ Plot chemical space using a transformer

        Args:
            dim_reducer (str or sklearn object):
                Technique to use to reduce fingerprint space.

            dim_red_kw (dict):
                Keyword args to pass to dim_reducer.

            subsample (int):
                for a large dataset, subsample the number of compounds to
                consider.

            ax (matplotlib.axis):
                Axis to make the plot on.

            plt_kw (dict):
                Keyword args to pass to the plot.

        Returns:
            matplotlib.axes
        """

        if dim_red_kw is None:
            dim_red_kw = {}

        if plt_kw is None:
            plt_kw = {}

        if isinstance(dim_reducer, str):
            if dim_reducer not in ('tsne', 'mds'):
                msg = 'Dimensionality reducer {} not available'.format(
                    dim_reducer)
                raise NotImplementedError(msg)
            reducers = {'tsne': TSNE, 'mds': MDS}
            dim_reducer = reducers[dim_reducer](**dim_red_kw)

        two_d = dim_reducer.fit_transform(self.fps)

        if not ax:
            ax = plt.gca()

        return ax.scatter(two_d[:, 0], two_d[:, 1], **plt_kw)
