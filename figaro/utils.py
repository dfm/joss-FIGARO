import numpy as np
import warnings

from pathlib import Path
from tqdm import tqdm
from collections import Counter
from scipy.stats import multivariate_normal as mn

from figaro.transform import transform_to_probit
from figaro.mixture import mixture

#-–––––––––-#
# Utilities #
#-----------#

def recursive_grid(bounds, n_pts):
    """
    Recursively generates the n-dimensional grid points (extremes are excluded).
    
    Arguments:
        :list-of-lists bounds: extremes for each dimension (excluded)
        :int n_pts:            number of points for each dimension
        
    Returns:
        :np.ndarray: grid
    """
    bounds = np.atleast_2d(bounds)
    n_pts  = np.atleast_1d(n_pts)
    if len(bounds) == 1:
        d  = np.linspace(bounds[0,0], bounds[0,1], n_pts[0]+2)[1:-1]
        dD = d[1]-d[0]
        return np.atleast_2d(d).T, [dD]
    else:
        grid_nm1, diff = recursive_grid(np.array(bounds)[1:], n_pts[1:])
        
        d = np.linspace(bounds[0,0], bounds[0,1], n_pts[0]+2)[1:-1]
        diff.append(d[1]-d[0])
        grid     = []
        for di in d:
            for gi in grid_nm1:
                grid.append([di,*gi])
        return np.array(grid), diff

def rejection_sampler(n_draws, f, bounds, selfunc = None):
    """
    1D rejection sampler, allows for a selection function
    
    Arguments:
        :int n_draws:      number of draws
        :callable f:       probability density to sample from
        :iterable bounds:  upper and lower bound
        :callable selfunc: selection function, must support numpy arrays
    
    Returns:
        :np.ndarray: samples
    """
    n_draws = int(n_draws)
    if selfunc is None:
        selfunc = lambda x: 1
    x   = np.linspace(bounds[0], bounds[1], 1000)
    top = np.max(f(x)*selfunc(x))
    samples = []
    while len(samples) < n_draws:
        pts   = np.random.uniform(bounds[0], bounds[1], size = n_draws)
        probs = f(pts)*selfunc(pts)
        h     = np.random.uniform(0, top, size = n_draws)
        samples.extend(pts[np.where(h < probs)])
    return np.array(samples).flatten()[:n_draws]

def get_priors(bounds, samples = None, mean = None, std = None, cov = None, df = None, k = None):
    """
    This method takes the prior parameters for the Normal-Inverse-Wishart distribution in the natural space and returns them as parameters in the probit space, ordered as required by FIGARO. In the following, D will denote the dimensionality of the inferred distribution.

    Four parameters are returned:
    * df, is the number of degrees of freedom for the Inverse Wishart distribution,. It must be greater than D+1. If this parameter is None or does not satisfy the condition df > D+1, the default value D+2 is used;
    * k is the scale parameter for the multivariate Normal distribution. Suggested values are  k <~ 1e-1. If None, the default value 1e-2 is used.
    * mu is the mean of the multivariate Normal distribution. It can be either estimated from the available samples or passed directly as a 1D array with length D (the keyword argument mean overrides the samples). If None, the default value 0 (corresponding to the parameter space center) is used.
    * L is the expected value for the Inverse Wishart distribution. This parameter can be either (in descending priority order):
        * passed as 2D array with shape (D,D), the covariance matrix - keyword cov;
        * passed as 1D array with shape (D,) or double: vector of standard deviations (if double, it assumes that the same std has to be used for all dimensions) - keyword std;
        * estimated from samples - keyword samples.
       
    The order in which they are returned is (k,L,df,mu).
    
    Arguments:
        :np.ndarray bounds:         boundaries for probit transformation
        :np.ndarray samples:        2D array with samples
        :double or np.ndarray mean: mean
        :double or np.ndarray std:  expected standard deviation (if double, the same expected std is used for all dimensions, if np.ndarray must match the number of dimensions)
        :np.ndarray cov:            covariance matrix
        :int df:                    degrees of freedom for Inverse Wishart distribution
        :double k:                  scale parameter for Normal distribution
        
    Returns:
        :tuple: prior parameters ordered as in H/DPGMM
    """
    bounds = np.atleast_2d(bounds)
    dim = len(bounds)
    if samples is not None:
        if len(np.shape(samples)) < 2:
            samples = np.atleast_2d(samples).T
        probit_samples = transform_to_probit(samples, bounds)
    # DF
    if df is not None and df > dim+2:
        df_out = df
    else:
        df_out = dim+2
        
    draw_flag = False
    # Mu
    if mean is not None:
        mean = np.atleast_1d(mean)
        if not np.prod(bounds[:,0] < mean) & np.prod(mean < bounds[:,1]):
            raise ValueError("Mean is outside of the given bounds")
        mu_out = transform_to_probit(mean, bounds)
    elif samples is not None:
        mu_out = np.atleast_1d(np.mean(probit_samples, axis = 0))
    else:
        mu_out = np.zeros(dim)
    # L
    if cov is not None:
        L_out = cov
        draw_flag = True
    elif std is not None:
        L_out = np.identity(dim)*std**2
        draw_flag = True
    elif samples is not None:
        # 1/3 (arbitrary) std of samples
        L_out = np.atleast_2d(np.cov(probit_samples.T))/9
        diag  = np.sqrt(np.diag(L_out))
        stds  = np.minimum(diag, 0.2)
        L_out = L_out*np.outer(stds, stds)/np.outer(diag, diag)
    else:
        L_out = np.identity(dim)*0.2**2
    # k
    if k is not None:
        k_out = k
    else:
        k_out = 1e-2
    
    if draw_flag:
        ss = mn(np.mean(bounds, axis = -1), L_out).rvs(10000)
        if dim == 1:
            ss = np.atleast_2d(ss).T
        # Keeping only samples within bounds
        ss = ss[np.where((np.prod(bounds[:,0] < ss, axis = 1) & np.prod(ss < bounds[:,1], axis = 1)))]
        probit_samples = transform_to_probit(ss, bounds)
        L_out = np.atleast_2d(np.cov(probit_samples.T))
        
    return (k_out, L_out, df_out, mu_out)

