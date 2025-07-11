# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""This module implements the base CCDData class."""

import itertools

import numpy as np

from astropy import log
from astropy import units as u
from astropy.io import fits, registry
from astropy.utils.decorators import sharedmethod
from astropy.wcs import WCS

from .compat import NDDataArray
from .nduncertainty import (
    InverseVariance,
    NDUncertainty,
    StdDevUncertainty,
    VarianceUncertainty,
)

__all__ = ["CCDData", "fits_ccddata_reader", "fits_ccddata_writer"]

_known_uncertainties = (StdDevUncertainty, VarianceUncertainty, InverseVariance)
_unc_name_to_cls = {cls.__name__: cls for cls in _known_uncertainties}
_unc_cls_to_name = {cls: cls.__name__ for cls in _known_uncertainties}

# Global value which can turn on/off the unit requirements when creating a
# CCDData. Should be used with care because several functions actually break
# if the unit is None!
_config_ccd_requires_unit = True


def _arithmetic(op):
    """Decorator factory which temporarily disables the need for a unit when
    creating a new CCDData instance. The final result must have a unit.

    Parameters
    ----------
    op : function
        The function to apply. Supported are:

        - ``np.add``
        - ``np.subtract``
        - ``np.multiply``
        - ``np.true_divide``

    Notes
    -----
    Should only be used on CCDData ``add``, ``subtract``, ``divide`` or
    ``multiply`` because only these methods from NDArithmeticMixin are
    overwritten.
    """

    def decorator(func):
        def inner(self, operand, operand2=None, **kwargs):
            global _config_ccd_requires_unit
            _config_ccd_requires_unit = False
            result = self._prepare_then_do_arithmetic(op, operand, operand2, **kwargs)
            # Wrap it again as CCDData so it checks the final unit.
            _config_ccd_requires_unit = True
            return result.__class__(result)

        inner.__doc__ = f"See `astropy.nddata.NDArithmeticMixin.{func.__name__}`."
        return sharedmethod(inner)

    return decorator


def _uncertainty_unit_equivalent_to_parent(uncertainty_type, unit, parent_unit):
    if uncertainty_type is StdDevUncertainty:
        return unit == parent_unit
    elif uncertainty_type is VarianceUncertainty:
        return unit == (parent_unit**2)
    elif uncertainty_type is InverseVariance:
        return unit == (1 / (parent_unit**2))
    raise ValueError(f"unsupported uncertainty type: {uncertainty_type}")


