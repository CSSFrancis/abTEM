from collections import defaultdict
from functools import lru_cache
from typing import Mapping

import cupy as cp
import numpy as np
from abtem.bases import HasAcceleratorMixin, Accelerator, watched_property, Event, DeviceManager, \
    cache_clear_callback, cached_method, Cache
from abtem.config import DTYPE
from abtem.utils import energy2wavelength
from abtem.cpu_kernels import complex_exponential

polar_symbols = ('C10', 'C12', 'phi12',
                 'C21', 'phi21', 'C23', 'phi23',
                 'C30', 'C32', 'phi32', 'C34', 'phi34',
                 'C41', 'phi41', 'C43', 'phi43', 'C45', 'phi45',
                 'C50', 'C52', 'phi52', 'C54', 'phi54', 'C56', 'phi56')

polar_aliases = {'defocus': 'C10', 'astigmatism': 'C12', 'astigmatism_angle': 'phi12',
                 'coma': 'C21', 'coma_angle': 'phi21',
                 'Cs': 'C30',
                 'C5': 'C50'}


def calculate_symmetric_chi(alpha: np.ndarray, wavelength: float, parameters: Mapping[str, float]) -> np.ndarray:
    """
    Calculates the first three symmetric terms in the phase error expansion.

    See Eq. 2.6 in ref [1].

    Parameters
    ----------
    alpha : numpy.ndarray
        Angle between the scattered electrons and the optical axis.
    wavelength : float
        Relativistic wavelength of wavefunction.
    parameters : Mapping[str, float]
        Mapping from Cn0 coefficients to its corresponding value.
    Returns
    -------

    References
    ----------
    .. [1] Kirkland, E. J. (2010). Advanced Computing in Electron Microscopy (2nd ed.). Springer.

    """
    alpha2 = alpha ** 2
    return 2 * np.pi / wavelength * (1 / 2. * alpha2 * parameters['C10'] +
                                     1 / 4. * alpha2 ** 2 * parameters['C30'] +
                                     1 / 6. * alpha2 ** 3 * parameters['C50'])


def calculate_polar_chi(alpha: np.ndarray, phi: np.ndarray, wavelength: float,
                        parameters: Mapping[str, float]) -> np.ndarray:
    """
    Calculates the polar expansion of the phase error up to 5th order.

    See Eq. 2.22 in ref [1].

    Parameters
    ----------
    alpha : numpy.ndarray
        Angle between the scattered electrons and the optical axis.
    phi : numpy.ndarray
        Angle around the optical axis of the scattered electrons.
    wavelength : float
        Relativistic wavelength of wavefunction.
    parameters : Mapping[str, float]
        Mapping from Cnn, phinn coefficients to their corresponding values. See parameter `parameters` in class CTFBase.

    Returns
    -------

    References
    ----------
    .. [1] Kirkland, E. J. (2010). Advanced Computing in Electron Microscopy (2nd ed.). Springer.

    """
    xp = cp.get_array_module(alpha)
    alpha2 = alpha ** 2
    array = xp.zeros(alpha.shape, dtype=DTYPE)
    if any([parameters[symbol] != 0. for symbol in ('C10', 'C12', 'phi12')]):
        array += (1 / 2 * alpha2 *
                  (parameters['C10'] +
                   parameters['C12'] * xp.cos(2 * (phi - parameters['phi12']))))

    if any([parameters[symbol] != 0. for symbol in ('C21', 'phi21', 'C23', 'phi23')]):
        array += (1 / 3 * alpha2 * alpha *
                  (parameters['C21'] * xp.cos(phi - parameters['phi21']) +
                   parameters['C23'] * xp.cos(3 * (phi - parameters['phi23']))))

    if any([parameters[symbol] != 0. for symbol in ('C30', 'C32', 'phi32', 'C34', 'phi34')]):
        array += (1 / 4 * alpha2 ** 2 *
                  (parameters['C30'] +
                   parameters['C32'] * xp.cos(2 * (phi - parameters['phi32'])) +
                   parameters['C34'] * xp.cos(4 * (phi - parameters['phi34']))))

    if any([parameters[symbol] != 0. for symbol in ('C41', 'phi41', 'C43', 'phi43', 'C45', 'phi41')]):
        array += (1 / 5 * alpha2 ** 2 * alpha *
                  (parameters['C41'] * xp.cos((phi - parameters['phi41'])) +
                   parameters['C43'] * xp.cos(3 * (phi - parameters['phi43'])) +
                   parameters['C45'] * xp.cos(5 * (phi - parameters['phi45']))))

    if any([parameters[symbol] != 0. for symbol in ('C50', 'C52', 'phi52', 'C54', 'phi54', 'C56', 'phi56')]):
        array += (1 / 6 * alpha2 ** 3 *
                  (parameters['C50'] +
                   parameters['C52'] * xp.cos(2 * (phi - parameters['phi52'])) +
                   parameters['C54'] * xp.cos(4 * (phi - parameters['phi54'])) +
                   parameters['C56'] * xp.cos(6 * (phi - parameters['phi56']))))

    array = 2 * xp.pi / wavelength * array
    return array


