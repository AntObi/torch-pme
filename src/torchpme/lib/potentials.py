import math
from typing import Union

import torch
from torch.special import gammainc, gammaincc, gammaln

from .kspace_filter import KSpaceKernel


# since pytorch has implemented the incomplete Gamma functions, but not the much more
# commonly used (complete) Gamma function, we define it in a custom way to make autograd
# work as in https://discuss.pytorch.org/t/is-there-a-gamma-function-in-pytorch/17122
def gamma(x: torch.Tensor) -> torch.Tensor:
    return torch.exp(gammaln(x))


class BasePotential(torch.nn.Module):
    r"""Base class defining the interface for a pair potential energy function.

    Internal state variables and parameters in derived classes should be defined
    in the ``__init__``  method. Supports computing the potential starting from a
    list of distances or a list of squared distances.
    """

    def __init__(self):
        super().__init__()

    def from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        """Computes a pair potential given a tensor of interatomic distances.

        :param dist: torch.tensor containing the distances at which the potential
            is to be evaluated.
        """

        raise NotImplementedError(
            f"from_dist is not implemented for {self.__class__.__name__}"
        )

    def from_dist_sq(self, dist_sq: torch.Tensor) -> torch.Tensor:
        """Computes a pair potential given a tensor of squared distances.

        :param dist_sq: torch.tensor containing the squared distances at which
            the potential is to be evaluated.
        """

        return self.from_dist(torch.sqrt(dist_sq))


class RangeSeparatedPotential(BasePotential, KSpaceKernel):
    r"""Base class defining the interface for a range-separated potential.

    Internal state variables and parameters in derived classes should be defined
    in the ``__init__``  method. It provides a short-range and long-range
    functions in real space (such that
    :math:`V(r)=V_{\mathrm{SR}}(r)+V_{\mathrm{LR}}(r))` ).

    It also should provide a ``from_k`` or `from_k_sq`` method
    (following the API of :py:class:`KSpaceKernel`) that are used
    to evaluate the long-range part of the potential in the Fourier domain.
    """

    def sr_from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError(
            f"sr_from_dist is not implemented for {self.__class__.__name__}"
        )

    def lr_from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        raise NotImplementedError(
            f"lr_from_dist is not implemented for {self.__class__.__name__}"
        )

    def from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        # if someone wants the full potential...
        return self.sr_from_dist(dist) + self.lr_from_dist(dist)

    def from_dist_sq(self, dist_sq: torch.Tensor) -> torch.Tensor:
        # if someone wants the full potential...
        dist = torch.sqrt(dist_sq)
        return self.sr_from_dist(dist) + self.lr_from_dist(dist)

    # recycles docstrings
    from_dist.__doc__ = BasePotential.__doc__
    from_dist_sq.__doc__ = BasePotential.__doc__