def rvs_median(draws, n_draws):
    idx = np.random.choice(np.arange(len(draws)), size = n_draws)
    ctr = Counter(idx)
    samples = np.empty(shape = (1, draws[0].dim))
    for i, n in zip(ctr.keys(), ctr.values()):
        samples = np.concatenate((samples, draws[i].rvs(n)))
    return samples[1:]
    
def make_single_gaussian_mixture(mu, cov, bounds, out_folder = '.', save = False, n_samps = 10000):
    """
    Builds mixtures composed of a single Gaussian distribution.
    WARNING: due to the probit coordinate change, a Gaussian distribution in the natural space does not correspond to a Gaussian distribution in the probit space.
    The resulting distributions, therefore, are just an approximation. This approximation holds for distributions which are far from boundaries.
    In general, a more robust (but slower) approach would be to draw samples from each original Gaussian distribution and to use them to make a hierarchical inference.
    
    Arguments:
        :np.ndarray mu: mean for each Gaussian distribution
        :np.ndarray cov: covariance matrix for each Gaussian distribution
        :np.ndarray bounds: boundaries for probit transformation
        :str out_folder: output folder
        :bool save: whether to save the draws or not
        :int n_samps: number of samples to estimate mean and covariance in probit space
    
    Returns:
        :np.ndarray: mixtures
    """
    bounds = np.atleast_2d(bounds)
    if len(cov.shape) == 1:
        cov     = np.atleast_2d(cov).T
    
    mixtures = []
    for m, c, in mu, cov:
        ss = mn(m, c).rvs(n_samps)
        # Keeping only samples within bounds
        ss = ss[np.where((np.prod(bounds[:,0] < ss, axis = 1) & np.prod(ss < bounds[:,1], axis = 1)))]
        # 1D issue
        if c.shape == (1,1):
            ss = ss.T
        # Probit samples
        p_ss = transform_to_probit(ss, bounds)
        mm = np.mean(p_ss)
        cc = np.atleast_2d(np.cov(probit_samples.T))
        
        mix = mixture(np.array([mm]), np.array([cc]), np.ones(1), bounds, len(bounds), 1, None)
        mixtures.append([mix])
    
    mixtures = np.array(mixtures)
    
    if save:
        with open(Path(out_folder, 'posteriors_single_event.pkl'), 'rb') as f:
            dill.dump(mixtures, f)
    
    return mixtures

#-------------#
#   Options   #
#-------------#

def save_options(options, out_folder):
    """
    Saves options for the run (reproducibility)
    
    Arguments:
        :dict options: options
    """
    logfile = open(Path(out_folder, 'options_log.txt'), 'w')
    for key, val in zip(vars(options).keys(), vars(options).values()):
        logfile.write('{0}: {1}\n'.format(key,val))
    logfile.close()