def calculate_symmetric_aberrations(alpha: np.ndarray, wavelength: float,
                                    parameters: Mapping[str, float]) -> np.ndarray:
    return complex_exponential(-calculate_symmetric_chi(alpha, wavelength, parameters))


def calculate_polar_aberrations(alpha: (np.ndarray, cp.ndarray), phi: np.ndarray, wavelength: float,
                                parameters: Mapping[str, float]) -> np.ndarray:
    return complex_exponential(-calculate_polar_chi(alpha, phi, wavelength, parameters))


def calculate_aperture(alpha: np.ndarray, cutoff: float, rolloff: float) -> np.ndarray:
    xp = cp.get_array_module(alpha)
    if rolloff > 0.:
        rolloff *= cutoff
        array = .5 * (1 + xp.cos(np.pi * (alpha - cutoff + rolloff) / rolloff))
        array[alpha > cutoff] = 0.
        array = xp.where(alpha > cutoff - rolloff, array, xp.ones_like(alpha, dtype=DTYPE))
    else:
        array = xp.array(alpha < cutoff).astype(DTYPE)
    return array


def calculate_temporal_envelope(alpha: np.ndarray, wavelength: float, focal_spread: float) -> np.ndarray:
    return DTYPE(np.exp(- (.5 * np.pi / wavelength * focal_spread * alpha ** 2) ** 2))


def calculate_gaussian_envelope(alpha: np.ndarray, wavelength: float, gaussian_spread: float) -> np.ndarray:
    return DTYPE(np.exp(- .5 * gaussian_spread ** 2 * alpha ** 2 / wavelength ** 2))


def calculate_spatial_envelope(alpha, phi, wavelength, angular_spread, parameters):
    dchi_dk = 2 * np.pi / wavelength * (
            (parameters['C12'] * np.cos(2. * (phi - parameters['phi12'])) + parameters['C10']) * alpha +
            (parameters['C23'] * np.cos(3. * (phi - parameters['phi23'])) +
             parameters['C21'] * np.cos(1. * (phi - parameters['phi21']))) * alpha ** 2 +
            (parameters['C34'] * np.cos(4. * (phi - parameters['phi34'])) +
             parameters['C32'] * np.cos(2. * (phi - parameters['phi32'])) + parameters['C30']) * alpha ** 3 +
            (parameters['C45'] * np.cos(5. * (phi - parameters['phi45'])) +
             parameters['C43'] * np.cos(3. * (phi - parameters['phi43'])) +
             parameters['C41'] * np.cos(1. * (phi - parameters['phi41']))) * alpha ** 4 +
            (parameters['C56'] * np.cos(6. * (phi - parameters['phi56'])) +
             parameters['C54'] * np.cos(4. * (phi - parameters['phi54'])) +
             parameters['C52'] * np.cos(2. * (phi - parameters['phi52'])) + parameters['C50']) * alpha ** 5)

    dchi_dphi = -2 * np.pi / wavelength * (
            1 / 2. * (2. * parameters['C12'] * np.sin(2. * (phi - parameters['phi12']))) * alpha +
            1 / 3. * (3. * parameters['C23'] * np.sin(3. * (phi - parameters['phi23'])) +
                      1. * parameters['C21'] * np.sin(1. * (phi - parameters['phi21']))) * alpha ** 2 +
            1 / 4. * (4. * parameters['C34'] * np.sin(4. * (phi - parameters['phi34'])) +
                      2. * parameters['C32'] * np.sin(2. * (phi - parameters['phi32']))) * alpha ** 3 +
            1 / 5. * (5. * parameters['C45'] * np.sin(5. * (phi - parameters['phi45'])) +
                      3. * parameters['C43'] * np.sin(3. * (phi - parameters['phi43'])) +
                      1. * parameters['C41'] * np.sin(1. * (phi - parameters['phi41']))) * alpha ** 4 +
            1 / 6. * (6. * parameters['C56'] * np.sin(6. * (phi - parameters['phi56'])) +
                      4. * parameters['C54'] * np.sin(4. * (phi - parameters['phi54'])) +
                      2. * parameters['C52'] * np.sin(2. * (phi - parameters['phi52']))) * alpha ** 5)

    return np.exp(-np.sign(angular_spread) * (angular_spread / 2) ** 2 * (dchi_dk ** 2 + dchi_dphi ** 2))


