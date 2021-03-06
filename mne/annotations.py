# Authors: Jaakko Leppakangas <jaeilepp@student.jyu.fi>
#
# License: BSD (3-clause)

from datetime import datetime
import time
import re
from copy import deepcopy

import numpy as np

from .utils import _pl, check_fname, _validate_type, verbose, warn, logger
from .utils import _Counter as Counter
from .externals.six import string_types
from .io.write import (start_block, end_block, write_float, write_name_list,
                       write_double, start_file)
from .io.constants import FIFF
from .io.open import fiff_open
from .io.tree import dir_tree_find
from .io.tag import read_tag


class Annotations(object):
    """Annotation object for annotating segments of raw data.

    Annotations are added to instance of :class:`mne.io.Raw` as an attribute
    named ``annotations``. To reject bad epochs using annotations, use
    annotation description starting with 'bad' keyword. The epochs with
    overlapping bad segments are then rejected automatically by default.

    To remove epochs with blinks you can do::

        >>> eog_events = mne.preprocessing.find_eog_events(raw)  # doctest: +SKIP
        >>> n_blinks = len(eog_events)  # doctest: +SKIP
        >>> onset = eog_events[:, 0] / raw.info['sfreq'] - 0.25  # doctest: +SKIP
        >>> duration = np.repeat(0.5, n_blinks)  # doctest: +SKIP
        >>> description = ['bad blink'] * n_blinks  # doctest: +SKIP
        >>> annotations = mne.Annotations(onset, duration, description)  # doctest: +SKIP
        >>> raw.set_annotations(annotations)  # doctest: +SKIP
        >>> epochs = mne.Epochs(raw, events, event_id, tmin, tmax)  # doctest: +SKIP

    Parameters
    ----------
    onset : array of float, shape (n_annotations,)
        The starting time of annotations in seconds after ``orig_time``.
    duration : array of float, shape (n_annotations,)
        Durations of the annotations in seconds.
    description : array of str, shape (n_annotations,) | str
        Array of strings containing description for each annotation. If a
        string, all the annotations are given the same description. To reject
        epochs, use description starting with keyword 'bad'. See example above.
    orig_time : float | int | instance of datetime | array of int | None
        A POSIX Timestamp, datetime or an array containing the timestamp as the
        first element and microseconds as the second element. Determines the
        starting time of annotation acquisition. If None (default),
        starting time is determined from beginning of raw data acquisition.
        In general, ``raw.info['meas_date']`` (or None) can be used for syncing
        the annotations with raw data if their acquisiton is started at the
        same time.

    Notes
    -----
    If ``orig_time`` is None, the annotations are synced to the start of the
    data (0 seconds). Otherwise the annotations are synced to sample 0 and
    ``raw.first_samp`` is taken into account the same way as with events.

    When setting annotations, the following alignments
    between ``raw.info['meas_date']`` and ``annotation.orig_time`` take place:

    ::

        ----------- meas_date=XX, orig_time=YY -----------------------------

             |              +------------------+
             |______________|     RAW          |
             |              |                  |
             |              +------------------+
         meas_date      first_samp
             .
             .         |         +------+
             .         |_________| ANOT |
             .         |         |      |
             .         |         +------+
             .     orig_time   onset[0]
             .
             |                   +------+
             |___________________|      |
             |                   |      |
             |                   +------+
         orig_time            onset[0]'

        ----------- meas_date=XX, orig_time=None ---------------------------

             |              +------------------+
             |______________|     RAW          |
             |              |                  |
             |              +------------------+
             .              N         +------+
             .              o_________| ANOT |
             .              n         |      |
             .              e         +------+
             .
             |                        +------+
             |________________________|      |
             |                        |      |
             |                        +------+
         orig_time                 onset[0]'

        ----------- meas_date=None, orig_time=YY ---------------------------

             N              +------------------+
             o______________|     RAW          |
             n              |                  |
             e              +------------------+
                       |         +------+
                       |_________| ANOT |
                       |         |      |
                       |         +------+


                    [[[ CRASH ]]]

        ----------- meas_date=None, orig_time=None -------------------------

             N              +------------------+
             o______________|     RAW          |
             n              |                  |
             e              +------------------+
             .              N         +------+
             .              o_________| ANOT |
             .              n         |      |
             .              e         +------+
             .
             N                        +------+
             o________________________|      |
             n                        |      |
             e                        +------+
         orig_time                 onset[0]'

    """  # noqa: E501

    def __init__(self, onset, duration, description,
                 orig_time=None):  # noqa: D102
        if orig_time is not None:
            if isinstance(orig_time, datetime):
                orig_time = float(time.mktime(orig_time.timetuple()))
            elif not np.isscalar(orig_time):
                orig_time = orig_time[0] + orig_time[1] / 1000000.
            else:  # isscalar
                orig_time = float(orig_time)  # np.int not serializable
        self.orig_time = orig_time

        onset = np.array(onset, dtype=float)
        if onset.ndim != 1:
            raise ValueError('Onset must be a one dimensional array, got %s '
                             '(shape %s).'
                             % (onset.ndim, onset.shape))
        duration = np.array(duration, dtype=float)
        if isinstance(description, string_types):
            description = np.repeat(description, len(onset))
        if duration.ndim != 1:
            raise ValueError('Duration must be a one dimensional array.')
        if not (len(onset) == len(duration) == len(description)):
            raise ValueError('Onset, duration and description must be '
                             'equal in sizes.')
        if any([';' in desc for desc in description]):
            raise ValueError('Semicolons in descriptions not supported.')

        self.onset = onset
        self.duration = duration
        self.description = np.array(description, dtype=str)

    def __repr__(self):
        """Show the representation."""
        kinds = sorted(set('%s' % d.split(' ')[0].lower()
                           for d in self.description))
        kinds = ['%s (%s)' % (kind, sum(d.lower().startswith(kind)
                                        for d in self.description))
                 for kind in kinds]
        kinds = ', '.join(kinds[:3]) + ('' if len(kinds) <= 3 else '...')
        kinds = (': ' if len(kinds) > 0 else '') + kinds
        return ('<Annotations  |  %s segment%s %s >'
                % (len(self.onset), _pl(len(self.onset)), kinds))

    def __len__(self):
        """Return the number of annotations."""
        return len(self.duration)

    def __add__(self, other):
        """Add (concatencate) two Annotation objects."""
        out = self.copy()
        out += other
        return out

    def __iadd__(self, other):
        """Add (concatencate) two Annotation objects in-place.

        Both annotations must have the same orig_time
        """
        if len(self) == 0:
            self.orig_time = other.orig_time
        if self.orig_time != other.orig_time:
            raise ValueError("orig_time should be the same to "
                             "add/concatenate 2 annotations "
                             "(got %s != %s)" % (self.orig_time,
                                                 other.orig_time))
        return self.append(other.onset, other.duration, other.description)

    def append(self, onset, duration, description):
        """Add an annotated segment. Operates inplace.

        Parameters
        ----------
        onset : float
            Annotation time onset from the beginning of the recording in
            seconds.
        duration : float
            Duration of the annotation in seconds.
        description : str
            Description for the annotation. To reject epochs, use description
            starting with keyword 'bad'

        Returns
        -------
        self : mne.Annotations
            The modified Annotations object.
        """
        self.onset = np.append(self.onset, onset)
        self.duration = np.append(self.duration, duration)
        self.description = np.append(self.description, description)
        return self

    def copy(self):
        """Return a deep copy of self."""
        return deepcopy(self)

    def delete(self, idx):
        """Remove an annotation. Operates inplace.

        Parameters
        ----------
        idx : int | list of int
            Index of the annotation to remove.
        """
        self.onset = np.delete(self.onset, idx)
        self.duration = np.delete(self.duration, idx)
        self.description = np.delete(self.description, idx)

    def save(self, fname):
        """Save annotations to FIF.

        Typically annotations get saved in the FIF file for raw data
        (e.g., as ``raw.annotations``), but this offers the possibility
        to also save them to disk separately.

        Parameters
        ----------
        fname : str
            The filename to use.
        """
        check_fname(fname, 'annotations', ('-annot.fif', '-annot.fif.gz',
                                           '_annot.fif', '_annot.fif.gz'))
        with start_file(fname) as fid:
            _write_annotations(fid, self)

    def crop(self, tmin=None, tmax=None, emit_warning=False):
        """Remove all annotation that are outside of [tmin, tmax].

        The method operates inplace.

        Parameters
        ----------
        tmin : float | None
            Start time of selection in seconds.
        tmax : float | None
            End time of selection in seconds.
        emit_warning : bool
            Whether to emit warnings when limiting or omitting annotations.
            Defaults to False.

        Returns
        -------
        self : instance of Annotations
            The cropped Annotations object.
        """
        offset = 0 if self.orig_time is None else self.orig_time
        absolute_onset = self.onset + offset
        absolute_offset = absolute_onset + self.duration

        tmin = tmin if tmin is not None else absolute_onset.min()
        tmax = tmax if tmax is not None else absolute_offset.max()

        if tmin > tmax:
            raise ValueError('tmax should be greater than tmin.')

        if tmin < 0:
            raise ValueError('tmin should be positive.')

        out_of_bounds = (absolute_onset > tmax) | (absolute_offset < tmin)

        # clip the left side
        clip_left_elem = (absolute_onset < tmin) & ~out_of_bounds
        self.onset[clip_left_elem] = tmin - offset
        diff = tmin - absolute_onset[clip_left_elem]
        self.duration[clip_left_elem] = self.duration[clip_left_elem] - diff

        # clip the right side
        clip_right_elem = (absolute_offset > tmax) & ~out_of_bounds
        diff = absolute_offset[clip_right_elem] - tmax
        self.duration[clip_right_elem] = self.duration[clip_right_elem] - diff

        # remove out of bounds
        self.onset = self.onset.compress(~out_of_bounds)
        self.duration = self.duration.compress(~out_of_bounds)
        self.description = self.description.compress(~out_of_bounds)

        if emit_warning:
            omitted = out_of_bounds.sum()
            if omitted > 0:
                warn('Omitted %s annotation(s) that were outside data'
                     ' range.' % omitted)
            limited = clip_left_elem.sum() + clip_right_elem.sum()
            if limited > 0:
                warn('Limited %s annotation(s) that were expanding outside the'
                     ' data range.' % limited)

        return self


