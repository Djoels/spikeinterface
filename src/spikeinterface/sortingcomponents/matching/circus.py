"""Sorting components: template matching."""

import numpy as np
import warnings

import scipy.spatial

import scipy

try:
    import sklearn
    from sklearn.feature_extraction.image import extract_patches_2d, reconstruct_from_patches_2d

    HAVE_SKLEARN = True
except ImportError:
    HAVE_SKLEARN = False


from spikeinterface.core import get_noise_levels, get_random_data_chunks, compute_sparsity
from spikeinterface.sortingcomponents.peak_detection import DetectPeakByChannel

(potrs,) = scipy.linalg.get_lapack_funcs(("potrs",), dtype=np.float32)

(nrm2,) = scipy.linalg.get_blas_funcs(("nrm2",), dtype=np.float32)

spike_dtype = [
    ("sample_index", "int64"),
    ("channel_index", "int64"),
    ("cluster_index", "int64"),
    ("amplitude", "float64"),
    ("segment_index", "int64"),
]

from .main import BaseTemplateMatchingEngine

#################
# Circus peeler #

def compute_overlaps(templates, num_samples, num_channels, sparsities):
    num_templates = len(templates)

    dense_templates = np.zeros((num_templates, num_samples, num_channels), dtype=np.float32)
    for i in range(num_templates):
        dense_templates[i, :, sparsities[i]] = templates[i].T

    size = 2 * num_samples - 1

    all_delays = list(range(0, num_samples + 1))

    overlaps = {}

    for delay in all_delays:
        source = dense_templates[:, :delay, :].reshape(num_templates, -1)
        target = dense_templates[:, num_samples - delay :, :].reshape(num_templates, -1)

        overlaps[delay] = scipy.sparse.csr_matrix(source.dot(target.T))

        if delay < num_samples:
            overlaps[size - delay + 1] = overlaps[delay].T.tocsr()

    new_overlaps = []

    for i in range(num_templates):
        data = [overlaps[j][i, :].T for j in range(size)]
        data = scipy.sparse.hstack(data)
        new_overlaps += [data]

    return new_overlaps


