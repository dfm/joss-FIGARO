import numpy as np
import warnings

import optparse as op
import dill

from pathlib import Path
from tqdm import tqdm

from figaro.mixture import DPGMM
from figaro.utils import save_options, get_priors
from figaro.plot import plot_median_cr, plot_multidim, plot_1d_dist
from figaro.load import load_single_event
from figaro.diagnostic import compute_entropy_single_draw, compute_angular_coefficients

def main():

    parser = op.OptionParser()
    # Input/output
    parser.add_option("-i", "--input", type = "string", dest = "samples_file", help = "File with samples")
    parser.add_option("-b", "--bounds", type = "string", dest = "bounds", help = "Density bounds. Must be a string formatted as '[[xmin, xmax], [ymin, ymax],...]'. For 1D distributions use '[xmin, xmax]'. Quotation marks are required and scientific notation is accepted", default = None)
    parser.add_option("-o", "--output", type = "string", dest = "output", help = "Output folder. Default: same directory as samples", default = None)
    parser.add_option("--inj_density", type = "string", dest = "inj_density_file", help = "Python module with injected density - please name the method 'density'", default = None)
    parser.add_option("--parameter", type = "string", dest = "par", help = "GW parameter(s) to be read from file", default = None)
    parser.add_option("--waveform", type = "string", dest = "wf", help = "Waveform to load from samples file. To be used in combination with --parameter. Accepted values: 'combined', 'imr', 'seob'", default = 'combined')
    # Plot
    parser.add_option("-p", "--postprocess", dest = "postprocess", action = 'store_true', help = "Postprocessing", default = False)
    parser.add_option("--symbol", type = "string", dest = "symbol", help = "LaTeX-style quantity symbol, for plotting purposes", default = None)
    parser.add_option("--unit", type = "string", dest = "unit", help = "LaTeX-style quantity unit, for plotting purposes", default = None)
    parser.add_option("-n", "--no_plot_dist", dest = "plot_dist", action = 'store_false', help = "Skip distribution plot", default = True)
    # Settings
    parser.add_option("--draws", type = "int", dest = "n_draws", help = "Number of draws", default = 100)
    parser.add_option("--n_samples_dsp", type = "int", dest = "n_samples_dsp", help = "Number of samples to analyse (downsampling). Default: all", default = -1)
    parser.add_option("--exclude_points", dest = "exclude_points", action = 'store_true', help = "Exclude points outside bounds from analysis", default = False)
    parser.add_option("--cosmology", type = "string", dest = "cosmology", help = "Cosmological parameters (h, om, ol). Default values from Planck (2021)", default = '0.674,0.315,0.685')
    parser.add_option("--sigma_prior", dest = "sigma_prior", type = "string", help = "Expected standard deviation (prior) - single value or n-dim values. If None, it is estimated from samples", default = None)
    parser.add_option("--snr_threshold", dest = "snr_threshold", type = "float", help = "SNR threshold for simulated GW datasets", default = None)
    parser.add_option("--zero_crossings", dest = "zero_crossings", type = "int", help = "Number of zero-crossings of the entropy derivative to call the number of samples sufficient. Default as in Appendix B of Rinaldi & Del Pozzo (2021)", default = 5)
    parser.add_option("--window", dest = "window", type = "int", help = "Number of points to use to approximate the entropy derivative", default = None)
    parser.add_option("--entropy_interval", dest = "entropy_interval", type = "int", help = "Number of samples between two entropy evaluations", default = 1)
    
    (options, args) = parser.parse_args()

    # Paths
    options.samples_file = Path(options.samples_file).resolve()
    if options.output is not None:
        options.output = Path(options.output).resolve()
        if not options.output.exists():
            options.output.mkdir(parents=True)
    else:
        options.output = options.samples_file.parent
    # Read bounds
    if options.bounds is not None:
        options.bounds = np.atleast_2d(eval(options.bounds))
    elif options.bounds is None and not options.postprocess:
        raise Exception("Please provide bounds for the inference (use -b '[[xmin,xmax],[ymin,ymax],...]')")
    # If provided, load injected density
    inj_density = None
    if options.inj_density_file is not None:
        inj_file_name = Path(options.inj_density_file).parts[-1].split('.')[0]
        spec = importlib.util.spec_from_file_location(inj_file_name, options.inj_density_file)
        inj_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inj_module)
        inj_density = inj_module.density
    # Read cosmology
    options.h, options.om, options.ol = (float(x) for x in options.cosmology.split(','))
    # Read parameter(s)
    if options.par is not None:
        options.par = options.par.split(',')


    save_options(options, options.output)
    
    # Load samples
    samples, name = load_single_event(options.samples_file, par = options.par, n_samples = options.n_samples_dsp, h = options.h, om = options.om, ol = options.ol, waveform = options.wf, snr_threshold = options.snr_threshold)
    try:
        dim = np.shape(samples)[-1]
    except IndexError:
        dim = 1
    if options.exclude_points:
        print("Ignoring points outside bounds.")
        samples = samples[np.where((np.prod(options.bounds[:,0] < samples, axis = 1) & np.prod(samples < options.bounds[:,1], axis = 1)))]
    else:
        # Check if all samples are within bounds
        if not np.alltrue([(samples[:,i] > options.bounds[i,0]).all() and (samples[:,i] < options.bounds[i,1]).all() for i in range(dim)]):
            raise ValueError("One or more samples are outside the given bounds.")
    if options.sigma_prior is not None:
        options.sigma_prior = np.array([float(s) for s in options.sigma_prior.split(',')])
    # Entropy derivative window
    min_window = 200 # Default (empiric) value
    if options.window is None:
        if len(samples) > min_window:
            options.window = np.max([len(samples)//5, min_window])
        else:
            options.window = len(samples)//5
    if options.window < min_window:
        warnings.warn("The window is smaller than the minimum recommended window for entropy derivative estimate. Results might be unreliable")
    options.window = options.window//options.entropy_interval
    
    # Reconstruction
    if not options.postprocess:
        # Actual analysis
        mix     = DPGMM(options.bounds, prior_pars = get_priors(options.bounds, samples = samples, std = options.sigma_prior))
        draws   = []
        entropy = []
        # This reproduces what it is done inside mix.density_from_samples while computing entropy for each new sample
        for j in tqdm(range(options.n_draws), desc = name, disable = (options.n_draws == 1)):
            S        = np.zeros(len(samples)//options.entropy_interval)
            n_eval_S = np.zeros(len(samples)//options.entropy_interval)
            mix.initialise()
            np.random.shuffle(samples)
            for i, s in tqdm(enumerate(samples), total = len(samples), disable = (j > 0)):
                mix.add_new_point(s)
                if i%options.entropy_interval == 0:
                    S[i//options.entropy_interval]        = compute_entropy_single_draw(mix)
                    n_eval_S[i//options.entropy_interval] = i
            draws.append(mix.build_mixture())
            entropy.append(S)
        draws     = np.array(draws)
        entropy   = np.concatenate(([n_eval_S], np.atleast_2d(entropy)))
        # Save reconstruction
        with open(Path(options.output, 'draws_'+name+'.pkl'), 'wb') as f:
            dill.dump(draws, f)
        np.savetxt(Path(options.output, 'entropy_'+name+'.txt'), entropy)

    else:
        try:
            with open(Path(options.output, 'draws_'+name+'.pkl'), 'rb') as f:
                draws = dill.load(f)
            entropy   = np.atleast_2d(np.loadtxt(Path(options.output, 'entropy_'+name+'.txt')))
        except FileNotFoundError:
            raise FileNotFoundError("No draws_{0}.pkl, entropy_{0}.txt or ang_coeff_{0}.txt file(s) found. Please provide them or re-run the inference".format(name))

    if options.plot_dist:
        # Plot distribution
        if dim == 1:
            plot_median_cr(draws, injected = inj_density, samples = samples, out_folder = options.output, name = name, label = options.symbol, unit = options.unit)
        else:
            if options.symbol is not None:
                symbols = options.symbol.split(',')
            else:
                symbols = options.symbol
            if options.unit is not None:
                units = options.unit.split(',')
            else:
                units = options.unit
            plot_multidim(draws, samples = samples, out_folder = options.output, name = name, labels = symbols, units = units)

    n_samps_S = entropy[0]
    entropy   = entropy[1:]
    entropy_interval = int(n_samps_S[1]-n_samps_S[0])

    # Angular coefficients
    ang_coeff = np.atleast_2d([compute_angular_coefficients(S, options.window) for S in entropy])
    # Zero-crossings
    zero_crossings = [(options.window + np.where(np.diff(np.sign(ac)))[0])*entropy_interval for ac in ang_coeff]
    endpoints = []
    conv_not_reached_flag = False
    for zc in zero_crossings:
        try:
            endpoints.append(zc[options.zero_crossings])
        except IndexError:
            conv_not_reached_flag = True
    if not len(endpoints) == 0:
        EP = int(np.mean(endpoints))
        EP_label = '{0}'.format(EP) + '\ \mathrm{samples}'
        np.savetxt(Path(options.output, 'endpoint_'+name+'.txt'), np.atleast_1d(int(EP)))
        if not conv_not_reached_flag:
            print('Average number of samples required for convergence: {0}'.format(EP))
        else:
            print('Average number of samples required for convergence: {0}\nWARNING: at least one draw did not converge.'.format(EP))
    else:
        EP = None
        EP_label = None
        print('Convergence not reached yet')
    
    # Entropy & entropy derivative plot
    plot_1d_dist(n_samps_S, entropy, out_folder = options.output, name = 'entropy_'+name, label = 'N_{s}', median_label = '\mathrm{Entropy}')
    plot_1d_dist(np.arange(options.window*entropy_interval, len(samples))[::entropy_interval], ang_coeff, out_folder = options.output, name = 'ang_coeff_'+name, label = 'N_{s}', injected = np.zeros((len(samples)-options.window*entropy_interval)//entropy_interval), true_value = EP, true_value_label = EP_label, median_label = '\mathrm{Entropy\ derivative}', injected_label = None)

if __name__ == '__main__':
    main()