def _combine_annotations(one, two, one_n_samples, one_first_samp,
                         two_first_samp, sfreq, meas_date):
    """Combine a tuple of annotations."""
    if one is None and two is None:
        return None
    elif two is None:
        return one
    elif one is None:
        one = Annotations([], [], [], None)

    # Compute the shift necessary for alignment:
    # 1. The shift (in time) due to concatenation
    shift = one_n_samples / sfreq
    meas_date = _handle_meas_date(meas_date)
    # 2. Shift by the difference in meas_date and one.orig_time
    if one.orig_time is not None:
        shift += one_first_samp / sfreq
        shift += meas_date - one.orig_time
    # 3. Shift by the difference in meas_date and two.orig_time
    if two.orig_time is not None:
        shift -= two_first_samp / sfreq
        shift -= meas_date - two.orig_time

    onset = np.concatenate([one.onset, two.onset + shift])
    duration = np.concatenate([one.duration, two.duration])
    description = np.concatenate([one.description, two.description])
    return Annotations(onset, duration, description, one.orig_time)


def _handle_meas_date(meas_date):
    """Convert meas_date to seconds."""
    if meas_date is None:
        meas_date = 0
    elif not np.isscalar(meas_date):
        if len(meas_date) > 1:
            meas_date = meas_date[0] + meas_date[1] / 1000000.
        else:
            meas_date = meas_date[0]
    return meas_date