class CCDData(NDDataArray):
    """A class describing basic CCD data.

    The CCDData class is based on the NDData object and includes a data array,
    uncertainty frame, mask frame, flag frame, meta data, units, and WCS
    information for a single CCD image.

    Parameters
    ----------
    data : `~astropy.nddata.CCDData`-like or array-like
        The actual data contained in this `~astropy.nddata.CCDData` object.
        Note that the data will always be saved by *reference*, so you should
        make a copy of the ``data`` before passing it in if that's the desired
        behavior.

    uncertainty : `~astropy.nddata.StdDevUncertainty`, \
            `~astropy.nddata.VarianceUncertainty`, \
            `~astropy.nddata.InverseVariance`, `numpy.ndarray` or \
            None, optional
        Uncertainties on the data. If the uncertainty is a `numpy.ndarray`, it
        it assumed to be, and stored as, a `~astropy.nddata.StdDevUncertainty`.
        Default is ``None``.

    mask : `numpy.ndarray` or None, optional
        Mask for the data, given as a boolean Numpy array with a shape
        matching that of the data. The values must be `False` where
        the data is *valid* and `True` when it is not (like Numpy
        masked arrays). If ``data`` is a numpy masked array, providing
        ``mask`` here will causes the mask from the masked array to be
        ignored.
        Default is ``None``.

    flags : `numpy.ndarray` or `~astropy.nddata.FlagCollection` or None, \
            optional
        Flags giving information about each pixel. These can be specified
        either as a Numpy array of any type with a shape matching that of the
        data, or as a `~astropy.nddata.FlagCollection` instance which has a
        shape matching that of the data.
        Default is ``None``.

    wcs : `~astropy.wcs.WCS` or None, optional
        WCS-object containing the world coordinate system for the data.
        Default is ``None``.

    meta : dict-like object or None, optional
        Metadata for this object. "Metadata" here means all information that
        is included with this object but not part of any other attribute
        of this particular object, e.g. creation date, unique identifier,
        simulation parameters, exposure time, telescope name, etc.

    unit : `~astropy.units.Unit` or str, optional
        The units of the data.
        Default is ``None``.

        .. warning::

            If the unit is ``None`` or not otherwise specified it will raise a
            ``ValueError``

    psf : `numpy.ndarray` or None, optional
        Image representation of the PSF at the center of this image. In order
        for convolution to be flux-preserving, this should generally be
        normalized to sum to unity.

    Raises
    ------
    ValueError
        If the ``uncertainty`` or ``mask`` inputs cannot be broadcast (e.g.,
        match shape) onto ``data``.

    Methods
    -------
    read(\\*args, \\**kwargs)
        ``Classmethod`` to create an CCDData instance based on a ``FITS`` file.
        This method uses :func:`fits_ccddata_reader` with the provided
        parameters.
    write(\\*args, \\**kwargs)
        Writes the contents of the CCDData instance into a new ``FITS`` file.
        This method uses :func:`fits_ccddata_writer` with the provided
        parameters.

    Attributes
    ----------
    known_invalid_fits_unit_strings
        A dictionary that maps commonly-used fits unit name strings that are
        technically invalid to the correct valid unit type (or unit string).
        This is primarily for variant names like "ELECTRONS/S" which are not
        formally valid, but are unambiguous and frequently enough encountered
        that it is convenient to map them to the correct unit.

    Notes
    -----
    `~astropy.nddata.CCDData` objects can be easily converted to a regular
     Numpy array using `numpy.asarray`.

    For example::

        >>> from astropy.nddata import CCDData
        >>> import numpy as np
        >>> x = CCDData([1,2,3], unit='adu')
        >>> np.asarray(x)
        array([1, 2, 3])

    This is useful, for example, when plotting a 2D image using
    matplotlib.

        >>> from astropy.nddata import CCDData
        >>> from matplotlib import pyplot as plt   # doctest: +SKIP
        >>> x = CCDData([[1,2,3], [4,5,6]], unit='adu')
        >>> plt.imshow(x)   # doctest: +SKIP

    """

    def __init__(self, *args, **kwd):
        if "meta" not in kwd:
            kwd["meta"] = kwd.pop("header", None)
        if "header" in kwd:
            raise ValueError("can't have both header and meta.")

        super().__init__(*args, **kwd)
        if self._wcs is not None:
            llwcs = self._wcs.low_level_wcs
            if not isinstance(llwcs, WCS):
                raise TypeError("the wcs must be a WCS instance.")
            self._wcs = llwcs

        # Check if a unit is set. This can be temporarily disabled by the
        # _CCDDataUnit contextmanager.
        if _config_ccd_requires_unit and self.unit is None:
            raise ValueError("a unit for CCDData must be specified.")

    def _slice_wcs(self, item):
        """
        Override the WCS slicing behaviour so that the wcs attribute continues
        to be an `astropy.wcs.WCS`.
        """
        if self.wcs is None:
            return None

        try:
            return self.wcs[item]
        except Exception as err:
            self._handle_wcs_slicing_error(err, item)

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):
        self._data = value

    @property
    def wcs(self):
        return self._wcs

    @wcs.setter
    def wcs(self, value):
        if value is not None and not isinstance(value, WCS):
            raise TypeError("the wcs must be a WCS instance.")
        self._wcs = value

    @property
    def unit(self):
        return self._unit

    @unit.setter
    def unit(self, value):
        self._unit = u.Unit(value)

    @property
    def psf(self):
        return self._psf

    @psf.setter
    def psf(self, value):
        if value is not None and not isinstance(value, np.ndarray):
            raise TypeError("The psf must be a numpy array.")
        self._psf = value

    @property
    def header(self):
        return self._meta

    @header.setter
    def header(self, value):
        self.meta = value

    @property
    def uncertainty(self):
        return self._uncertainty

    @uncertainty.setter
    def uncertainty(self, value):
        if value is not None:
            if isinstance(value, NDUncertainty):
                if getattr(value, "_parent_nddata", None) is not None:
                    value = value.__class__(value, copy=False)
                self._uncertainty = value
            elif isinstance(value, np.ndarray):
                if value.shape != self.shape:
                    raise ValueError("uncertainty must have same shape as data.")
                self._uncertainty = StdDevUncertainty(value)
                log.info(
                    "array provided for uncertainty; assuming it is a "
                    "StdDevUncertainty."
                )
            else:
                raise TypeError(
                    "uncertainty must be an instance of a "
                    "NDUncertainty object or a numpy array."
                )
            self._uncertainty.parent_nddata = self
        else:
            self._uncertainty = value

    def to_hdu(
        self,
        hdu_mask="MASK",
        hdu_uncertainty="UNCERT",
        hdu_flags=None,
        wcs_relax=True,
        key_uncertainty_type="UTYPE",
        as_image_hdu=False,
        hdu_psf="PSFIMAGE",
    ):
        """Creates an HDUList object from a CCDData object.

        Parameters
        ----------
        hdu_mask, hdu_uncertainty, hdu_flags, hdu_psf : str or None, optional
            If it is a string append this attribute to the HDUList as
            `~astropy.io.fits.ImageHDU` with the string as extension name.
            Flags are not supported at this time. If ``None`` this attribute
            is not appended.
            Default is ``'MASK'`` for mask, ``'UNCERT'`` for uncertainty,
            ``'PSFIMAGE'`` for psf, and `None` for flags.

        wcs_relax : bool
            Value of the ``relax`` parameter to use in converting the WCS to a
            FITS header using `~astropy.wcs.WCS.to_header`. The common
            ``CTYPE`` ``RA---TAN-SIP`` and ``DEC--TAN-SIP`` requires
            ``relax=True`` for the ``-SIP`` part of the ``CTYPE`` to be
            preserved.

        key_uncertainty_type : str, optional
            The header key name for the class name of the uncertainty (if any)
            that is used to store the uncertainty type in the uncertainty hdu.
            Default is ``UTYPE``.

            .. versionadded:: 3.1

        as_image_hdu : bool
            If this option is `True`, the first item of the returned
            `~astropy.io.fits.HDUList` is a `~astropy.io.fits.ImageHDU`, instead
            of the default `~astropy.io.fits.PrimaryHDU`.

        Raises
        ------
        ValueError
            - If ``self.mask`` is set but not a `numpy.ndarray`.
            - If ``self.uncertainty`` is set but not a astropy uncertainty type.
            - If ``self.uncertainty`` is set but has another unit then
              ``self.data``.

        NotImplementedError
            Saving flags is not supported.

        Returns
        -------
        hdulist : `~astropy.io.fits.HDUList`
        """
        if isinstance(self.header, fits.Header):
            # Copy here so that we can modify the HDU header by adding WCS
            # information without changing the header of the CCDData object.
            header = self.header.copy()
        else:
            # Because _insert_in_metadata_fits_safe is written as a method
            # we need to create a dummy CCDData instance to hold the FITS
            # header we are constructing. This probably indicates that
            # _insert_in_metadata_fits_safe should be rewritten in a more
            # sensible way...
            dummy_ccd = CCDData([1], meta=fits.Header(), unit="adu")
            for k, v in self.header.items():
                dummy_ccd._insert_in_metadata_fits_safe(k, v)
            header = dummy_ccd.header
        if self.unit is not u.dimensionless_unscaled:
            header["bunit"] = self.unit.to_string()
        if self.wcs:
            # Simply extending the FITS header with the WCS can lead to
            # duplicates of the WCS keywords; iterating over the WCS
            # header should be safer.
            #
            # Turns out if I had read the io.fits.Header.extend docs more
            # carefully, I would have realized that the keywords exist to
            # avoid duplicates and preserve, as much as possible, the
            # structure of the commentary cards.
            #
            # Note that until astropy/astropy#3967 is closed, the extend
            # will fail if there are comment cards in the WCS header but
            # not header.
            wcs_header = self.wcs.to_header(relax=wcs_relax)
            header.extend(wcs_header, useblanks=False, update=True)

        if as_image_hdu:
            hdus = [fits.ImageHDU(self.data, header)]
        else:
            hdus = [fits.PrimaryHDU(self.data, header)]

        if hdu_mask and self.mask is not None:
            # Always assuming that the mask is a np.ndarray (check that it has
            # a 'shape').
            if not hasattr(self.mask, "shape"):
                raise ValueError("only a numpy.ndarray mask can be saved.")

            # Convert boolean mask to uint since io.fits cannot handle bool.
            hduMask = fits.ImageHDU(self.mask.astype(np.uint8), name=hdu_mask)
            hdus.append(hduMask)

        if hdu_uncertainty and self.uncertainty is not None:
            # We need to save some kind of information which uncertainty was
            # used so that loading the HDUList can infer the uncertainty type.
            # No idea how this can be done so only allow StdDevUncertainty.
            uncertainty_cls = self.uncertainty.__class__
            if uncertainty_cls not in _known_uncertainties:
                raise ValueError(
                    f"only uncertainties of type {_known_uncertainties} can be saved."
                )
            uncertainty_name = _unc_cls_to_name[uncertainty_cls]

            hdr_uncertainty = fits.Header()
            hdr_uncertainty[key_uncertainty_type] = uncertainty_name

            # Assuming uncertainty is an StdDevUncertainty save just the array
            # this might be problematic if the Uncertainty has a unit differing
            # from the data so abort for different units. This is important for
            # astropy > 1.2
            if hasattr(self.uncertainty, "unit") and self.uncertainty.unit is not None:
                if not _uncertainty_unit_equivalent_to_parent(
                    uncertainty_cls, self.uncertainty.unit, self.unit
                ):
                    raise ValueError(
                        "saving uncertainties with a unit that is not "
                        "equivalent to the unit from the data unit is not "
                        "supported."
                    )

            hduUncert = fits.ImageHDU(
                self.uncertainty.array, hdr_uncertainty, name=hdu_uncertainty
            )
            hdus.append(hduUncert)

        if hdu_flags and self.flags:
            raise NotImplementedError(
                "adding the flags to a HDU is not supported at this time."
            )

        if hdu_psf and self.psf is not None:
            # The PSF is an image, so write it as a separate ImageHDU.
            hdu_psf = fits.ImageHDU(self.psf, name=hdu_psf)
            hdus.append(hdu_psf)

        hdulist = fits.HDUList(hdus)

        return hdulist

    def copy(self):
        """
        Return a copy of the CCDData object.
        """
        return self.__class__(self, copy=True)

    add = _arithmetic(np.add)(NDDataArray.add)
    subtract = _arithmetic(np.subtract)(NDDataArray.subtract)
    multiply = _arithmetic(np.multiply)(NDDataArray.multiply)
    divide = _arithmetic(np.true_divide)(NDDataArray.divide)

    def _insert_in_metadata_fits_safe(self, key, value):
        """
        Insert key/value pair into metadata in a way that FITS can serialize.

        Parameters
        ----------
        key : str
            Key to be inserted in dictionary.

        value : str or None
            Value to be inserted.

        Notes
        -----
        This addresses a shortcoming of the FITS standard. There are length
        restrictions on both the ``key`` (8 characters) and ``value`` (72
        characters) in the FITS standard. There is a convention for handling
        long keywords and a convention for handling long values, but the
        two conventions cannot be used at the same time.

        This addresses that case by checking the length of the ``key`` and
        ``value`` and, if necessary, shortening the key.
        """
        if len(key) > 8 and len(value) > 72:
            short_name = key[:8]
            self.meta[f"HIERARCH {key.upper()}"] = (
                short_name,
                f"Shortened name for {key}",
            )
            self.meta[short_name] = value
        else:
            self.meta[key] = value

    # A dictionary mapping "known" invalid fits unit
    known_invalid_fits_unit_strings = {
        "ELECTRONS/S": u.electron / u.s,
        "ELECTRONS": u.electron,
        "electrons": u.electron,
    }


