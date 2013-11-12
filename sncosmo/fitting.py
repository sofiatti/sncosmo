# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import division
from warnings import warn
import copy
import sys
from itertools import product

import numpy as np
from scipy.interpolate import InterpolatedUnivariateSpline as Spline1d

from .spectral import get_magsystem, get_bandpass
from .models import get_model
from .photometric_data import standardize_data, normalize_data
from . import nest
from .utils import Result, Interp1d, pdf_to_ppf

import sys
MAX_FLOAT = sys.float_info.max

__all__ = ['fit_lc', 'nest_lc', 'mcmc_lc']

def fit_lc(data, model, param_names, bounds=None, method='iminuit',
           guess_amplitude=True, refine_guess=False, disp=False, maxiter=10000):

    # Standardize and normalize data.
    data = standardize_data(data)
    data = normalize_data(data)

    # Make a copy of the model so we can modify it with impunity.
    model = copy.copy(model)

    # initialize bounds
    if bounds is None:
        bounds = {}

    # Check that 'z' is bounded (if it is going to be fit).
    if 'z' in param_names:
        if 'z' not in bounds:
            raise ValueError('z must be bounded if fit.')
        if model['z'] < bounds['z'][0] or model['z'] > bounds['z'][1]:
            model['z'] = sum(bounds['z']) / 2.

    # Cut bands that are not allowed by the wavelength range of the model
    if 'z' not in param_names:
        valid = model.bandoverlap(data['band'])
    else:
        valid = model.bandoverlap(data['band'], z=bounds['z'])
        valid = np.all(valid, axis=1)
    if not np.all(valid):
        if not np.any(valid):
            raise RuntimeError('No bands in data overlap model.')
        drop_bands = [repr(b) for b in set(data['band'][np.invert(valid)])]
        warn("Dropping following bands from data: " + ", ".join(drop_bands) +
             "(out of model wavelength range)", RuntimeWarning)
        data = data[valid]

    # Unique set of bands in data
    bands = set(data['band'].tolist())

    # Find t0 bounds to use, if not explicitly given
    if 't0' in param_names and 't0' not in bounds:
        bounds['t0'] = (model['t0'] + np.min(data['time']) - model.maxtime,
                        model['t0'] + np.max(data['time']) - model.mintime)
        model['t0'] = sum(bounds['t0']) / 2.

    # If we're fitting for 'amplitude', find its starting point.
    # (For now we assume it is the 3rd parameter of the model.)
    if model.param_names[2] in param_names and guess_amplitude:
        modelflux = {}
        dataflux = {}
        zp = data['zp'][0]
        zpsys = data['zpsys'][0]
        for band in bands:
            modelflux[band] = (model.bandflux(band, zp=zp, zpsys=zpsys) /
                               model.parameters[2])
            mask = data['band'] == band
            dataflux[band] = data['flux'][mask]

        # ratio of maximum data flux to maximum model flux in each band
        amplitude = max([abs(np.max(dataflux[band]) / np.max(modelflux[band]))
                         for band in bands])

        model.parameters[2] = amplitude

        # Get a refined guess by doing a small grid search
        #if refine_guess:
        #
        #    # Get data times, errors and turn model fluxes into splines
        #    modeltimes = model.times - model['t0']
        #    datatimes = {}
        #    datafluxerr = {}
        #    for band in bands:
        #        mask = data['band'] == band
        #        datatimes[band] = data['time'][mask]
        #        datafluxerr[band] = data['fluxerr'][mask]
        #        modelflux[band] = Spline1d(modeltimes, modelflux[band])
        #
        #    # set up a grid in (t0, amplitude) space
        #    t0_grid = np.linspace(bounds['t0'][0], bounds['t0'][1], 50)
        #    a_grid = np.logspace(np.log10(amplitude) - 2.,
        #                         np.log10(amplitude) + 1., 20)
        #    chisqmin = MAX_FLOAT
        #    best = None
        #    for t0, a in product(t0_grid, a_grid):
        #        chisq = 0.
        #        for band in bands:
        #            mflux = a * modelflux[band](datatimes[band] - t0)
        #            chisq += ((dataflux[band] - mflux) / datafluxerr[band])**2
        #        if chisq < chisqmin:
        #            chisqmin = chisq
        #            best = t0, a
        #
        #    model.parameters[1] = best[0]
        #    model.parameters[2] = best[1]

    # add model's default bounds:
    for name in param_names:
        if name not in bounds:
            i = model.param_names.index(name)
            bounds[name] = model.param_bounds[i]

    # count degrees of freedom
    ndof = len(data) - len(param_names)

    # Indicies of the model parameters in param_names 
    idx = np.array([model.param_names.index(name) for name in param_names])

    # define chi2 where input is array_like
    def chi2_arraylike(parameters):
        model._parameters[idx] = parameters
        modelflux = model.bandflux(data['band'], data['time'],
                                   zp=data['zp'], zpsys=data['zpsys'])
        return np.sum(((data['flux'] - modelflux) / data['fluxerr'])**2)

    if method == 'iminuit':
        try:
            import iminuit
        except ImportError:
            raise ValueError("Minimization method 'iminuit' requires the "
                             "iminuit package")

        # The iminuit minimizer expects the function signature to have an
        # argument for each parameter.
        def chi2(*parameters):
            return chi2_arraylike(parameters)

        # Set up keyword arguments to pass to Minuit initializer
        kwargs = {}
        for name in param_names:

            # starting point
            i = model.param_names.index(name)
            kwargs[name] = model.parameters[i]  # starting point


            lo, hi = bounds[name]
            if lo is not None and hi is not None:
                # iminuit doesn't like bounds to be None...
                #if lo is None:
                #    lo = -MAX_FLOAT
                #if hi is None:
                #    hi = MAX_FLOAT
                kwargs['limit_' + name] = (lo, hi)

            # set initial step size
            if bounds[name][0] is not None and bounds[name][1] is not None:
                step = 0.02 * (bounds[name][1] - bounds[name][0])
            elif model.parameters[i] != 0.:
                step = 0.1 * model.parameters[i]
            else:
                step = 1.
            kwargs['error_' + name] = step

        if disp:
            print "Initial parameters:"
            for name in param_names:
                print name, kwargs[name], 'step=', kwargs['error_' + name],
                if 'limit_' + name in kwargs:
                    print 'bounds=', kwargs['limit_' + name]
                else:
                    print ''

        m = iminuit.Minuit(chi2,
                           errordef=1.,
                           forced_parameters=param_names,
                           print_level=(1 if disp else 0),
                           throw_nan=True,
                           **kwargs)
        d, l = m.migrad(ncall=maxiter)

        # Compile results
        res = Result(
            success=d.is_valid, # need to check if hesse succeeds as well!
            message='',
            ncall=d.nfcn,
            fval=d.fval,
            param_names=m.values.keys(),
            values=m.values,
            errors=m.errors,
            covariance=m.covariance,
            ndof=ndof
            )
        return res, model