def _sync_onset(raw, onset, inverse=False):
    """Adjust onsets in relation to raw data."""
    meas_date = _handle_meas_date(raw.info['meas_date'])
    if raw.annotations.orig_time is None:
        annot_start = onset
    else:
        offset = -raw._first_time if inverse else raw._first_time
        annot_start = (raw.annotations.orig_time - meas_date) - offset + onset
    return annot_start


def _annotations_starts_stops(raw, kinds, name='unknown', invert=False):
    """Get starts and stops from given kinds.

    onsets and ends are inclusive.
    """
    _validate_type(kinds, (string_types, list, tuple), str(type(kinds)),
                   "str, list or tuple")
    if isinstance(kinds, string_types):
        kinds = [kinds]
    else:
        for kind in kinds:
            _validate_type(kind, 'str', "All entries")

    if len(raw.annotations) == 0:
        onsets, ends = np.array([], int), np.array([], int)
    else:
        idxs = [idx for idx, desc in enumerate(raw.annotations.description)
                if any(desc.upper().startswith(kind.upper())
                       for kind in kinds)]
        onsets = raw.annotations.onset[idxs]
        onsets = _sync_onset(raw, onsets)
        ends = onsets + raw.annotations.duration[idxs]
        order = np.argsort(onsets)
        onsets = raw.time_as_index(onsets[order], use_rounding=True)
        ends = raw.time_as_index(ends[order], use_rounding=True)
    if invert:
        # We invert the relationship (i.e., get segments that do not satisfy)
        if len(onsets) == 0 or onsets[0] != 0:
            onsets = np.concatenate([[0], onsets])
            ends = np.concatenate([[0], ends])
        if len(ends) == 1 or ends[-1] != len(raw.times):
            onsets = np.concatenate([onsets, [len(raw.times)]])
            ends = np.concatenate([ends, [len(raw.times)]])
        onsets, ends = ends[:-1], onsets[1:]
    return onsets, ends