# These need to be importable by the tests...
_KEEP_THESE_KEYWORDS_IN_HEADER = ["JD-OBS", "MJD-OBS", "DATE-OBS"]
_PCs = {"PC1_1", "PC1_2", "PC2_1", "PC2_2"}
_CDs = {"CD1_1", "CD1_2", "CD2_1", "CD2_2"}


def _generate_wcs_and_update_header(hdr):
    """
    Generate a WCS object from a header and remove the WCS-specific
    keywords from the header.

    Parameters
    ----------
    hdr : astropy.io.fits.header or other dict-like

    Returns
    -------
    new_header, wcs
    """
    # Try constructing a WCS object.
    try:
        wcs = WCS(hdr)
    except Exception as exc:
        # Normally WCS only raises Warnings and doesn't fail but in rare
        # cases (malformed header) it could fail...
        log.info(
            "An exception happened while extracting WCS information from "
            f"the Header.\n{type(exc).__name__}: {exc!s}"
        )
        return hdr, None
    # Test for success by checking to see if the wcs ctype has a non-empty
    # value, return None for wcs if ctype is empty.
    if not wcs.wcs.ctype[0]:
        return (hdr, None)

    new_hdr = hdr.copy()
    # If the keywords below are in the header they are also added to WCS.
    # It seems like they should *not* be removed from the header, though.

    wcs_header = wcs.to_header(relax=True)
    for k in wcs_header:
        if k not in _KEEP_THESE_KEYWORDS_IN_HEADER:
            new_hdr.remove(k, ignore_missing=True)

    # Check that this does not result in an inconsistent header WCS if the WCS
    # is converted back to a header.

    if (_PCs & set(wcs_header)) and (_CDs & set(new_hdr)):
        # The PCi_j representation is used by the astropy.wcs object,
        # so CDi_j keywords were not removed from new_hdr. Remove them now.
        for cd in _CDs:
            new_hdr.remove(cd, ignore_missing=True)

    # The other case -- CD in the header produced by astropy.wcs -- should
    # never happen based on [1], which computes the matrix in PC form.
    # [1]: https://github.com/astropy/astropy/blob/1cf277926d3598dd672dd528504767c37531e8c9/cextern/wcslib/C/wcshdr.c#L596
    #
    # The test test_ccddata.test_wcs_keyword_removal_for_wcs_test_files() does
    # check for the possibility that both PC and CD are present in the result
    # so if the implementation of to_header changes in wcslib in the future
    # then the tests should catch it, and then this code will need to be
    # updated.

    # We need to check for any SIP coefficients that got left behind if the
    # header has SIP.
    if wcs.sip is not None:
        keyword = "{}_{}_{}"
        polynomials = ["A", "B", "AP", "BP"]
        for poly in polynomials:
            order = wcs.sip.__getattribute__(f"{poly.lower()}_order")
            for i, j in itertools.product(range(order), repeat=2):
                new_hdr.remove(keyword.format(poly, i, j), ignore_missing=True)

    return (new_hdr, wcs)