def old_fit_lc(data, model, param_names, p0=None, bounds=None,
               t0_range=20., include_model_error=False,
               fit_offset=False, offset_zp=25., offset_zpsys='ab',
               method='iminuit', return_minuit=False, max_ncall=10000,
               print_level=0):
    """Fit model parameters to data by minimizing chi^2.

    Ths function defines a chi^2 to minimize, makes initial guesses for
    some standard model parameters, such as 't0' and 'fscale', based on
    the data, then runs a minimizer.

    Parameters
    ----------
    data : `~numpy.ndarray` or `dict` of list_like
        Table of photometric data. Must include certain column names.
    model : `~sncosmo.Model`
        The model to fit.
    param_names : list
        Model parameters to vary in the fit.
    p0 : `dict`, optional
        If given, use these initial parameters in fit. Default is to use
        current model parameters.
    bounds : `dict`, optional
        Bounded range for each parameter. Keys should be parameter names,
        values are tuples. If a bound is not given for some parameter,
        the parameter is unbounded. The exception is ``t0``, which has a
        default bound of  ``(initial guess) +/- t0_range``.
    t0_range : float, optional
        Bounds for t0 (if varied in fit and not given in `bounds`).
        Default is 20.
    include_model_error : bool, optional
        Default is False.
    fit_offset : bool or list of str, optional
        Fit for the "offset flux value" in each bandpass
        specified. The "offset flux value" is added to the model flux
        in the fit. If a list is supplied, it should be a list of bandpass
        names for which to fit the offset. If a bool is supplied, then
        the offset will be fit for all bandpasses if True or for no
        bandpasses if False. The default is False.
    offset_zp : float
        Default is 25.
    offset_zpsys : `~sncosmo.MagSystem` or str, optional
        Default is 'ab'.
    method : {'iminuit', 'l-bfgs-b'}, optional
        Minimization method to use.
    return_minuit : bool, optional
        For method 'iminuit', return the `~iminuit.Minuit` object used in
        the fit.
    print_level : int, optional
        Print level. 0 is no output, 1 is standard amount.

    Returns
    -------
    res : Result
        The optimization result represented as a ``Result`` object (a dict
        subclass with attribute access). Some important attributes:

        - ``res.params``: dictionary of best-fit parameter values.
        - ``res.fval``: Minimum chi squared value.
        - ``res.ncalls``: Number of function calls.

        See ``res.keys()`` for available attributes.

    m : `~iminuit.Minuit`
        Only returned if method is 'iminuit' and `return_minuit` is True.

    Notes
    -----
    No notes at this time.
    """

    data = standardize_data(data)
    unique_bandnames = [get_bandpass(band).name
                        for band in set(data['band'].tolist())]

    method = method.lower()
    if fit_offset == True:
        fit_offset = unique_bandnames

    if fit_offset:
        data = normalize_data(data, zp=offset_zp, zpsys=offset_zpsys)

    # Get a shallow copy of the model so that we can change the parameters
    # without worrying.
    model = get_model(model, copy=True)

    # Check that 'z' is bounded (if it is going to be fit).
    if 'z' in param_names and (bounds is None or 'z' not in bounds):
        raise ValueError('z must be bounded if fit.')

    # Cut bands that are not allowed by the wavelength range of the model
    if 'z' not in param_names:
        valid = model.bandoverlap(data['band'])
    else:
        valid = model.bandoverlap(data['band'], z=bounds['z'])
        valid = np.all(valid, axis=1)
    if not np.all(valid):
        drop_bands = [repr(b) for b in set(data['band'][np.invert(valid)])]
        warn("Dropping following bands from data: " + ", ".join(drop_bands) +
             "(out of model wavelength range)", RuntimeWarning)
        data = data[valid]

    # Set initial parameters, to help with guessing parameters, below.
    if p0 is None:
        p0 = {}
    if 'z' in param_names and 'z' not in p0:
        p0['z'] = sum(bounds['z']) / 2.
    model.set(**p0)

    # Get list of initial guesses.
    if fit_offset:
        guesses = guess_parvals(data, model, param_names=['t0', 'fscale'])
    else:
        ndata = normalize_data(data, zp=offset_zp, zpsys=offset_zpsys)
        guesses = guess_parvals(ndata, model, param_names=['t0', 'fscale'])

    # Set initial parameters. Order of priority: 
    #   1. p0
    #   2. guesses
    #   3. current params (if not None)
    #   4. 0.
    parvals0 = []
    current = model.params
    for name in param_names:
        if name in p0:
            parvals0.append(p0[name])
        elif name in guesses:
            parvals0.append(guesses[name])
        elif current[name] is not None:
            parvals0.append(current[name])
        else:
            parvals0.append(0.)

    # Add parameters for offset, if we're fitting it.
    # TODO: Make this work with Bandpass objects not in registry.
    if fit_offset:
        offset_to_data = {} # map offset param to data idx
        for bandname in fit_offset:
            parname = 'offset_' + bandname
            idx = data['band'] == bandname
            param_names.append(parname)
            parvals0.append(np.min(data['flux'][idx]))
            offset_to_data[parname] = idx

    # Set up a complete list of bounds.
    bounds_list = []
    for name in param_names:
        if bounds is not None and name in bounds:
            bounds_list.append(bounds[name])
        elif name == 't0':
            i = param_names.index('t0')
            bounds_list.append((parvals0[i] - t0_range,
                                parvals0[i] + t0_range))
        else:
            bounds_list.append((None, None))

    # count degrees of freedom
    ndof = len(data) - len(param_names)

    if print_level > 0:
        print "starting point:"
        for name, val, bound in zip(param_names, parvals0, bounds_list):
            print "   ", name, val, bound

    fscale_factor = 1.

    # define chi2 where input is array_like
    def chi2_array_like(parvals):
        params = dict(zip(param_names, parvals))

        if 'fscale' in params:
            params['fscale'] *= fscale_factor
        model.set(**params)

        if include_model_error:
            mflux, mfluxerr = \
                model.bandflux(data['band'], data['time'], zp=data['zp'],
                               zpsys=data['zpsys'], include_error=True)
            denom = mfluxerr**2 + data['fluxerr']**2

        else:
            mflux = model.bandflux(data['band'], data['time'],
                                   zp=data['zp'], zpsys=data['zpsys'])
            denom = data['fluxerr']**2

        if fit_offset:
            for key, value in params.iteritems():
                if key[0:7] == 'offset_':
                    idx = offset_to_data[key]
                    mflux[idx] += value

        return np.sum((data['flux'] - mflux)**2 / denom)

    if method == 'iminuit':
        try:
            import iminuit
        except ImportError:
            raise ValueError("Minimization method 'iminuit' requires the "
                             "iminuit package")

        # The iminuit minimizer expects the function signature to have an
        # argument for each parameter.
        def chi2(*parvals):
            return chi2_array_like(parvals)

        # Set up keyword arguments to pass to Minuit initializer
        kwargs = {}
        for parname, parval, bounds in zip(param_names, parvals0, bounds_list):
            kwargs[parname] = parval
            if bounds is not None and None not in bounds:
                kwargs['limit_' + parname] = bounds
            if parname == 't0':
                step_size = 1.
            if parname == 'fscale':
                step_size = 0.1 * parval
            if parname == 'z':
                step_size = 0.05
            else:
                step_size = 1.
            kwargs['error_' + parname] = step_size

        m = iminuit.Minuit(chi2, errordef=1., forced_parameters=param_names,
                           print_level=print_level, **kwargs)
        d, l = m.migrad(ncall=max_ncall)
        res = Result(ncalls=d.nfcn, fval=d.fval, params=m.values,
                     errors=m.errors, covariance=m.covariance,
                     matrix=m.matrix(), ndof=ndof)

    elif method == 'l-bfgs-b':
        from scipy.optimize import fmin_l_bfgs_b

        # Scale 'fscale' to ~1 for numerical precision reasons.
        if 'fscale' in param_names:
            i = param_names.index('fscale')
            fscale_factor = parvals0[i]
            parvals0[i] = 1.
            if 'fscale' in bounds:
                bounds_list[i] = (bounds_list[i][0] / fscale_factor,
                                  bounds_list[i][1] / fscale_factor)

        x, f, d = fmin_l_bfgs_b(chi2_array_like, parvals0,
                                bounds=bounds_list, approx_grad=True,
                                iprint=(print_level - 1))

        d['ncalls'] = d.pop('funcalls')
        res = Result(d)
        res.params = dict(zip(param_names, x))
        res.fval = f
        res.ndof = ndof

        # adjust fscale
        if 'fscale' in res.values:
            res.values['fscale'] *= fscale_factor

    else:
        raise ValueError('Unknown solver %s' % method)

    # append offsets to result by bandname
    if fit_offset:
        res.offsets = {}
        for key, value in res.params.iteritems():
            if key[0:7] == 'offset_':
                res.offsets[key[7:]] = value

    if method == 'iminuit' and return_minuit:
        return res, m
    else:
        return res