def _write_annotations(fid, annotations):
    """Write annotations."""
    start_block(fid, FIFF.FIFFB_MNE_ANNOTATIONS)
    write_float(fid, FIFF.FIFF_MNE_BASELINE_MIN, annotations.onset)
    write_float(fid, FIFF.FIFF_MNE_BASELINE_MAX,
                annotations.duration + annotations.onset)
    # To allow : in description, they need to be replaced for serialization
    write_name_list(fid, FIFF.FIFF_COMMENT, [d.replace(':', ';') for d in
                                             annotations.description])
    if annotations.orig_time is not None:
        write_double(fid, FIFF.FIFF_MEAS_DATE, annotations.orig_time)
    end_block(fid, FIFF.FIFFB_MNE_ANNOTATIONS)


def read_annotations(fname):
    """Read annotations from a FIF file.

    Parameters
    ----------
    fname : str
        The filename.

    Returns
    -------
    annot : instance of Annotations | None
        The annotations.
    """
    ff, tree, _ = fiff_open(fname, preload=False)
    with ff as fid:
        annotations = _read_annotations(fid, tree)
    if annotations is None:
        raise IOError('No annotation data found in file "%s"' % fname)
    return annotations


def read_brainstorm_annotations(fname, orig_time=None):
    """Read annotations from a Brainstorm events_ file.

    Parameters
    ----------
    fname : str
        The filename
    orig_time : float | int | instance of datetime | array of int | None
        A POSIX Timestamp, datetime or an array containing the timestamp as the
        first element and microseconds as the second element. Determines the
        starting time of annotation acquisition. If None (default),
        starting time is determined from beginning of raw data acquisition.
        In general, ``raw.info['meas_date']`` (or None) can be used for syncing
        the annotations with raw data if their acquisiton is started at the
        same time.

    Returns
    -------
    annot : instance of Annotations | None
        The annotations.
    """
    from scipy import io

    def get_duration_from_times(t):
        return t[1] - t[0] if t.shape[0] == 2 else np.zeros(len(t[0]))

    annot_data = io.loadmat(fname)
    onsets, durations, descriptions = (list(), list(), list())
    for label, _, _, _, times, _, _ in annot_data['events'][0]:
        onsets.append(times[0])
        durations.append(get_duration_from_times(times))
        n_annot = len(times[0])
        descriptions += [str(label[0])] * n_annot

    return Annotations(onset=np.concatenate(onsets),
                       duration=np.concatenate(durations),
                       description=descriptions,
                       orig_time=orig_time)


