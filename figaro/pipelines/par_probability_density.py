import numpy as np

import optparse as op
import dill
import importlib

from pathlib import Path
from tqdm import tqdm

from figaro.mixture import DPGMM
from figaro.utils import save_options, get_priors
from figaro.plot import plot_median_cr, plot_multidim
from figaro.load import load_single_event, save_density, load_density, supported_extensions

import ray
from ray.util import ActorPool

@ray.remote
class worker:
    def __init__(self, bounds,
                       sigma = None,
                       samples = None,
                       probit = True,
                       ):
        self.dim     = bounds.shape[-1]
        self.mixture = DPGMM(bounds, prior_pars = get_priors(bounds, samples = samples, std = sigma, probit = probit), probit = probit)
        self.samples = np.copy(samples)
        self.samples.setflags(write = True)

    def draw_sample(self):
        return self.mixture.density_from_samples(self.samples)

def main():

    parser = op.OptionParser()
    # Input/output
    parser.add_option("-i", "--input", type = "string", dest = "samples_path", help = "File with samples")
    parser.add_option("-b", "--bounds", type = "string", dest = "bounds", help = "Density bounds. Must be a string formatted as '[[xmin, xmax], [ymin, ymax],...]'. For 1D distributions use '[xmin, xmax]'. Quotation marks are required and scientific notation is accepted", default = None)
    parser.add_option("-o", "--output", type = "string", dest = "output", help = "Output folder. Default: same directory as samples", default = None)
    parser.add_option("-j", dest = "json", action = 'store_true', help = "Save mixtures in json file", default = False)
    parser.add_option("--inj_density", type = "string", dest = "inj_density_file", help = "Python module with injected density - please name the method 'density'", default = None)
    parser.add_option("--parameter", type = "string", dest = "par", help = "GW parameter(s) to be read from file", default = None)
    parser.add_option("--waveform", type = "string", dest = "wf", help = "Waveform to load from samples file. To be used in combination with --parameter. Accepted values: 'combined', 'imr', 'seob'", default = 'combined')
    # Plot
    parser.add_option("-p", "--postprocess", dest = "postprocess", action = 'store_true', help = "Postprocessing", default = False)
    parser.add_option("--symbol", type = "string", dest = "symbol", help = "LaTeX-style quantity symbol, for plotting purposes", default = None)
    parser.add_option("--unit", type = "string", dest = "unit", help = "LaTeX-style quantity unit, for plotting purposes", default = None)
    # Settings
    parser.add_option("--draws", type = "int", dest = "n_draws", help = "Number of draws", default = 100)
    parser.add_option("--n_samples_dsp", type = "int", dest = "n_samples_dsp", help = "Number of samples to analyse (downsampling). Default: all", default = -1)
    parser.add_option("--exclude_points", dest = "exclude_points", action = 'store_true', help = "Exclude points outside bounds from analysis", default = False)
    parser.add_option("--cosmology", type = "string", dest = "cosmology", help = "Cosmological parameters (h, om, ol). Default values from Planck (2021)", default = '0.674,0.315,0.685')
    parser.add_option("--sigma_prior", dest = "sigma_prior", type = "string", help = "Expected standard deviation (prior) - single value or n-dim values. If None, it is estimated from samples", default = None)
    parser.add_option("--n_parallel", dest = "n_parallel", type = "int", help = "Number of parallel threads", default = 4)
    parser.add_option("--snr_threshold", dest = "snr_threshold", type = "float", help = "SNR threshold for simulated GW datasets", default = None)
    parser.add_option("--far_threshold", dest = "far_threshold", type = "float", help = "FAR threshold for simulated GW datasets", default = None)
    parser.add_option("--no_probit", dest = "probit", action = 'store_false', help = "Disable probit transformation", default = True)
    
    (options, args) = parser.parse_args()

    # Paths
    options.samples_path = Path(options.samples_path).resolve()
    if options.output is not None:
        options.output = Path(options.output).resolve()
        if not options.output.exists():
            options.output.mkdir(parents=True)
    else:
        options.output = options.samples_path.parent
    # Read bounds
    if options.bounds is not None:
        options.bounds = np.array(np.atleast_2d(eval(options.bounds)), dtype = np.float64)
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
    # File extension
    if options.json:
        options.ext = 'json'
    else:
        options.ext = 'pkl'
    
    save_options(options, options.output)

    if options.sigma_prior is not None:
        options.sigma_prior = np.array([float(s) for s in options.sigma_prior.split(',')])
    if options.samples_path.is_file():
        files = [options.samples_path]
        output_draws = options.output
        subfolder = False
    else:
        files = sum([list(options.samples_path.glob('*.'+ext)) for ext in supported_extensions], [])
        output_draws = Path(options.output, 'draws')
        if not output_draws.exists():
            output_draws.mkdir()
        subfolder = True
    
    if not options.postprocess:
        ray.init(num_cpus = options.n_parallel)
    
    for i, file in enumerate(files):
        # Load samples
        samples, name = load_single_event(file, par = options.par, n_samples = options.n_samples_dsp, h = options.h, om = options.om, ol = options.ol, waveform = options.wf, snr_threshold = options.snr_threshold, far_threshold = options.far_threshold)
        try:
            dim = np.shape(samples)[-1]
        except IndexError:
            dim = 1
        if options.exclude_points:
            print("Ignoring points outside bounds.")
            samples = samples[np.where((np.prod(options.bounds[:,0] < samples, axis = 1) & np.prod(samples < options.bounds[:,1], axis = 1)))]
        else:
            # Check if all samples are within bounds
            if options.probit:
                if not np.alltrue([(samples[:,i] > options.bounds[i,0]).all() and (samples[:,i] < options.bounds[i,1]).all() for i in range(dim)]):
                    raise ValueError("One or more samples are outside the given bounds.")

        # Reconstruction
        if not options.postprocess:
            # Actual analysis
            desc = name + ' ({0}/{1})'.format(i+1, len(files))
            pool = ActorPool([worker.remote(bounds  = options.bounds,
                                            sigma   = options.sigma_prior,
                                            samples = samples,
                                            probit  = options.probit,
                                            )
                              for _ in range(options.n_parallel)])
            draws = []
            for s in tqdm(pool.map_unordered(lambda a, v: a.draw_sample.remote(), [_ for _ in range(options.n_draws)]), total = options.n_draws, desc = desc):
                draws.append(s)
            draws = np.array(draws)
            # Save reconstruction
            save_density(draws, folder = output_draws, name = 'draws_'+name, ext = options.ext)
        else:
            draws = load_density(Path(output_draws, 'draws_'+name+'.'+options.ext))

        # Plot
        if dim == 1:
            plot_median_cr(draws, injected = inj_density, samples = samples, out_folder = options.output, name = name, label = options.symbol, unit = options.unit, subfolder = subfolder)
        else:
            if options.symbol is not None:
                symbols = options.symbol.split(',')
            else:
                symbols = options.symbol
            if options.unit is not None:
                units = options.unit.split(',')
            else:
                units = options.unit
            plot_multidim(draws, samples = samples, out_folder = options.output, name = name, labels = symbols, units = units, subfolder = subfolder)

if __name__ == '__main__':
    main()