# ------------------------------------------------------------------------
# This is the code for adding tied parameters to loglikelihood in nest_lc
# (d is a dictionary of parameters)

#         if tied is not None:
#            for parname, func in tied.iteritems():
#                d[parname] = func(d)



# ---------------------------------------------------------------------
# This is the code for adding tied parameters to results of nest_lc
# before returning

#    # Add tied parameters to results. This is inelegant, but, eh.
#    nsamples = len(res['samples_parvals'])
#    res['nsamples'] = nsamples
#    if tied is not None:
#        tiedparnames = tied.keys()
#        ntiedpar = len(tiedparnames)
#        tiedparvals = np.empty((nsamples, ntiedpar), dtype=np.float)
#        for i in range(nsamples):
#            d = dict(zip(parnames, res['samples_parvals'][i, :]))
#            for j, parname in enumerate(tiedparnames):
#                tiedparvals[i, j] = tied[parname](d)
#
#        res['samples_parvals'] = np.hstack((res['samples_parvals'], 
#                                            tiedparvals))
#        parnames = parnames + tiedparnames
#
#    # Sample averages and their standard deviations.
#    res['parvals'] = np.average(res['samples_parvals'],
#                                weights=res['samples_wt'], axis=0)
#    res['parerrs'] = np.sqrt(np.sum(res['samples_wt'][:, np.newaxis] *
#                             res['samples_parvals']**2, axis=0) -
#                             res['parvals']**2)
#
#    # Add some more to results
#    res['parnames'] = parnames
#    res['chisq_min'] = -2. * res.pop('loglmax')
#    res['dof'] = len(data) - npar
#
#    return res