def _read_annotations(fid, tree):
    """Read annotations."""
    annot_data = dir_tree_find(tree, FIFF.FIFFB_MNE_ANNOTATIONS)
    if len(annot_data) == 0:
        annotations = None
    else:
        annot_data = annot_data[0]
        orig_time = None
        onset, duration, description = list(), list(), list()
        for ent in annot_data['directory']:
            kind = ent.kind
            pos = ent.pos
            tag = read_tag(fid, pos)
            if kind == FIFF.FIFF_MNE_BASELINE_MIN:
                onset = tag.data
                onset = list() if onset is None else onset
            elif kind == FIFF.FIFF_MNE_BASELINE_MAX:
                duration = tag.data
                duration = list() if duration is None else duration - onset
            elif kind == FIFF.FIFF_COMMENT:
                description = tag.data.split(':')
                description = [d.replace(';', ':') for d in
                               description]
            elif kind == FIFF.FIFF_MEAS_DATE:
                orig_time = float(tag.data)
        assert len(onset) == len(duration) == len(description)
        annotations = Annotations(onset, duration, description,
                                  orig_time)
    return annotations


def _ensure_annotation_object(obj):
    """Check that the object is an Annotations instance.

    Raise error otherwise.
    """
    if not isinstance(obj, Annotations):
        raise ValueError('Annotations must be an instance of '
                         'mne.Annotations. Got %s.' % obj)


@verbose
def events_from_annotations(raw, event_id=None, regexp=None, use_rounding=True,
                            verbose=None):
    """Get events and event_id from an Annotations object.

    Parameters
    ----------
    raw : instance of Raw
        The raw data for which Annotations are defined.
    event_id : dict | Callable | None
        Dictionary of string keys and integer values as used in mne.Epochs
        to map annotation descriptions to integer event codes. Only the
        keys present will be mapped and the annotations with other descriptions
        will be ignored. Otherwise, a callable that provides an integer given
        a string or that returns None for an event to ignore.
        If None, all descriptions of annotations are mapped
        and assigned arbitrary unique integer values.
    regexp : str | None
        Regular expression used to filter the annotations whose
        descriptions is a match.
    use_rounding : boolean
        If True, use rounding (instead of truncation) when converting
        times to indices. This can help avoid non-unique indices.
    verbose : bool, str, int, or None
        If not None, override default verbose level (see
        :func:`mne.verbose` and :ref:`Logging documentation <tut_logging>`
        for more). Defaults to self.verbose.

    Returns
    -------
    events : ndarray, shape (n_events, 3)
        The events.
    event_id : dict
        The event_id variable that can be passed to Epochs.
    """
    if len(raw.annotations) == 0:
        return np.empty((0, 3), dtype=int), event_id

    annotations = raw.annotations

    inds = raw.time_as_index(annotations.onset, use_rounding=use_rounding,
                             origin=annotations.orig_time) + raw.first_samp

    # Filter out the annotations that do not match regexp
    regexp_comp = re.compile('.*' if regexp is None else regexp)

    if event_id is None:
        event_id = Counter()

    event_id_ = dict()
    dropped = []
    for desc in annotations.description:
        if desc in event_id_:
            continue

        if regexp_comp.match(desc) is None:
            continue

        if isinstance(event_id, dict):
            if desc in event_id:
                event_id_[desc] = event_id[desc]
            else:
                continue
        else:
            trigger = event_id(desc)
            if trigger is not None:
                event_id_[desc] = trigger
            else:
                dropped.append(desc)

    event_sel = [ii for ii, kk in enumerate(annotations.description)
                 if kk in event_id_]

    if len(event_sel) == 0 and regexp is not None:
        raise ValueError('Could not find any of the events you specified.')

    values = [event_id_[kk] for kk in
              annotations.description[event_sel]]
    previous_value = np.zeros(len(event_sel))
    inds = inds[event_sel]
    events = np.c_[inds, previous_value, values].astype(int)

    logger.info('Used Annotations descriptions: %s' %
                (list(event_id_.keys()),))

    return events, event_id_