class CircusOMPPeeler(BaseTemplateMatchingEngine):
    """
    Orthogonal Matching Pursuit inspired from Spyking Circus sorter

    https://elifesciences.org/articles/34518

    This is an Orthogonal Template Matching algorithm. For speed and
    memory optimization, templates are automatically sparsified. Signal
    is convolved with the templates, and as long as some scalar products
    are higher than a given threshold, we use a Cholesky decomposition
    to compute the optimal amplitudes needed to reconstruct the signal.

    IMPORTANT NOTE: small chunks are more efficient for such Peeler,
    consider using 100ms chunk

    Parameters
    ----------
    amplitude: tuple
        (Minimal, Maximal) amplitudes allowed for every template
    omp_min_sps: float
        Stopping criteria of the OMP algorithm, in percentage of the norm
    noise_levels: array
        The noise levels, for every channels. If None, they will be automatically
        computed
    random_chunk_kwargs: dict
        Parameters for computing noise levels, if not provided (sub optimal)
    sparse_kwargs: dict
        Parameters to extract a sparsity mask from the waveform_extractor, if not
        already sparse.
    -----
    """

    _default_params = {
        "amplitudes": [0.6, 2],
        "omp_min_sps": 0.1,
        "waveform_extractor": None,
        "random_chunk_kwargs": {},
        "noise_levels": None,
        "rank": 5,
        "sparse_kwargs": {"method": "ptp", "threshold": 1},
        "ignored_ids": [],
        "vicinity": 0,
    }

    @classmethod
    def _prepare_templates(cls, d):
        waveform_extractor = d["waveform_extractor"]
        num_templates = len(d["waveform_extractor"].sorting.unit_ids)

        if not waveform_extractor.is_sparse():
            sparsity = compute_sparsity(waveform_extractor, **d["sparse_kwargs"]).mask
        else:
            sparsity = waveform_extractor.sparsity.mask

        templates = waveform_extractor.get_all_templates(mode="median").copy()

        # First, we set masked channels to 0
        d["sparsities"] = {}
        for count in range(num_templates):
            template = templates[count][:, sparsity[count]]
            (d["sparsities"][count],) = np.nonzero(sparsity[count])
            templates[count][:, ~sparsity[count]] = 0

        # Then we keep only the strongest components
        rank = d["rank"]
        temporal, singular, spatial = np.linalg.svd(templates, full_matrices=False)
        d["temporal"] = temporal[:, :, :rank]
        d["singular"] = singular[:, :rank]
        d["spatial"] = spatial[:, :rank, :]

        # We reconstruct the approximated templates
        templates = np.matmul(d["temporal"] * d["singular"][:, np.newaxis, :], d["spatial"])

        d["temporal"] = np.flip(temporal, axis=1)
        d["templates"] = {}
        d["norms"] = np.zeros(num_templates, dtype=np.float32)

        # And get the norms, saving compressed templates for CC matrix
        for count in range(num_templates):
            template = templates[count][:, sparsity[count]]
            d["norms"][count] = np.linalg.norm(template)
            d["templates"][count] = template / d["norms"][count]

        d["temporal"] /= d["norms"][:, np.newaxis, np.newaxis]
        d["spatial"] = np.moveaxis(d["spatial"][:, :rank, :], [0, 1, 2], [1, 0, 2])
        d["temporal"] = np.moveaxis(d["temporal"][:, :, :rank], [0, 1, 2], [1, 2, 0])
        d["singular"] = d["singular"].T[:, :, np.newaxis]
        return d

    @classmethod
    def initialize_and_check_kwargs(cls, recording, kwargs):
        d = cls._default_params.copy()
        d.update(kwargs)

        # assert isinstance(d['waveform_extractor'], WaveformExtractor)

        for v in ["omp_min_sps"]:
            assert (d[v] >= 0) and (d[v] <= 1), f"{v} should be in [0, 1]"

        d["num_channels"] = d["waveform_extractor"].recording.get_num_channels()
        d["num_samples"] = d["waveform_extractor"].nsamples
        d["nbefore"] = d["waveform_extractor"].nbefore
        d["nafter"] = d["waveform_extractor"].nafter
        d["sampling_frequency"] = d["waveform_extractor"].recording.get_sampling_frequency()
        d["vicinity"] *= d["num_samples"]

        if d["noise_levels"] is None:
            print("CircusOMPPeeler : noise should be computed outside")
            d["noise_levels"] = get_noise_levels(recording, **d["random_chunk_kwargs"], return_scaled=False)

        if "templates" not in d:
            d = cls._prepare_templates(d)
        else:
            for key in ["norms", "sparsities", "temporal", "spatial", "singular"]:
                assert d[key] is not None, "If templates are provided, %d should also be there" % key

        d["num_templates"] = len(d["templates"])

        if "overlaps" not in d:
            d["overlaps"] = compute_overlaps(d["templates"], d["num_samples"], d["num_channels"], d["sparsities"])

        d["ignored_ids"] = np.array(d["ignored_ids"])

        omp_min_sps = d["omp_min_sps"]
        # d["stop_criteria"] = omp_min_sps * np.sqrt(d["noise_levels"].sum() * d["num_samples"])
        d["stop_criteria"] = omp_min_sps * np.maximum(d["norms"], np.sqrt(d["noise_levels"].sum() * d["num_samples"]))

        return d

    @classmethod
    def serialize_method_kwargs(cls, kwargs):
        kwargs = dict(kwargs)
        # remove waveform_extractor
        kwargs.pop("waveform_extractor")
        return kwargs

    @classmethod
    def unserialize_in_worker(cls, kwargs):
        return kwargs

    @classmethod
    def get_margin(cls, recording, kwargs):
        margin = 2 * max(kwargs["nbefore"], kwargs["nafter"])
        return margin

    @classmethod
    def main_function(cls, traces, d):
        templates = d["templates"]
        num_templates = d["num_templates"]
        num_channels = d["num_channels"]
        num_samples = d["num_samples"]
        overlaps = d["overlaps"]
        norms = d["norms"]
        nbefore = d["nbefore"]
        nafter = d["nafter"]
        omp_tol = np.finfo(np.float32).eps
        num_samples = d["nafter"] + d["nbefore"]
        neighbor_window = num_samples - 1
        min_amplitude, max_amplitude = d["amplitudes"]
        ignored_ids = d["ignored_ids"]
        stop_criteria = d["stop_criteria"][:, np.newaxis]
        vicinity = d["vicinity"]
        rank = d["rank"]

        num_timesteps = len(traces)

        num_peaks = num_timesteps - num_samples + 1
        conv_shape = (num_templates, num_peaks)
        scalar_products = np.zeros(conv_shape, dtype=np.float32)

        # Filter using overlap-and-add convolution
        spatially_filtered_data = np.matmul(d["spatial"], traces.T[np.newaxis, :, :])
        scaled_filtered_data = spatially_filtered_data * d["singular"]
        objective_by_rank = scipy.signal.oaconvolve(scaled_filtered_data, d["temporal"], axes=2, mode="valid")
        scalar_products += np.sum(objective_by_rank, axis=0)

        if len(ignored_ids) > 0:
            scalar_products[ignored_ids] = -np.inf

        num_spikes = 0

        spikes = np.empty(scalar_products.size, dtype=spike_dtype)
        idx_lookup = np.arange(scalar_products.size).reshape(num_templates, -1)

        M = np.zeros((100, 100), dtype=np.float32)

        all_selections = np.empty((2, scalar_products.size), dtype=np.int32)
        final_amplitudes = np.zeros(scalar_products.shape, dtype=np.float32)
        num_selection = 0

        full_sps = scalar_products.copy()

        neighbors = {}
        cached_overlaps = {}

        is_valid = scalar_products > stop_criteria
        all_amplitudes = np.zeros(0, dtype=np.float32)
        is_in_vicinity = np.zeros(0, dtype=np.int32)

        while np.any(is_valid):
            best_amplitude_ind = scalar_products[is_valid].argmax()
            best_cluster_ind, peak_index = np.unravel_index(idx_lookup[is_valid][best_amplitude_ind], idx_lookup.shape)

            if num_selection > 0:
                delta_t = selection[1] - peak_index
                idx = np.where((delta_t < neighbor_window) & (delta_t > -num_samples))[0]
                myline = num_samples + delta_t[idx]

                if not best_cluster_ind in cached_overlaps:
                    cached_overlaps[best_cluster_ind] = overlaps[best_cluster_ind].toarray()

                if num_selection == M.shape[0]:
                    Z = np.zeros((2 * num_selection, 2 * num_selection), dtype=np.float32)
                    Z[:num_selection, :num_selection] = M
                    M = Z

                M[num_selection, idx] = cached_overlaps[best_cluster_ind][selection[0, idx], myline]

                if vicinity == 0:
                    scipy.linalg.solve_triangular(
                        M[:num_selection, :num_selection],
                        M[num_selection, :num_selection],
                        trans=0,
                        lower=1,
                        overwrite_b=True,
                        check_finite=False,
                    )

                    v = nrm2(M[num_selection, :num_selection]) ** 2
                    Lkk = 1 - v
                    if Lkk <= omp_tol:  # selected atoms are dependent
                        break
                    M[num_selection, num_selection] = np.sqrt(Lkk)
                else:
                    is_in_vicinity = np.where(np.abs(delta_t) < vicinity)[0]

                    if len(is_in_vicinity) > 0:
                        L = M[is_in_vicinity, :][:, is_in_vicinity]

                        M[num_selection, is_in_vicinity] = scipy.linalg.solve_triangular(
                            L, M[num_selection, is_in_vicinity], trans=0, lower=1, overwrite_b=True, check_finite=False
                        )

                        v = nrm2(M[num_selection, is_in_vicinity]) ** 2
                        Lkk = 1 - v
                        if Lkk <= omp_tol:  # selected atoms are dependent
                            break
                        M[num_selection, num_selection] = np.sqrt(Lkk)
                    else:
                        M[num_selection, num_selection] = 1.0
            else:
                M[0, 0] = 1

            all_selections[:, num_selection] = [best_cluster_ind, peak_index]
            num_selection += 1

            selection = all_selections[:, :num_selection]
            res_sps = full_sps[selection[0], selection[1]]

            if True:  # vicinity == 0:
                all_amplitudes, _ = potrs(M[:num_selection, :num_selection], res_sps, lower=True, overwrite_b=False)
                all_amplitudes /= norms[selection[0]]
            else:
                # This is not working, need to figure out why
                is_in_vicinity = np.append(is_in_vicinity, num_selection - 1)
                all_amplitudes = np.append(all_amplitudes, np.float32(1))
                L = M[is_in_vicinity, :][:, is_in_vicinity]
                all_amplitudes[is_in_vicinity], _ = potrs(L, res_sps[is_in_vicinity], lower=True, overwrite_b=False)
                all_amplitudes[is_in_vicinity] /= norms[selection[0][is_in_vicinity]]

            diff_amplitudes = all_amplitudes - final_amplitudes[selection[0], selection[1]]
            modified = np.where(np.abs(diff_amplitudes) > omp_tol)[0]
            final_amplitudes[selection[0], selection[1]] = all_amplitudes

            for i in modified:
                tmp_best, tmp_peak = selection[:, i]
                diff_amp = diff_amplitudes[i] * norms[tmp_best]

                if not tmp_best in cached_overlaps:
                    cached_overlaps[tmp_best] = overlaps[tmp_best].toarray()

                if not tmp_peak in neighbors.keys():
                    idx = [max(0, tmp_peak - num_samples), min(num_peaks, tmp_peak + neighbor_window)]
                    tdx = [num_samples + idx[0] - tmp_peak, num_samples + idx[1] - tmp_peak]
                    neighbors[tmp_peak] = {"idx": idx, "tdx": tdx}

                idx = neighbors[tmp_peak]["idx"]
                tdx = neighbors[tmp_peak]["tdx"]

                to_add = diff_amp * cached_overlaps[tmp_best][:, tdx[0] : tdx[1]]
                scalar_products[:, idx[0] : idx[1]] -= to_add

            is_valid = scalar_products > stop_criteria

        is_valid = (final_amplitudes > min_amplitude) * (final_amplitudes < max_amplitude)
        valid_indices = np.where(is_valid)

        num_spikes = len(valid_indices[0])
        spikes["sample_index"][:num_spikes] = valid_indices[1] + d["nbefore"]
        spikes["channel_index"][:num_spikes] = 0
        spikes["cluster_index"][:num_spikes] = valid_indices[0]
        spikes["amplitude"][:num_spikes] = final_amplitudes[valid_indices[0], valid_indices[1]]

        spikes = spikes[:num_spikes]
        order = np.argsort(spikes["sample_index"])
        spikes = spikes[order]

        return spikes