def fits_ccddata_reader(
    filename,
    hdu=0,
    unit=None,
    hdu_uncertainty="UNCERT",
    hdu_mask="MASK",
    hdu_flags=None,
    key_uncertainty_type="UTYPE",
    hdu_psf="PSFIMAGE",
    **kwd,
):
    """
    Generate a CCDData object from a FITS file.

    Parameters
    ----------
    filename : str
        Name of fits file.

    hdu : int, str, tuple of (str, int), optional
        Index or other identifier of the Header Data Unit of the FITS
        file from which CCDData should be initialized. If zero and
        no data in the primary HDU, it will search for the first
        extension HDU with data. The header will be added to the primary HDU.
        Default is ``0``.

    unit : `~astropy.units.Unit`, optional
        Units of the image data. If this argument is provided and there is a
        unit for the image in the FITS header (the keyword ``BUNIT`` is used
        as the unit, if present), this argument is used for the unit.
        Default is ``None``.

    hdu_uncertainty : str or None, optional
        FITS extension from which the uncertainty should be initialized. If the
        extension does not exist the uncertainty of the CCDData is ``None``.
        Default is ``'UNCERT'``.

    hdu_mask : str or None, optional
        FITS extension from which the mask should be initialized. If the
        extension does not exist the mask of the CCDData is ``None``.
        Default is ``'MASK'``.

    hdu_flags : str or None, optional
        Currently not implemented.
        Default is ``None``.

    key_uncertainty_type : str, optional
        The header key name where the class name of the uncertainty  is stored
        in the hdu of the uncertainty (if any).
        Default is ``UTYPE``.

        .. versionadded:: 3.1

    hdu_psf : str or None, optional
        FITS extension from which the psf image should be initialized. If the
        extension does not exist the psf of the CCDData is `None`.

    kwd :
        Any additional keyword parameters are passed through to the FITS reader
        in :mod:`astropy.io.fits`; see Notes for additional discussion.

    Notes
    -----
    FITS files that contained scaled data (e.g. unsigned integer images) will
    be scaled and the keywords used to manage scaled data in
    :mod:`astropy.io.fits` are disabled.
    """
    unsupport_open_keywords = {
        "do_not_scale_image_data": "Image data must be scaled.",
        "scale_back": "Scale information is not preserved.",
    }
    for key, msg in unsupport_open_keywords.items():
        if key in kwd:
            prefix = f"unsupported keyword: {key}."
            raise TypeError(f"{prefix} {msg}")
    with fits.open(filename, **kwd) as hdus:
        hdr = hdus[hdu].header

        if hdu_uncertainty is not None and hdu_uncertainty in hdus:
            unc_hdu = hdus[hdu_uncertainty]
            stored_unc_name = unc_hdu.header.get(key_uncertainty_type, "None")
            # For compatibility reasons the default is standard deviation
            # uncertainty because files could have been created before the
            # uncertainty type was stored in the header.
            unc_type = _unc_name_to_cls.get(stored_unc_name, StdDevUncertainty)
            uncertainty = unc_type(unc_hdu.data)
        else:
            uncertainty = None

        if hdu_mask is not None and hdu_mask in hdus:
            # Mask is saved as uint but we want it to be boolean.
            mask = hdus[hdu_mask].data.astype(np.bool_)
        else:
            mask = None

        if hdu_flags is not None and hdu_flags in hdus:
            raise NotImplementedError("loading flags is currently not supported.")

        if hdu_psf is not None and hdu_psf in hdus:
            psf = hdus[hdu_psf].data
        else:
            psf = None

        # search for the first instance with data if
        # the primary header is empty.
        if hdu == 0 and hdus[hdu].data is None:
            for i in range(len(hdus)):
                if (
                    hdus.info(hdu)[i][3] == "ImageHDU"
                    and hdus.fileinfo(i)["datSpan"] > 0
                ):
                    hdu = i
                    comb_hdr = hdus[hdu].header.copy()
                    # Add header values from the primary header that aren't
                    # present in the extension header.
                    comb_hdr.extend(hdr, unique=True)
                    hdr = comb_hdr
                    log.info(f"first HDU with data is extension {hdu}.")
                    break

        if "bunit" in hdr:
            fits_unit_string = hdr["bunit"]
            # patch to handle FITS files using ADU for the unit instead of the
            # standard version of 'adu'
            if fits_unit_string.strip().lower() == "adu":
                fits_unit_string = fits_unit_string.lower()
        else:
            fits_unit_string = None

        if fits_unit_string:
            if unit is None:
                # Convert the BUNIT header keyword to a unit and if that's not
                # possible raise a meaningful error message.
                try:
                    kifus = CCDData.known_invalid_fits_unit_strings
                    if fits_unit_string in kifus:
                        fits_unit_string = kifus[fits_unit_string]
                    fits_unit_string = u.Unit(fits_unit_string)
                except ValueError:
                    raise ValueError(
                        f"The Header value for the key BUNIT ({fits_unit_string}) "
                        "cannot be interpreted as valid unit. To successfully read the "
                        "file as CCDData you can pass in a valid `unit` "
                        "argument explicitly or change the header of the FITS "
                        "file before reading it."
                    )
            else:
                log.info(
                    f"using the unit {unit} passed to the FITS reader instead "
                    f"of the unit {fits_unit_string} in the FITS file."
                )

        use_unit = unit or fits_unit_string
        hdr, wcs = _generate_wcs_and_update_header(hdr)
        ccd_data = CCDData(
            hdus[hdu].data,
            meta=hdr,
            unit=use_unit,
            mask=mask,
            uncertainty=uncertainty,
            wcs=wcs,
            psf=psf,
        )

    return ccd_data