def _nest_lc(data, model, param_names,
             bounds=None, priors=None, ppfs=None,
             nobj=100, maxiter=10000, verbose=False):
    """Assumes that data has already been standardized."""

    # Indicies of the model parameters in param_names 
    idx = np.array([model.param_names.index(name) for name in param_names])

    # Set up a list of ppfs to be used in the prior() function.
    npar = len(param_names)
    ppflist = npar * [None]

    # If a ppf is directly supplied for a parameter, it takes precedence.
    if ppfs is not None:
        for i, param_name in enumerate(param_names):
            if param_name in ppfs:
                ppflist[i] = ppfs[param_name]

    # For parameters without ppfs, construct one from bounds and prior.
    for i, param_name in enumerate(param_names):
        if ppflist[i] is not None:
            continue
        if param_name not in bounds:
            raise ValueError("Must supply ppf or limits for parameter '{}'"
                             .format(param_name))
        a, b = bounds[param_name]
        if (priors is not None and param_name in priors):
            ppflist[i] = pdf_to_ppf(priors[param_name], a, b)
        else:
            ppflist[i] = Interp1d(0., 1., np.array([a, b]))

    def prior(u):
        v = np.empty(npar, dtype=np.float)
        for i in range(npar):
            v[i] = ppflist[i](u[i])
        return v

    def loglikelihood(parameters):
        model.parameters[idx] = parameters
        mflux = model.bandflux(data['band'], data['time'],
                               zp=data['zp'], zpsys=data['zpsys'])
        chisq = np.sum(((data['flux'] - mflux) / data['fluxerr'])**2)
        return -chisq / 2.

    res = nest.nest(loglikelihood, prior, npar, nobj=nobj, maxiter=maxiter,
                    verbose=verbose)
    res.param_names = param_names
    return res