class InversePowerLawPotential(RangeSeparatedPotential):
    """Inverse power-law potentials of the form :math:`1/r^p`.

    Herem :math:`r` is a distance parameter and :math:`p` an exponent.

    It can be used to compute:

    1. the full :math:`1/r^p` potential
    2. its short-range (SR) and long-range (LR) parts, the split being determined by a
       length-scale parameter (called "smearing" in the code)
    3. the Fourier transform of the LR part

    :param exponent: the exponent :math:`p` in :math:`1/r^p` potentials
    :param smearing: float or torch.Tensor containing the parameter often called "sigma"
        in publications, which determines the length-scale at which the short-range and
        long-range parts of the naive :math:`1/r^p` potential are separated. For the
        Coulomb potential (:math:`p=1`), this potential can be interpreted as the
        effective potential generated by a Gaussian charge density, in which case this
        smearing parameter corresponds to the "width" of the Gaussian.
    """

    def __init__(
        self,
        exponent: float,
        smearing: Union[float, torch.Tensor],
    ):
        super().__init__()
        self.exponent = exponent
        self.smearing = smearing

    def from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        """
        Full :math:`1/r^p` potential as a function of :math:`r`.

        :param dist: torch.tensor containing the distances at which the potential is to
            be evaluated.
        """
        return torch.pow(dist, -self.exponent)

    def from_dist_sq(self, dist_sq: torch.Tensor) -> torch.Tensor:
        """Full :math:`1/r^p` potential as a function of :math:`r^2`.

        :param dist_sq: torch.tensor containing the squared distances at which the
            potential is to be evaluated.
        """
        return torch.pow(dist_sq, -self.exponent / 2.0)

    def sr_from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        r"""Short-range (SR) part of the range-separated :math:`1/r^p` potential.

        More explicitly: it corresponds to `:math:`V_\mathrm{SR}(r)` in :math:`1/r^p =
        V_\mathrm{SR}(r) + V_\mathrm{LR}(r)`, where the location of the split is
        determined by the smearing parameter.

        For the Coulomb potential, this would return

        .. code-block:: python

            potential = torch.erfc(dist / torch.sqrt(2.0) / smearing) / dist

        :param dist: torch.tensor containing the distances at which the potential is to
            be evaluated.
        """
        exponent = torch.full([], self.exponent, device=dist.device, dtype=dist.dtype)
        x = 0.5 * dist**2 / self.smearing**2
        peff = exponent / 2
        prefac = 1.0 / (2 * self.smearing**2) ** peff
        potential = prefac * gammaincc(peff, x) / x**peff

        return potential

    def lr_from_dist(self, dist: torch.Tensor) -> torch.Tensor:
        """LR part of the range-separated :math:`1/r^p` potential.

        Used to subtract out the interior contributions after computing the LR part in
        reciprocal (Fourier) space.

        For the Coulomb potential, this would return (note that the only change between
        the SR and LR parts is the fact that erfc changes to erf)

        .. code-block:: python

            potential = erf(dist / sqrt(2) / smearing) / dist

        :param dist: torch.tensor containing the distances at which the potential is to
            be evaluated.
        """
        exponent = torch.full([], self.exponent, device=dist.device, dtype=dist.dtype)
        x = 0.5 * dist**2 / self.smearing**2
        peff = exponent / 2
        prefac = 1.0 / (2 * self.smearing**2) ** peff
        potential = prefac * gammainc(peff, x) / x**peff
        return potential

    def from_k_sq(self, k_sq: torch.Tensor) -> torch.Tensor:
        """Fourier transform of the LR part potential in terms of :math:`k^2`.

        If only the Coulomb potential is needed, the last line can be
        replaced by

        .. code-block:: python

            fourier = 4 * torch.pi * torch.exp(-0.5 * smearing**2 * k_sq) / k_sq

        :param k_sq: torch.tensor containing the squared lengths (2-norms) of the wave
            vectors k at which the Fourier-transformed potential is to be evaluated
        :param smearing: float containing the parameter often called "sigma" in
            publications, which determines the length-scale at which the short-range and
            long-range parts of the naive :math:`1/r^p` potential are separated. For the
            Coulomb potential (:math:`p=1`), this potential can be interpreted as the
            effective potential generated by a Gaussian charge density, in which case
            this smearing parameter corresponds to the "width" of the Gaussian.
        """
        exponent = torch.full([], self.exponent, device=k_sq.device, dtype=k_sq.dtype)
        peff = (3 - exponent) / 2
        prefac = math.pi**1.5 / gamma(exponent / 2) * (2 * self.smearing**2) ** peff
        x = 0.5 * self.smearing**2 * k_sq

        # The k=0 term often needs to be set separately since for exponents p<=3
        # dimension, there is a divergence to +infinity. Setting this value manually
        # to zero physically corresponds to the addition of a uniform background charge
        # to make the system charge-neutral. For p>3, on the other hand, the
        # Fourier-transformed LR potential does not diverge as k->0, and one
        # could instead assign the correct limit. This is not implemented for now
        # for consistency reasons.
        fourier = torch.where(
            k_sq == 0,
            0.0,
            prefac * gammaincc(peff, x) / x**peff * gamma(peff),
        )
        return fourier