class CircusPeeler(BaseTemplateMatchingEngine):

    """
    Greedy Template-matching ported from the Spyking Circus sorter

    https://elifesciences.org/articles/34518

    This is a Greedy Template Matching algorithm. The idea is to detect
    all the peaks (negative, positive or both) above a certain threshold
    Then, at every peak (plus or minus some jitter) we look if the signal
    can be explained with a scaled template.
    The amplitudes allowed, for every templates, are automatically adjusted
    in an optimal manner, to enhance the Matthew Correlation Coefficient
    between all spikes/templates in the waveformextractor. For speed and
    memory optimization, templates are automatically sparsified if the
    density of the matrix falls below a given threshold

    Parameters
    ----------
    peak_sign: str
        Sign of the peak (neg, pos, or both)
    exclude_sweep_ms: float
        The number of samples before/after to classify a peak (should be low)
    jitter: int
        The number of samples considered before/after every peak to search for
        matches
    detect_threshold: int
        The detection threshold
    noise_levels: array
        The noise levels, for every channels
    random_chunk_kwargs: dict
        Parameters for computing noise levels, if not provided (sub optimal)
    max_amplitude: float
        Maximal amplitude allowed for every template
    min_amplitude: float
        Minimal amplitude allowed for every template
    use_sparse_matrix_threshold: float
        If density of the templates is below a given threshold, sparse matrix
        are used (memory efficient)
    sparse_kwargs: dict
        Parameters to extract a sparsity mask from the waveform_extractor, if not
        already sparse.
    -----


    """

    _default_params = {
        "peak_sign": "neg",
        "exclude_sweep_ms": 0.1,
        "jitter_ms": 0.1,
        "detect_threshold": 5,
        "noise_levels": None,
        "random_chunk_kwargs": {},
        "max_amplitude": 1.5,
        "min_amplitude": 0.5,
        "use_sparse_matrix_threshold": 0.25,
        "waveform_extractor": None,
        "sparse_kwargs": {"method": "ptp", "threshold": 1},
    }

    @classmethod
    def _prepare_templates(cls, d):
        waveform_extractor = d["waveform_extractor"]
        num_samples = d["num_samples"]
        num_channels = d["num_channels"]
        num_templates = d["num_templates"]
        use_sparse_matrix_threshold = d["use_sparse_matrix_threshold"]

        d["norms"] = np.zeros(num_templates, dtype=np.float32)

        all_units = list(d["waveform_extractor"].sorting.unit_ids)

        if not waveform_extractor.is_sparse():
            sparsity = compute_sparsity(waveform_extractor, **d["sparse_kwargs"]).mask
        else:
            sparsity = waveform_extractor.sparsity.mask

        templates = waveform_extractor.get_all_templates(mode="median").copy()
        d["sparsities"] = {}
        d["circus_templates"] = {}

        for count, unit_id in enumerate(all_units):
            (d["sparsities"][count],) = np.nonzero(sparsity[count])
            templates[count][:, ~sparsity[count]] = 0
            d["norms"][count] = np.linalg.norm(templates[count])
            templates[count] /= d["norms"][count]
            d["circus_templates"][count] = templates[count][:, sparsity[count]]

        templates = templates.reshape(num_templates, -1)

        nnz = np.sum(templates != 0) / (num_templates * num_samples * num_channels)
        if nnz <= use_sparse_matrix_threshold:
            templates = scipy.sparse.csr_matrix(templates)
            print(f"Templates are automatically sparsified (sparsity level is {nnz})")
            d["is_dense"] = False
        else:
            d["is_dense"] = True

        d["templates"] = templates

        return d

    @classmethod
    def _mcc_error(cls, bounds, good, bad):
        fn = np.sum((good < bounds[0]) | (good > bounds[1]))
        fp = np.sum((bounds[0] <= bad) & (bad <= bounds[1]))
        tp = np.sum((bounds[0] <= good) & (good <= bounds[1]))
        tn = np.sum((bad < bounds[0]) | (bad > bounds[1]))
        denom = (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)
        if denom > 0:
            mcc = 1 - (tp * tn - fp * fn) / np.sqrt(denom)
        else:
            mcc = 1
        return mcc

    @classmethod
    def _cost_function_mcc(cls, bounds, good, bad, delta_amplitude, alpha):
        # We want a minimal error, with the larger bounds that are possible
        cost = alpha * cls._mcc_error(bounds, good, bad) + (1 - alpha) * np.abs(
            (1 - (bounds[1] - bounds[0]) / delta_amplitude)
        )
        return cost

    @classmethod
    def _optimize_amplitudes(cls, noise_snippets, d):
        parameters = d
        waveform_extractor = parameters["waveform_extractor"]
        templates = parameters["templates"]
        num_templates = parameters["num_templates"]
        max_amplitude = parameters["max_amplitude"]
        min_amplitude = parameters["min_amplitude"]
        alpha = 0.5
        norms = parameters["norms"]
        all_units = list(waveform_extractor.sorting.unit_ids)

        parameters["amplitudes"] = np.zeros((num_templates, 2), dtype=np.float32)
        noise = templates.dot(noise_snippets) / norms[:, np.newaxis]

        all_amps = {}
        for count, unit_id in enumerate(all_units):
            waveform = waveform_extractor.get_waveforms(unit_id, force_dense=True)
            snippets = waveform.reshape(waveform.shape[0], -1).T
            amps = templates.dot(snippets) / norms[:, np.newaxis]
            good = amps[count, :].flatten()

            sub_amps = amps[np.concatenate((np.arange(count), np.arange(count + 1, num_templates))), :]
            bad = sub_amps[sub_amps >= good]
            bad = np.concatenate((bad, noise[count]))
            cost_kwargs = [good, bad, max_amplitude - min_amplitude, alpha]
            cost_bounds = [(min_amplitude, 1), (1, max_amplitude)]
            res = scipy.optimize.differential_evolution(cls._cost_function_mcc, bounds=cost_bounds, args=cost_kwargs)
            parameters["amplitudes"][count] = res.x

        return d

    @classmethod
    def initialize_and_check_kwargs(cls, recording, kwargs):
        assert HAVE_SKLEARN, "CircusPeeler needs sklearn to work"
        default_parameters = cls._default_params.copy()
        default_parameters.update(kwargs)

        # assert isinstance(d['waveform_extractor'], WaveformExtractor)
        for v in ["use_sparse_matrix_threshold"]:
            assert (default_parameters[v] >= 0) and (default_parameters[v] <= 1), f"{v} should be in [0, 1]"

        default_parameters["num_channels"] = default_parameters["waveform_extractor"].recording.get_num_channels()
        default_parameters["num_samples"] = default_parameters["waveform_extractor"].nsamples
        default_parameters["num_templates"] = len(default_parameters["waveform_extractor"].sorting.unit_ids)

        if default_parameters["noise_levels"] is None:
            print("CircusPeeler : noise should be computed outside")
            default_parameters["noise_levels"] = get_noise_levels(
                recording, **default_parameters["random_chunk_kwargs"], return_scaled=False
            )

        default_parameters["abs_threholds"] = (
            default_parameters["noise_levels"] * default_parameters["detect_threshold"]
        )

        default_parameters = cls._prepare_templates(default_parameters)

        default_parameters["overlaps"] = compute_overlaps(
            default_parameters["circus_templates"],
            default_parameters["num_samples"],
            default_parameters["num_channels"],
            default_parameters["sparsities"],
        )

        default_parameters["exclude_sweep_size"] = int(
            default_parameters["exclude_sweep_ms"] * recording.get_sampling_frequency() / 1000.0
        )

        default_parameters["nbefore"] = default_parameters["waveform_extractor"].nbefore
        default_parameters["nafter"] = default_parameters["waveform_extractor"].nafter
        default_parameters["patch_sizes"] = (
            default_parameters["waveform_extractor"].nsamples,
            default_parameters["num_channels"],
        )
        default_parameters["sym_patch"] = default_parameters["nbefore"] == default_parameters["nafter"]
        default_parameters["jitter"] = int(
            default_parameters["jitter_ms"] * recording.get_sampling_frequency() / 1000.0
        )

        num_segments = recording.get_num_segments()
        if default_parameters["waveform_extractor"]._params["max_spikes_per_unit"] is None:
            num_snippets = 1000
        else:
            num_snippets = 2 * default_parameters["waveform_extractor"]._params["max_spikes_per_unit"]

        num_chunks = num_snippets // num_segments
        noise_snippets = get_random_data_chunks(
            recording, num_chunks_per_segment=num_chunks, chunk_size=default_parameters["num_samples"], seed=42
        )
        noise_snippets = (
            noise_snippets.reshape(num_chunks, default_parameters["num_samples"], default_parameters["num_channels"])
            .reshape(num_chunks, -1)
            .T
        )
        parameters = cls._optimize_amplitudes(noise_snippets, default_parameters)

        return parameters

    @classmethod
    def serialize_method_kwargs(cls, kwargs):
        kwargs = dict(kwargs)
        # remove waveform_extractor
        kwargs.pop("waveform_extractor")
        return kwargs

    @classmethod
    def unserialize_in_worker(cls, kwargs):
        return kwargs

    @classmethod
    def get_margin(cls, recording, kwargs):
        margin = 2 * max(kwargs["nbefore"], kwargs["nafter"])
        return margin

    @classmethod
    def main_function(cls, traces, d):
        peak_sign = d["peak_sign"]
        abs_threholds = d["abs_threholds"]
        exclude_sweep_size = d["exclude_sweep_size"]
        templates = d["templates"]
        num_templates = d["num_templates"]
        num_channels = d["num_channels"]
        overlaps = d["overlaps"]
        margin = d["margin"]
        norms = d["norms"]
        jitter = d["jitter"]
        patch_sizes = d["patch_sizes"]
        num_samples = d["nafter"] + d["nbefore"]
        neighbor_window = num_samples - 1
        amplitudes = d["amplitudes"]
        sym_patch = d["sym_patch"]

        peak_traces = traces[margin // 2 : -margin // 2, :]
        peak_sample_index, peak_chan_ind = DetectPeakByChannel.detect_peaks(
            peak_traces, peak_sign, abs_threholds, exclude_sweep_size
        )

        if jitter > 0:
            jittered_peaks = peak_sample_index[:, np.newaxis] + np.arange(-jitter, jitter)
            jittered_channels = peak_chan_ind[:, np.newaxis] + np.zeros(2 * jitter)
            mask = (jittered_peaks > 0) & (jittered_peaks < len(peak_traces))
            jittered_peaks = jittered_peaks[mask]
            jittered_channels = jittered_channels[mask]
            peak_sample_index, unique_idx = np.unique(jittered_peaks, return_index=True)
            peak_chan_ind = jittered_channels[unique_idx]
        else:
            peak_sample_index, unique_idx = np.unique(peak_sample_index, return_index=True)
            peak_chan_ind = peak_chan_ind[unique_idx]

        num_peaks = len(peak_sample_index)

        if sym_patch:
            snippets = extract_patches_2d(traces, patch_sizes)[peak_sample_index]
            peak_sample_index += margin // 2
        else:
            peak_sample_index += margin // 2
            snippet_window = np.arange(-d["nbefore"], d["nafter"])
            snippets = traces[peak_sample_index[:, np.newaxis] + snippet_window]

        if num_peaks > 0:
            snippets = snippets.reshape(num_peaks, -1)
            scalar_products = templates.dot(snippets.T)
        else:
            scalar_products = np.zeros((num_templates, 0), dtype=np.float32)

        num_spikes = 0
        spikes = np.empty(scalar_products.size, dtype=spike_dtype)
        idx_lookup = np.arange(scalar_products.size).reshape(num_templates, -1)

        min_sps = (amplitudes[:, 0] * norms)[:, np.newaxis]
        max_sps = (amplitudes[:, 1] * norms)[:, np.newaxis]

        is_valid = (scalar_products > min_sps) & (scalar_products < max_sps)

        cached_overlaps = {}

        while np.any(is_valid):
            best_amplitude_ind = scalar_products[is_valid].argmax()
            best_cluster_ind, peak_index = np.unravel_index(idx_lookup[is_valid][best_amplitude_ind], idx_lookup.shape)

            best_amplitude = scalar_products[best_cluster_ind, peak_index]
            best_peak_sample_index = peak_sample_index[peak_index]
            best_peak_chan_ind = peak_chan_ind[peak_index]

            peak_data = peak_sample_index - peak_sample_index[peak_index]
            is_valid_nn = np.searchsorted(peak_data, [-neighbor_window, neighbor_window + 1])
            idx_neighbor = peak_data[is_valid_nn[0] : is_valid_nn[1]] + neighbor_window

            if not best_cluster_ind in cached_overlaps.keys():
                cached_overlaps[best_cluster_ind] = overlaps[best_cluster_ind].toarray()

            to_add = -best_amplitude * cached_overlaps[best_cluster_ind][:, idx_neighbor]

            scalar_products[:, is_valid_nn[0] : is_valid_nn[1]] += to_add
            scalar_products[best_cluster_ind, is_valid_nn[0] : is_valid_nn[1]] = -np.inf

            spikes["sample_index"][num_spikes] = best_peak_sample_index
            spikes["channel_index"][num_spikes] = best_peak_chan_ind
            spikes["cluster_index"][num_spikes] = best_cluster_ind
            spikes["amplitude"][num_spikes] = best_amplitude
            num_spikes += 1

            is_valid = (scalar_products > min_sps) & (scalar_products < max_sps)

        spikes["amplitude"][:num_spikes] /= norms[spikes["cluster_index"][:num_spikes]]

        spikes = spikes[:num_spikes]
        order = np.argsort(spikes["sample_index"])
        spikes = spikes[order]

        return spikes
