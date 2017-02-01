# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function
import os
import json
import copy
import pprint
import logging
import numpy as np
from astropy.io import fits
from astropy.coordinates import SkyCoord
from astropy.table import Table, Column
import fermipy.config
import fermipy.utils as utils
import fermipy.wcs_utils as wcs_utils
from fermipy import fits_utils
from fermipy.sourcefind_utils import fit_error_ellipse
from fermipy.sourcefind_utils import find_peaks
from fermipy.skymap import Map
from fermipy.config import ConfigSchema
from fermipy.gtutils import FreeParameterState, SourceMapState
from fermipy.model_utils import get_function_norm_par_name
from LikelihoodState import LikelihoodState
import pyLikelihood as pyLike


class SourceFind(object):
    """Mixin class which provides source-finding functionality to
    `~fermipy.gtanalysis.GTAnalysis`."""

    def find_sources(self, prefix='', **kwargs):
        """An iterative source-finding algorithm.

        Parameters
        ----------

        model : dict
           Dictionary defining the properties of the test source.
           This is the model that will be used for generating TS maps.

        sqrt_ts_threshold : float
           Source threshold in sqrt(TS).  Only peaks with sqrt(TS)
           exceeding this threshold will be used as seeds for new
           sources.

        min_separation : float
           Minimum separation in degrees of sources detected in each
           iteration. The source finder will look for the maximum peak
           in the TS map within a circular region of this radius.

        max_iter : int
           Maximum number of source finding iterations.  The source
           finder will continue adding sources until no additional
           peaks are found or the number of iterations exceeds this
           number.

        sources_per_iter : int
           Maximum number of sources that will be added in each
           iteration.  If the number of detected peaks in a given
           iteration is larger than this number, only the N peaks with
           the largest TS will be used as seeds for the current
           iteration.

        tsmap_fitter : str
           Set the method used internally for generating TS maps.
           Valid options:

           * tsmap
           * tscube

        tsmap : dict
           Keyword arguments dictionary for tsmap method.

        tscube : dict
           Keyword arguments dictionary for tscube method.


        Returns
        -------

        peaks : list
           List of peak objects.

        sources : list
           List of source objects.

        """

        self.logger.info('Starting.')

        schema = ConfigSchema(self.defaults['sourcefind'],
                              tsmap=self.defaults['tsmap'],
                              tscube=self.defaults['tscube'])

        schema.add_option('search_skydir', None, '', SkyCoord)
        schema.add_option('search_minmax_radius', [None, 1.0], '', list)

        config = utils.create_dict(self.config['sourcefind'],
                                   tsmap=self.config['tsmap'],
                                   tscube=self.config['tscube'])
        config = schema.create_config(config, **kwargs)

        # Defining default properties of test source model
        config['model'].setdefault('Index', 2.0)
        config['model'].setdefault('SpectrumType', 'PowerLaw')
        config['model'].setdefault('SpatialModel', 'PointSource')
        config['model'].setdefault('Prefactor', 1E-13)

        o = {'sources': [], 'peaks': []}

        for i in range(config['max_iter']):
            srcs, peaks = self._find_sources_iterate(prefix, i, **config)

            self.logger.info('Found %i sources in iteration %i.' %
                             (len(srcs), i))

            o['sources'] += srcs
            o['peaks'] += peaks
            if len(srcs) == 0:
                break

        self.logger.info('Done.')

        return o

    def _build_src_dicts_from_peaks(self, peaks, maps, src_dict_template):

        tsmap = maps['ts']
        amp = maps['amplitude']

        src_dicts = []
        names = []

        for p in peaks:

            o, skydir = fit_error_ellipse(tsmap, (p['ix'], p['iy']), dpix=2)
            p['fit_loc'] = o
            p['fit_skydir'] = skydir

            p.update(o)

            if o['fit_success']:
                skydir = p['fit_skydir']
            else:
                skydir = p['skydir']

            name = utils.create_source_name(skydir)
            src_dict = copy.deepcopy(src_dict_template)
            norm_par = get_function_norm_par_name(
                src_dict_template['SpectrumType'])
            src_dict.update({norm_par: amp.counts[p['iy'], p['ix']],
                             'ra': skydir.icrs.ra.deg,
                             'dec': skydir.icrs.dec.deg})

            src_dict['pos_sigma'] = o['sigma']
            src_dict['pos_sigma_semimajor'] = o['sigma_semimajor']
            src_dict['pos_sigma_semiminor'] = o['sigma_semiminor']
            src_dict['pos_r68'] = o['r68']
            src_dict['pos_r95'] = o['r95']
            src_dict['pos_r99'] = o['r99']
            src_dict['pos_angle'] = np.degrees(o['theta'])

            self.logger.info('Found source\n' +
                             'name: %s\n' % name +
                             'ts: %f' % p['amp'] ** 2)

            names.append(name)
            src_dicts.append(src_dict)

        return names, src_dicts

    def _find_sources_iterate(self, prefix, iiter, **kwargs):

        src_dict_template = kwargs.pop('model')

        threshold = kwargs.get('sqrt_ts_threshold')
        min_separation = kwargs.get('min_separation')
        sources_per_iter = kwargs.get('sources_per_iter')
        search_skydir = kwargs.get('search_skydir', None)
        search_minmax_radius = kwargs.get('search_minmax_radius', [None, 1.0])
        tsmap_fitter = kwargs.get('tsmap_fitter', 'tsmap')
        free_params = kwargs.get('free_params', None)
        if not free_params:
            free_params = None

        if tsmap_fitter == 'tsmap':
            kw = kwargs.get('tsmap', {})
            kw['model'] = src_dict_template
            m = self.tsmap('%s_sourcefind_%02i' % (prefix, iiter),
                           **kw)

        elif tsmap_fitter == 'tscube':
            kw = kwargs.get('tscube', {})
            kw['model'] = src_dict_template
            m = self.tscube('%s_sourcefind_%02i' % (prefix, iiter),
                            **kw)
        else:
            raise Exception(
                'Unrecognized option for fitter: %s.' % tsmap_fitter)

        if tsmap_fitter == 'tsmap':
            peaks = find_peaks(m['sqrt_ts'], threshold, min_separation)
            (names, src_dicts) = \
                self._build_src_dicts_from_peaks(peaks, m, src_dict_template)
        elif tsmap_fitter == 'tscube':
            sd = m['tscube'].find_sources(threshold ** 2, min_separation,
                                          use_cumul=False,
                                          output_src_dicts=True,
                                          output_peaks=True)
            peaks = sd['Peaks']
            names = sd['Names']
            src_dicts = sd['SrcDicts']

        # Loop over the seeds and add them to the model
        new_src_names = []
        for name, src_dict in zip(names, src_dicts):
            # Protect against finding the same source twice
            if self.roi.has_source(name):
                self.logger.info('Source %s found again.  Ignoring it.' % name)
                continue
            # Skip the source if it's outside the search region
            if search_skydir is not None:

                skydir = SkyCoord(src_dict['ra'], src_dict['dec'], unit='deg')
                separation = search_skydir.separation(skydir).deg

                if not utils.apply_minmax_selection(separation,
                                                    search_minmax_radius):
                    self.logger.info('Source %s outside of '
                                     'search region.  Ignoring it.',
                                     name)
                    continue

            self.add_source(name, src_dict, free=True)
            self.free_source(name, False)
            new_src_names.append(name)

            if len(new_src_names) >= sources_per_iter:
                break

        # Re-fit spectral parameters of each source individually
        for name in new_src_names:
            self.logger.info('Performing spectral fit for %s.', name)
            self.logger.debug(pprint.pformat(self.roi[name].params))
            self.free_source(name, True, pars=free_params)
            self.fit()
            self.logger.info(pprint.pformat(self.roi[name].params))
            self.free_source(name, False)

        srcs = []
        for name in new_src_names:
            srcs.append(self.roi[name])

        return srcs, peaks

    def localize(self, name, **kwargs):
        """Find the best-fit position of a source.  Localization is
        performed in two steps.  First a TS map is computed centered
        on the source with half-width set by ``dtheta_max``.  A fit is
        then performed to the maximum TS peak in this map.  The source
        position is then further refined by scanning the likelihood in
        the vicinity of the peak found in the first step.  The size of
        the scan region is set to encompass the 99% positional
        uncertainty contour as determined from the peak fit.

        Parameters
        ----------
        name : str
            Source name.

        {options}

        optimizer : dict
            Dictionary that overrides the default optimizer settings.

        Returns
        -------
        localize : dict
            Dictionary containing results of the localization
            analysis.

        """
        name = self.roi.get_source_by_name(name).name

        schema = ConfigSchema(self.defaults['localize'],
                              optimizer=self.defaults['optimizer'])
        schema.add_option('use_cache', True)
        schema.add_option('prefix', '')
        config = utils.create_dict(self.config['localize'],
                                   optimizer=self.config['optimizer'])
        config = schema.create_config(config, **kwargs)

        self.logger.info('Running localization for %s' % name)

        free_state = FreeParameterState(self)
        loc = self._localize(name, **config)
        free_state.restore()

        self.logger.info('Finished localization.')

        if config['make_plots']:
            self._plotter.make_localization_plots(loc, self.roi,
                                                  prefix=config['prefix'])

        outfile = \
            utils.format_filename(self.workdir, 'loc',
                                  prefix=[config['prefix'],
                                          name.lower().replace(' ', '_')])

        if config['write_fits']:
            loc['file'] = os.path.basename(outfile) + '.fits'
            self._make_localize_fits(loc, outfile + '.fits',
                                     **config)

        if config['write_npy']:
            np.save(outfile + '.npy', loc)

        return loc

    def _make_localize_fits(self, loc, filename, **kwargs):

        tab = fits_utils.dict_to_table(loc)
        hdu_data = fits.table_to_hdu(tab)
        hdu_data.name = 'LOC_DATA'

        hdus = [loc['tsmap_peak'].create_primary_hdu(),
                loc['tsmap'].create_image_hdu('TSMAP'),
                hdu_data]

        hdus[0].header['CONFIG'] = json.dumps(loc['config'])
        hdus[2].header['CONFIG'] = json.dumps(loc['config'])
        fits_utils.write_hdus(hdus, filename)

    def _localize(self, name, **kwargs):

        nstep = kwargs.get('nstep')
        dtheta_max = kwargs.get('dtheta_max')
        update = kwargs.get('update', True)
        prefix = kwargs.get('prefix', '')
        use_cache = kwargs.get('use_cache', False)
        free_background = kwargs.get('free_background', False)
        free_radius = kwargs.get('free_radius', None)

        saved_state = LikelihoodState(self.like)

        if not free_background:
            self.free_sources(free=False, loglevel=logging.DEBUG)

        if free_radius is not None:
            diff_sources = [s.name for s in self.roi.sources if s.diffuse]
            skydir = self.roi[name].skydir
            free_srcs = [s.name for s in
                         self.roi.get_sources(skydir=skydir,
                                              distance=free_radius,
                                              exclude=diff_sources)]
            self.free_sources_by_name(free_srcs, pars='norm',
                                      loglevel=logging.DEBUG)

        src = self.roi.copy_source(name)
        skydir = src.skydir
        skywcs = self._skywcs
        src_pix = skydir.to_pixel(skywcs)

        fit0 = self._fit_position_tsmap(name, prefix=prefix,
                                        dtheta_max=dtheta_max)

        self.logger.debug('Completed localization with TS Map.\n'
                          '(ra,dec) = (%10.4f,%10.4f)\n'
                          '(glon,glat) = (%10.4f,%10.4f)',
                          fit0['ra'], fit0['dec'],
                          fit0['glon'], fit0['glat'])

        # Fit baseline (point-source) model
        self.free_norm(name)
        fit_output = self._fit(loglevel=logging.DEBUG, **
                               kwargs.get('optimizer', {}))

        # Save likelihood value for baseline fit
        loglike0 = fit_output['loglike']
        self.logger.debug('Baseline Model Likelihood: %f', loglike0)

        o = {'name': name,
             'config': kwargs,
             'fit_success': True,
             'loglike_base': loglike0,
             'loglike_loc': np.nan,
             'dloglike_loc': np.nan}

        if fit0['fit_success']:
            scan_cdelt = 2.0 * fit0['r95'] / (nstep - 1.0)
        else:
            scan_cdelt = np.abs(skywcs.wcs.cdelt[0])

        self.logger.debug('Refining localization search to '
                          'region of width: %.4f deg',
                          scan_cdelt * nstep)

        fit1 = self._fit_position_scan(name,
                                       skydir=fit0['skydir'],
                                       scan_cdelt=scan_cdelt,
                                       **kwargs)

        o['loglike_loc'] = 0.5 * (np.max(fit1['tsmap'].data) + fit1['zoffset'])
        o['dloglike_loc'] = o['loglike_loc'] - o['loglike_base']
        o['tsmap'] = fit0.pop('tsmap')
        o['tsmap_peak'] = fit1.pop('tsmap')
        o.update(fit1)

        # Best fit position and uncertainty from fit to TS map
        o['fit_init'] = fit0

        # Best fit position and uncertainty from pylike scan
        o['fit_scan'] = fit1

        cdelt0 = np.abs(skywcs.wcs.cdelt[0])
        cdelt1 = np.abs(skywcs.wcs.cdelt[1])
        pix = fit1['skydir'].to_pixel(skywcs)
        o['xpix'] = float(pix[0])
        o['ypix'] = float(pix[1])
        o['deltax'] = (o['xpix'] - src_pix[0]) * cdelt0
        o['deltay'] = (o['ypix'] - src_pix[1]) * cdelt1
        o['offset'] = skydir.separation(fit1['skydir']).deg
        o['ra_preloc'] = skydir.ra.deg
        o['dec_preloc'] = skydir.dec.deg
        o['glon_preloc'] = skydir.galactic.l.deg
        o['glat_preloc'] = skydir.galactic.b.deg

        if o['offset'] > dtheta_max:
            o['fit_success'] = False

        self.logger.info('Localization completed with coordinates:\n'
                         '(ra,dec) = (%10.4f,%10.4f)\n'
                         '(glon,glat) = (%10.4f,%10.4f)\n'
                         'offset = %8.4f r68 = %8.4f r99 = %8.4f',
                         o['ra'], o['dec'],
                         o['glon'], o['glat'],
                         o['offset'], o['r68'], o['r99'])

        if not o['fit_success']:
            self.logger.warning('Fit to localization contour failed.')
        else:
            self.logger.info('Localization succeeded.')

        if update:
            self.logger.info('Updating source %s '
                             'to localized position.', name)
            src = self.delete_source(name)
            src.set_position(fit1['skydir'])
            self.add_source(name, src, free=True)
            fit_output = self.fit(loglevel=logging.DEBUG)
            o['loglike_loc'] = fit_output['loglike']
            o['dloglike_loc'] = o['loglike_loc'] - o['loglike_base']
            src = self.roi.get_source_by_name(name)
            self.logger.info('LogLike: %12.3f DeltaLogLike: %12.3f',
                             o['loglike_loc'], o['dloglike_loc'])

            src['pos_sigma'] = o['sigma']
            src['pos_sigma_semimajor'] = o['sigma_semimajor']
            src['pos_sigma_semiminor'] = o['sigma_semiminor']
            src['pos_r68'] = o['r68']
            src['pos_r95'] = o['r95']
            src['pos_r99'] = o['r99']
            src['pos_angle'] = np.degrees(o['theta'])
        else:
            saved_state.restore()
            self._sync_params(name)
            self._update_roi()

        return o

    def _fit_position(self, name, **kwargs):

        dtheta_max = kwargs.setdefault('dtheta_max', 0.5)
        nstep = kwargs.setdefault('nstep', 5)
        fit0 = self._fit_position_tsmap(name, **kwargs)

        scan_cdelt = min(2.0 * fit0['r68'] / (nstep - 1.0),
                         self._binsz)
        fit1 = self._fit_position_scan(name,
                                       skydir=fit0['skydir'],
                                       scan_cdelt=scan_cdelt,
                                       **kwargs)
        return fit1

    def _fit_position_tsmap(self, name, **kwargs):
        """Localize a source from its TS map."""

        prefix = kwargs.get('prefix', '')
        dtheta_max = kwargs.get('dtheta_max', 0.5)
        write_fits = kwargs.get('write_fits', False)
        write_npy = kwargs.get('write_npy', False)
        use_pylike = kwargs.get('use_pylike', True)
        zmin = kwargs.get('zmin', -9.0)

        src = self.roi.copy_source(name)
        skydir = kwargs.get('skydir', src.skydir)
        tsmap = self.tsmap(utils.join_strings([prefix, name.lower().
                                               replace(' ', '_')]),
                           model=src.data,
                           map_skydir=skydir,
                           map_size=2.0 * dtheta_max,
                           exclude=[name],
                           write_fits=write_fits,
                           write_npy=write_npy,
                           use_pylike=use_pylike,
                           make_plots=False,
                           loglevel=logging.DEBUG)

        ts_value = np.max(tsmap['ts'].counts)
        zmin = max(zmin, -ts_value * 0.5)
        posfit, skydir = fit_error_ellipse(tsmap['ts'], dpix=2,
                                           zmin=zmin)
        pix = skydir.to_pixel(self._skywcs)

        o = {}
        o.update(posfit)
        o['xpix'] = float(pix[0])
        o['ypix'] = float(pix[1])
        o['skydir'] = skydir.transform_to('icrs')
        o['offset'] = skydir.separation(self.roi[name].skydir).deg
        o['loglike'] = 0.5 * posfit['zoffset']
        o['tsmap'] = tsmap['ts']

        return o

    def _fit_position_scan(self, name, **kwargs):

        zmin = kwargs.get('zmin', -9.0)

        tsmap = self._scan_position(name, **kwargs)
        posfit, skydir = fit_error_ellipse(tsmap, dpix=2,
                                           zmin=zmin)
        pix = skydir.to_pixel(self._skywcs)

        o = {}
        o.update(posfit)
        o['xpix'] = float(pix[0])
        o['ypix'] = float(pix[1])
        o['skydir'] = skydir.transform_to('icrs')
        o['offset'] = skydir.separation(self.roi[name].skydir).deg
        o['loglike'] = 0.5 * posfit['zoffset']
        o['tsmap'] = tsmap

        return o

    def _scan_position(self, name, **kwargs):

        saved_state = LikelihoodState(self.like)

        skydir = kwargs.pop('skydir', self.roi[name].skydir)
        scan_cdelt = kwargs.pop('scan_cdelt', 0.02)
        nstep = kwargs.pop('nstep', 5)
        use_cache = kwargs.get('use_cache', True)
        use_pylike = kwargs.get('use_pylike', False)
        optimizer = kwargs.get('optimizer', {})

        self.free_norm(name)

        lnlmap = Map.create(skydir, scan_cdelt, (nstep, nstep),
                            coordsys=wcs_utils.get_coordsys(self._skywcs))

        src = self.roi.copy_source(name)

        if use_cache and not use_pylike:
            self._create_srcmap_cache(src.name, src)

        scan_skydir = lnlmap.get_pixel_skydirs().transform_to('icrs')
        loglike = []
        for ra, dec in zip(scan_skydir.ra.deg, scan_skydir.dec.deg):

            spatial_pars = {'ra': ra, 'dec': dec}
            self.set_source_morphology(name,
                                       spatial_pars=spatial_pars,
                                       use_pylike=use_pylike)
            fit_output = self._fit(loglevel=logging.DEBUG,
                                   **optimizer)
            loglike += [fit_output['loglike']]

        self.set_source_morphology(name, spatial_pars=src.spatial_pars,
                                   use_pylike=use_pylike)
        saved_state.restore()

        lnlmap.data = np.array(loglike).reshape((nstep, nstep)).T
        tsmap = Map(2.0 * lnlmap.data, lnlmap.wcs)

        self._clear_srcmap_cache()
        return tsmap

    def _fit_position_opt(self, name, use_cache=True):

        state = SourceMapState(self.like, [name])

        src = self.roi.copy_source(name)

        if use_cache:
            self._create_srcmap_cache(src.name, src)

        loglike = []
        skydir = src.skydir
        skywcs = self._skywcs
        src_pix = skydir.to_pixel(skywcs)

        c = skydir.transform_to('icrs')
        src.set_radec(c.ra.deg, c.dec.deg)
        self._update_srcmap(src.name, src)

        print(src_pix, self.like())

        import time

        def fit_fn(params):

            t0 = time.time()

            c = SkyCoord.from_pixel(params[0], params[1], self._skywcs)
            c = c.transform_to('icrs')
            src.set_radec(c.ra.deg, c.dec.deg)

            t1 = time.time()

            self._update_srcmap(src.name, src)

            t2 = time.time()

            val = self.like()

            t3 = time.time()

            print(params, val)
            # print(t1-t0,t2-t1,t3-t2)

            return val

        #lnl0 = fit_fn(src_pix[0],src_pix[1])
        #lnl1 = fit_fn(src_pix[0]+0.1,src_pix[1])
        # print(lnl0,lnl1)

        import scipy

        #src_pix[1] += 3.0
        p0 = [src_pix[0], src_pix[1]]

        #p0 = np.array([14.665692574327048, 16.004594098101926])
        #delta = np.array([0.3,-0.4])
        #p0 = [14.665692574327048, 16.004594098101926]

        o = scipy.optimize.minimize(fit_fn, p0,
                                    bounds=[(0.0, 39.0),
                                            (0.0, 39.0)],
                                    # method='L-BFGS-B',
                                    method='SLSQP',
                                    tol=1e-6)

        print ('fit 2')

        o = scipy.optimize.minimize(fit_fn, o.x,
                                    bounds=[(0.0, 39.0),
                                            (0.0, 39.0)],
                                    # method='L-BFGS-B',
                                    method='SLSQP',
                                    tol=1e-6)
        print(o)

        print(fit_fn(p0))
        print(fit_fn(o.x))
        print(fit_fn(o.x + np.array([0.02, 0.02])))
        print(fit_fn(o.x + np.array([0.02, -0.02])))
        print(fit_fn(o.x + np.array([-0.02, 0.02])))
        print(fit_fn(o.x + np.array([-0.02, -0.02])))

        state.restore()

        return o