class CTF(HasAcceleratorMixin):

    def __init__(self, semiangle_cutoff: float = np.inf, rolloff: float = 0., focal_spread: float = 0.,
                 angular_spread: float = 0., gaussian_spread: float = 0., energy: float = None,
                 parameters: Mapping[str, float] = None, device=None, **kwargs):

        self.changed = Event()
        self.cache = Cache(1)
        self._accelerator = Accelerator(energy=energy)
        self.device_manager = DeviceManager(device)

        self._semiangle_cutoff = DTYPE(semiangle_cutoff)
        self._rolloff = DTYPE(rolloff)
        self._focal_spread = DTYPE(focal_spread)
        self._angular_spread = DTYPE(angular_spread)
        self._gaussian_spread = DTYPE(gaussian_spread)
        self._parameters = dict(zip(polar_symbols, [0.] * len(polar_symbols)))

        if parameters is None:
            parameters = {}

        parameters.update(kwargs)

        self.set_parameters(parameters)

        def parametrization_property(key):
            def getter(self):
                return self._parameters[key]

            def setter(self, value):
                old = getattr(self, key)
                self._parameters[key] = value
                self.notify_observers({'notifier': key, 'change': old != value})

            return property(getter, setter)

        for symbol in polar_symbols:
            setattr(self.__class__, symbol, parametrization_property(symbol))
            kwargs.pop(symbol, None)

        for key, value in polar_aliases.items():
            if key != 'defocus':
                setattr(self.__class__, key, parametrization_property(value))
            kwargs.pop(key, None)

        self.changed.register(cache_clear_callback(self.evaluate_on_grid))

    @property
    def parameters(self):
        return self._parameters

    @property
    def defocus(self) -> float:
        return - self._parameters['C10']

    @defocus.setter
    @watched_property('changed')
    def defocus(self, value: float):
        self._parameters['C10'] = DTYPE(-value)

    @property
    def semiangle_cutoff(self) -> float:
        return self._semiangle_cutoff

    @semiangle_cutoff.setter
    @watched_property('changed')
    def semiangle_cutoff(self, value: float):
        self._semiangle_cutoff = DTYPE(value)

    @property
    def rolloff(self) -> float:
        return self._rolloff

    @rolloff.setter
    @watched_property('changed')
    def rolloff(self, value: float):
        self._rolloff = DTYPE(value)

    @property
    def focal_spread(self) -> float:
        return self._focal_spread

    @focal_spread.setter
    @watched_property('changed')
    def focal_spread(self, value: float):
        self._focal_spread = DTYPE(value)

    @property
    def angular_spread(self) -> float:
        return self._angular_spread

    @angular_spread.setter
    @watched_property('changed')
    def angular_spread(self, value: float):
        self._angular_spread = DTYPE(value)

    @property
    def gaussian_spread(self) -> float:
        return self._gaussian_spread

    @gaussian_spread.setter
    @watched_property('changed')
    def gaussian_spread(self, value: float):
        self._gaussian_spread = DTYPE(value)

    def set_parameters(self, parameters):
        for symbol, value in parameters.items():
            if symbol in self._parameters.keys():
                self._parameters[symbol] = value

            elif symbol == 'defocus':
                self._parameters[polar_aliases[symbol]] = -value

            elif symbol in polar_aliases.keys():
                self._parameters[polar_aliases[symbol]] = value

            else:
                raise ValueError('{} not a recognized parameter'.format(symbol))

        return parameters

    def evaluate_aperture(self, alpha):
        return calculate_aperture(alpha, self.semiangle_cutoff, self.rolloff)

    def evaluate_temporal_envelope(self, alpha):
        return calculate_temporal_envelope(alpha, self.wavelength, self.focal_spread)

    def evaluate_spatial_envelope(self, alpha, phi):
        return calculate_spatial_envelope(alpha, phi, self.wavelength, self.angular_spread, self.parameters)

    def evaluate_gaussian_envelope(self, alpha):
        return calculate_gaussian_envelope(alpha, self.wavelength, self.gaussian_spread)

    def evaluate_aberrations(self, alpha, phi):
        return calculate_polar_aberrations(alpha, phi, self.wavelength, self._parameters)

    def evaluate(self, alpha, phi):
        array = self.evaluate_aberrations(alpha, phi)

        if self.semiangle_cutoff < np.inf:
            array *= self.evaluate_aperture(alpha)

        if self.focal_spread > 0.:
            array *= self.evaluate_temporal_envelope(alpha)

        if self.angular_spread > 0.:
            array *= self.evaluate_spatial_envelope(alpha, phi)

        if self.gaussian_spread > 0.:
            array *= self.evaluate_gaussian_envelope(alpha)

        return array

    @cached_method('cache')
    def evaluate_on_grid(self, grid):
        grid.check_is_defined()
        self.accelerator.check_is_defined()

        xp = self.device_manager.get_array_library()
        kx, ky = grid.spatial_frequencies()
        alpha_x = xp.asarray(kx) * self.wavelength
        alpha_y = xp.asarray(ky) * self.wavelength
        alpha = xp.sqrt(alpha_x.reshape((-1, 1)) ** 2 + alpha_y.reshape((1, -1)) ** 2)
        phi = xp.arctan2(alpha_x.reshape((-1, 1)), alpha_y.reshape((1, -1)))

        return self.evaluate(alpha, phi)

    # def copy(self):
    #     parameters = self._parameters
    #
    #     self.__class__()