def nest_lc(data, model, param_names, bounds=None, priors=None,
            nobj=100, maxiter=10000, verbose=False):
    """Run nested sampling algorithm to estimate model parameters and evidence.

    Parameters
    ----------
    model : `~sncosmo.ObsModel`
    data : `~astropy.table.Table` or `~numpy.ndarray`
    param_names : list of str
    bounds : dict
    priors : dict
    nobj : 
    maxiter :
    verbose :

    Returns
    -------
    res : Result
    model : `~sncosmo.ObsModel`
        Copy of model with parameters set.
    """

    data = standardize_data(data)
    model = copy.copy(model)
    res = _nest_lc(data, model, param_names, bounds=bounds, priors=priors,
                   nobj=nobj, maxiter=maxiter, verbose=verbose)
    
    # Calculate 'best' values and set a copy of the model to them
    parameters = np.average(res['samples'], weights=res['weights'], axis=0)
    model.set(**dict(zip(param_names, parameters)))

    return res, model

def mcmc_lc(data, model, parnames, p0=None, errors=None, nwalkers=10,
            nburn=100, nsamples=500, return_sampler=False, verbose=False):
    """Run an MCMC chain to get model parameter samples.

    Parameters
    ----------
    data : `~numpy.ndarray` or `dict` of list_like
        Table of photometric data. Must include certain column names.
    model : `~sncosmo.Model`
        The model to fit.
    parnames : list
        Model parameters to vary in the fit.
    p0 : `dict`, optional
        If given, use these initial parameters in fit. Default is to use
        current model parameters.
    errors : `dict`, optional
    nwalkers : int, optional
    nburn : int, optional
    nsamples : int, optional
    return_sampler : bool, optional
    verbose : bool, optional

    Returns
    -------
    samples : `~numpy.ndarray`
        The shape is (nsamples * nwalkers, npar).
    """

    try:
        import emcee
    except:
        raise ImportError("mcmc_lc() requires the emcee package.")

    data = standardize_data(data)
    model = get_model(model)
    ndim = len(parnames)

    # --------------------- COPIED FROM FIT_LC ----------------------------
    # Set initial parameters, to help with guessing parameters, below.
    if p0 is not None:
        model.set(**p0)

    # Get list of initial guesses.
    ndata = normalize_data(data, zp=25., zpsys='ab')
    guesses = guess_parvals(ndata, model, parnames=['t0', 'fscale'])

    # Set initial parameters. Order of priority: 
    #   1. p0
    #   2. guesses
    #   3. current params (if not None)
    #   4. 0.
    parvals0 = []
    current = model.params
    for name in parnames:
        if p0 is not None and name in p0:
            parvals0.append(p0[name])
        elif name in guesses:
            parvals0.append(guesses[name])
        elif current[name] is not None:
            parvals0.append(current[name])
        else:
            parvals0.append(0.)
    # --------------------- END OF COPY FROM FIT_LC ------------------------

    step_sizes = []
    for parname, parval in zip(parnames, parvals0):
        if errors is not None and parname in errors:
            step_size = errors[parname]
        elif parname == 't0':
            step_size = 0.5
        elif parname == 'fscale':
            step_size = 0.1 * parval
        elif parname == 'z':
            step_size = 0.05
        else:
            step_size = 0.1
        step_sizes.append(step_size)

    # Starting positions of walkers.
    randarr = np.random.rand(ndim * nwalkers).reshape((nwalkers, ndim))
    start = np.array(parvals0) + np.array(step_sizes) * (randarr - 0.5)

    # define likelihood
    def loglikelihood(parvals):
        params = dict(zip(parnames, parvals))
        model.set(**params)
        mflux = model.bandflux(data['band'], data['time'],
                               zp=data['zp'], zpsys=data['zpsys'])
        return -0.5 * np.sum(((data['flux'] - mflux) / data['fluxerr']) ** 2)

    # Create sampler
    sampler = emcee.EnsembleSampler(nwalkers, ndim, loglikelihood)

    # burn-in
    pos, prob, state = sampler.run_mcmc(start, nburn)
    sampler.reset()

    # production run
    sampler.run_mcmc(pos, nsamples)
    if verbose:
        print "Avg acceptance fraction:", np.mean(sampler.acceptance_fraction)

    if return_sampler:
        return sampler
    else:
        return sampler.flatchain