def fits_ccddata_writer(
    ccd_data,
    filename,
    hdu_mask="MASK",
    hdu_uncertainty="UNCERT",
    hdu_flags=None,
    key_uncertainty_type="UTYPE",
    as_image_hdu=False,
    hdu_psf="PSFIMAGE",
    **kwd,
):
    """
    Write CCDData object to FITS file.

    Parameters
    ----------
    ccd_data : CCDData
        Object to write.

    filename : str
        Name of file.

    hdu_mask, hdu_uncertainty, hdu_flags, hdu_psf : str or None, optional
        If it is a string append this attribute to the HDUList as
        `~astropy.io.fits.ImageHDU` with the string as extension name.
        Flags are not supported at this time. If ``None`` this attribute
        is not appended.
        Default is ``'MASK'`` for mask, ``'UNCERT'`` for uncertainty,
        ``'PSFIMAGE'`` for psf, and `None` for flags.

    key_uncertainty_type : str, optional
        The header key name for the class name of the uncertainty (if any)
        that is used to store the uncertainty type in the uncertainty hdu.
        Default is ``UTYPE``.

        .. versionadded:: 3.1

    as_image_hdu : bool
        If this option is `True`, the first item of the returned
        `~astropy.io.fits.HDUList` is a `~astropy.io.fits.ImageHDU`, instead of
        the default `~astropy.io.fits.PrimaryHDU`.

    kwd :
        All additional keywords are passed to :py:mod:`astropy.io.fits`

    Raises
    ------
    ValueError
        - If ``self.mask`` is set but not a `numpy.ndarray`.
        - If ``self.uncertainty`` is set but not a
          `~astropy.nddata.StdDevUncertainty`.
        - If ``self.uncertainty`` is set but has another unit then
          ``self.data``.

    NotImplementedError
        Saving flags is not supported.
    """
    hdu = ccd_data.to_hdu(
        hdu_mask=hdu_mask,
        hdu_uncertainty=hdu_uncertainty,
        key_uncertainty_type=key_uncertainty_type,
        hdu_flags=hdu_flags,
        as_image_hdu=as_image_hdu,
        hdu_psf=hdu_psf,
    )
    if as_image_hdu:
        hdu.insert(0, fits.PrimaryHDU())
    hdu.writeto(filename, **kwd)


with registry.delay_doc_updates(CCDData):
    registry.register_reader("fits", CCDData, fits_ccddata_reader)
    registry.register_writer("fits", CCDData, fits_ccddata_writer)
    registry.register_identifier("fits", CCDData, fits.connect.is_fits)