def scherzer_defocus(Cs, energy):
    return 1.2 * np.sign(Cs) * np.sqrt(np.abs(Cs) * energy2wavelength(energy))


def polar2cartesian(polar):
    polar = defaultdict(lambda: 0, polar)

    cartesian = {}
    cartesian['C10'] = polar['C10']
    cartesian['C12a'] = - polar['C12'] * np.cos(2 * polar['phi12'])
    cartesian['C12b'] = polar['C12'] * np.sin(2 * polar['phi12'])
    cartesian['C21a'] = polar['C21'] * np.sin(polar['phi21'])
    cartesian['C21b'] = polar['C21'] * np.cos(polar['phi21'])
    cartesian['C23a'] = - polar['C23'] * np.sin(3 * polar['phi23'])
    cartesian['C23b'] = polar['C23'] * np.cos(3 * polar['phi23'])
    cartesian['C30'] = polar['C30']
    cartesian['C32a'] = - polar['C32'] * np.cos(2 * polar['phi32'])
    cartesian['C32b'] = polar['C32'] * np.cos(np.pi / 2 - 2 * polar['phi32'])
    cartesian['C34a'] = polar['C34'] * np.cos(-4 * polar['phi34'])
    K = np.sqrt(3 + np.sqrt(8.))
    cartesian['C34b'] = 1 / 4. * (1 + K ** 2) ** 2 / (K ** 3 - K) * polar['C34'] * np.cos(
        4 * np.arctan(1 / K) - 4 * polar['phi34'])

    return cartesian


def cartesian2polar(cartesian):
    cartesian = defaultdict(lambda: 0, cartesian)

    polar = {}
    polar['C10'] = cartesian['C10']
    polar['C12'] = - np.sqrt(cartesian['C12a'] ** 2 + cartesian['C12b'] ** 2)
    polar['phi12'] = - np.arctan2(cartesian['C12b'], cartesian['C12a']) / 2.
    polar['C21'] = np.sqrt(cartesian['C21a'] ** 2 + cartesian['C21b'] ** 2)
    polar['phi21'] = np.arctan2(cartesian['C21a'], cartesian['C21b'])
    polar['C23'] = np.sqrt(cartesian['C23a'] ** 2 + cartesian['C23b'] ** 2)
    polar['phi23'] = -np.arctan2(cartesian['C23a'], cartesian['C23b']) / 3.
    polar['C30'] = cartesian['C30']
    polar['C32'] = -np.sqrt(cartesian['C32a'] ** 2 + cartesian['C32b'] ** 2)
    polar['phi32'] = -np.arctan2(cartesian['C32b'], cartesian['C32a']) / 2.
    polar['C34'] = np.sqrt(cartesian['C34a'] ** 2 + cartesian['C34b'] ** 2)
    polar['phi34'] = np.arctan2(cartesian['C34b'], cartesian['C34a']) / 4

    return polar
