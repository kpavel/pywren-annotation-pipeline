import numpy as np
import pandas as pd
import sys
from annotation_pipeline.utils import logger, get_pixel_indices, get_ibm_cos_client, append_pywren_stats, list_keys,\
    clean_from_cos, read_object_with_retry
from concurrent.futures import ThreadPoolExecutor
import msgpack_numpy as msgpack

ISOTOPIC_PEAK_N = 4
MAX_MZ_VALUE = 10 ** 5


def chunk_spectra(config, input_data, imzml_parser, coordinates):
    cos_client = get_ibm_cos_client(config)
    sp_id_to_idx = get_pixel_indices(coordinates)

    def chunk_size(coords, max_size=512 * 1024 ** 2):
        curr_sp_i = 0
        sp_inds_list, mzs_list, ints_list = [], [], []

        estimated_size_mb = 0
        for x, y in coords:
            mzs_, ints_ = imzml_parser.getspectrum(curr_sp_i)
            mzs_, ints_ = map(np.array, [mzs_, ints_])
            sp_idx = sp_id_to_idx[curr_sp_i]
            sp_inds_list.append(np.ones_like(mzs_) * sp_idx)
            mzs_list.append(mzs_)
            ints_list.append(ints_)
            estimated_size_mb += 2 * mzs_.nbytes + ints_.nbytes
            curr_sp_i += 1
            if estimated_size_mb > max_size:
                yield sp_inds_list, mzs_list, ints_list
                sp_inds_list, mzs_list, ints_list = [], [], []
                estimated_size_mb = 0

        if len(sp_inds_list) > 0:
            yield sp_inds_list, mzs_list, ints_list

    def _upload_chunk(ch_i, sp_mz_int_buf):
        chunk = msgpack.dumps(sp_mz_int_buf)
        key = f'{input_data["ds_chunks"]}/{ch_i}.msgpack'
        size = sys.getsizeof(chunk) * (1 / 1024 ** 2)
        logger.info(f'Uploading spectra chunk {ch_i} - %.2f MB' % size)
        cos_client.put_object(Bucket=config["storage"]["ds_bucket"],
                              Key=key,
                              Body=chunk)
        logger.info(f'Spectra chunk {ch_i} finished')
        return key

    max_size = 512 * 1024 ** 2  # 512MB
    chunk_it = chunk_size(coordinates, max_size)

    futures = []
    with ThreadPoolExecutor() as ex:
        for ch_i, chunk in enumerate(chunk_it):
            sp_inds_list, mzs_list, ints_list = chunk
            dtype = imzml_parser.mzPrecision
            mzs = np.concatenate(mzs_list)
            by_mz = np.argsort(mzs)
            sp_mz_int_buf = np.array([np.concatenate(sp_inds_list)[by_mz],
                                      mzs[by_mz],
                                      np.concatenate(ints_list)[by_mz]], dtype).T

            logger.info(f'Parsed spectra chunk {ch_i}')
            futures.append(ex.submit(_upload_chunk, ch_i, sp_mz_int_buf))

        logger.info(f'Parsed dataset into {len(futures)} chunks')

    logger.info(f'Uploaded {len(futures)} dataset chunks')
    keys = [future.result() for future in futures]
    return keys


def spectra_sample_gen(imzml_parser, sample_ratio=0.05):
    sp_n = len(imzml_parser.coordinates)
    sample_size = int(sp_n * sample_ratio)
    sample_sp_inds = np.random.choice(np.arange(sp_n), sample_size)
    for sp_idx in sample_sp_inds:
        mzs, ints = imzml_parser.getspectrum(sp_idx)
        yield sp_idx, mzs, ints


def define_ds_segments(imzml_parser, ds_segm_size_mb=5, sample_ratio=0.05):
    logger.info('Defining dataset segments bounds')
    spectra_sample = list(spectra_sample_gen(imzml_parser, sample_ratio=sample_ratio))

    spectra_mzs = np.array([mz for sp_id, mzs, ints in spectra_sample for mz in mzs])
    total_n_mz = spectra_mzs.shape[0] / sample_ratio

    float_prec = 4 if imzml_parser.mzPrecision == 'f' else 8
    segm_arr_columns = 3
    segm_n = segm_arr_columns * (total_n_mz * float_prec) // (ds_segm_size_mb * 2 ** 20)
    segm_n = max(1, int(segm_n))

    segm_bounds_q = [i * 1 / segm_n for i in range(0, segm_n + 1)]
    segm_lower_bounds = [np.quantile(spectra_mzs, q) for q in segm_bounds_q]
    ds_segments = np.array(list(zip(segm_lower_bounds[:-1], segm_lower_bounds[1:])))

    logger.info(f'Generated {len(ds_segments)} dataset bounds: {ds_segments[0]}...{ds_segments[-1]}')
    return ds_segments


def segment_spectra(pw, bucket, ds_chunks_prefix, ds_segments_prefix, ds_segments_bounds, ds_segm_size_mb, segm_dtype):
    ds_segm_n = len(ds_segments_bounds)

    # extend boundaries of the first and last segments
    # to include all mzs outside of the spectra sample mz range
    ds_segments_bounds = ds_segments_bounds.copy()
    ds_segments_bounds[0, 0] = 0
    ds_segments_bounds[-1, 1] = MAX_MZ_VALUE

    # define first level segmentation and then segment each one into desired number
    first_level_segm_size_mb = 512
    first_level_segm_n = (len(ds_segments_bounds) * ds_segm_size_mb) // first_level_segm_size_mb
    first_level_segm_n = max(first_level_segm_n, 1)
    ds_segments_bounds = np.array_split(ds_segments_bounds, first_level_segm_n)

    def segment_spectra_chunk(obj, id, ibm_cos):
        print(f'Segmenting spectra chunk {obj.key}')
        sp_mz_int_buf = msgpack.loads(obj.data_stream.read())

        def _first_level_segment_upload(segm_i):
            l = ds_segments_bounds[segm_i][0, 0]
            r = ds_segments_bounds[segm_i][-1, 1]
            segm_start, segm_end = np.searchsorted(sp_mz_int_buf[:, 1], (l, r))  # mz expected to be in column 1
            segm = sp_mz_int_buf[segm_start:segm_end]
            ibm_cos.put_object(Bucket=bucket,
                               Key=f'{ds_segments_prefix}/chunk/{segm_i}/{id}.msgpack',
                               Body=msgpack.dumps(segm))

        with ThreadPoolExecutor(max_workers=128) as pool:
            pool.map(_first_level_segment_upload, range(len(ds_segments_bounds)))

    memory_safe_mb = 1024
    memory_capacity_mb = first_level_segm_size_mb * 2 + memory_safe_mb
    first_futures = pw.map(segment_spectra_chunk, f'{bucket}/{ds_chunks_prefix}/', runtime_memory=memory_capacity_mb)
    pw.get_result(first_futures)
    if not isinstance(first_futures, list): first_futures = [first_futures]
    append_pywren_stats(first_futures, memory=memory_capacity_mb, plus_objects=len(first_futures) * len(ds_segments_bounds))

    def merge_spectra_chunk_segments(segm_i, ibm_cos):
        print(f'Merging segment {segm_i} spectra chunks')

        keys = list_keys(bucket, f'{ds_segments_prefix}/chunk/{segm_i}/', ibm_cos)

        def _merge(key):
            segm_spectra_chunk = read_object_with_retry(ibm_cos, bucket, key, msgpack.load)
            return segm_spectra_chunk

        with ThreadPoolExecutor(max_workers=128) as pool:
            segm = list(pool.map(_merge, keys))

        segm = np.concatenate(segm)

        # Alternative in-place sorting (slower) :
        # segm.view(f'{segm_dtype},{segm_dtype},{segm_dtype}').sort(order=['f1'], axis=0)
        segm = segm[segm[:, 1].argsort()]

        clean_from_cos(None, bucket, f'{ds_segments_prefix}/chunk/{segm_i}/', ibm_cos)
        bounds_list = ds_segments_bounds[segm_i]

        segms_len = []
        for segm_j in range(len(bounds_list)):
            l, r = bounds_list[segm_j]
            segm_start, segm_end = np.searchsorted(segm[:, 1], (l, r))  # mz expected to be in column 1
            sub_segm = segm[segm_start:segm_end]
            segms_len.append(len(sub_segm))
            base_id = sum([len(bounds) for bounds in ds_segments_bounds[:segm_i]])
            id = base_id + segm_j
            print(f'Storing dataset segment {id}')
            ibm_cos.put_object(Bucket=bucket,
                               Key=f'{ds_segments_prefix}/{id}.msgpack',
                               Body=msgpack.dumps(sub_segm))

        return segms_len

    # same memory capacity
    second_futures = pw.map(merge_spectra_chunk_segments, range(len(ds_segments_bounds)), runtime_memory=memory_capacity_mb)
    ds_segms_len = list(np.concatenate(pw.get_result(second_futures)))
    append_pywren_stats(second_futures, memory=memory_capacity_mb, plus_objects=ds_segm_n, minus_objects=len(first_futures) * len(ds_segments_bounds))

    return ds_segm_n, ds_segms_len


def clip_centr_df(pw, bucket, centr_chunks_prefix, clip_centr_chunk_prefix, mz_min, mz_max):
    def clip_centr_df_chunk(obj, id, ibm_cos):
        print(f'Clipping centroids dataframe chunk {obj.key}')
        centroids_df_chunk = pd.read_msgpack(obj.data_stream._raw_stream).sort_values('mz')
        centroids_df_chunk = centroids_df_chunk[centroids_df_chunk.mz > 0]

        ds_mz_range_unique_formulas = centroids_df_chunk[(mz_min < centroids_df_chunk.mz) &
                                                         (centroids_df_chunk.mz < mz_max)].index.unique()
        centr_df_chunk = centroids_df_chunk[centroids_df_chunk.index.isin(ds_mz_range_unique_formulas)].reset_index()
        ibm_cos.put_object(Bucket=bucket,
                           Key=f'{clip_centr_chunk_prefix}/{id}.msgpack',
                           Body=centr_df_chunk.to_msgpack())

        return centr_df_chunk.shape[0]

    memory_capacity_mb = 512
    futures = pw.map(clip_centr_df_chunk, f'{bucket}/{centr_chunks_prefix}/', runtime_memory=memory_capacity_mb)
    centr_n = sum(pw.get_result(futures))
    append_pywren_stats(futures, memory=memory_capacity_mb, plus_objects=len(futures))

    logger.info(f'Prepared {centr_n} centroids')
    return centr_n


def define_centr_segments(pw, bucket, clip_centr_chunk_prefix, centr_n, ds_segm_n, ds_segm_size_mb):
    logger.info('Defining centroids segments bounds')

    def get_first_peak_mz(obj):
        print(f'Extracting first peak mz values from clipped centroids dataframe {obj.key}')
        centr_df = pd.read_msgpack(obj.data_stream._raw_stream)
        first_peak_df = centr_df[centr_df.peak_i == 0]
        return first_peak_df.mz.values

    memory_capacity_mb = 512
    futures = pw.map(get_first_peak_mz, f'{bucket}/{clip_centr_chunk_prefix}/', runtime_memory=memory_capacity_mb)
    first_peak_df_mz = np.concatenate(pw.get_result(futures))
    append_pywren_stats(futures, memory=memory_capacity_mb)

    ds_size_mb = ds_segm_n * ds_segm_size_mb
    data_per_centr_segm_mb = 50
    peaks_per_centr_segm = 1e4
    centr_segm_n = int(max(ds_size_mb // data_per_centr_segm_mb, centr_n // peaks_per_centr_segm, 32))

    segm_bounds_q = [i * 1 / centr_segm_n for i in range(0, centr_segm_n)]
    centr_segm_lower_bounds = np.quantile(first_peak_df_mz, segm_bounds_q)

    logger.info(f'Generated {len(centr_segm_lower_bounds)} centroids bounds: {centr_segm_lower_bounds[0]}...{centr_segm_lower_bounds[-1]}')
    return centr_segm_lower_bounds


def segment_centroids(pw, bucket, clip_centr_chunk_prefix, centr_segm_prefix, centr_segm_lower_bounds):
    centr_segm_n = len(centr_segm_lower_bounds)
    centr_segm_lower_bounds = centr_segm_lower_bounds.copy()

    # define first level segmentation and then segment each one into desired number
    first_level_centr_segm_n = min(32, len(centr_segm_lower_bounds))
    centr_segm_lower_bounds = np.array_split(centr_segm_lower_bounds, first_level_centr_segm_n)
    first_level_centr_segm_bounds = np.array([bounds[0] for bounds in centr_segm_lower_bounds])

    def segment_centr_df(centr_df, db_segm_lower_bounds):
        first_peak_df = centr_df[centr_df.peak_i == 0].copy()
        segment_mapping = np.searchsorted(db_segm_lower_bounds, first_peak_df.mz.values, side='right') - 1
        first_peak_df['segm_i'] = segment_mapping
        centr_segm_df = pd.merge(centr_df, first_peak_df[['formula_i', 'segm_i']], on='formula_i').sort_values('mz')
        return centr_segm_df

    def segment_centr_chunk(obj, id, ibm_cos):
        print(f'Segmenting clipped centroids dataframe chunk {obj.key}')
        centr_df = pd.read_msgpack(obj.data_stream._raw_stream)
        centr_segm_df = segment_centr_df(centr_df, first_level_centr_segm_bounds)

        def _first_level_upload(args):
            segm_i, df = args
            ibm_cos.put_object(Bucket=bucket,
                               Key=f'{centr_segm_prefix}/chunk/{segm_i}/{id}.msgpack',
                               Body=df.to_msgpack())

        with ThreadPoolExecutor(max_workers=128) as pool:
            pool.map(_first_level_upload, [(segm_i, df) for segm_i, df in centr_segm_df.groupby('segm_i')])

    memory_capacity_mb = 512
    first_futures = pw.map(segment_centr_chunk, f'{bucket}/{clip_centr_chunk_prefix}/', runtime_memory=memory_capacity_mb)
    pw.get_result(first_futures)
    append_pywren_stats(first_futures, memory=memory_capacity_mb,
                        plus_objects=len(first_futures) * len(centr_segm_lower_bounds))

    def merge_centr_df_segments(segm_i, ibm_cos):
        print(f'Merging segment {segm_i} clipped centroids chunks')

        keys = list_keys(bucket, f'{centr_segm_prefix}/chunk/{segm_i}/', ibm_cos)

        def _merge(key):
            segm_centr_df_chunk = read_object_with_retry(ibm_cos, bucket, key, pd.read_msgpack)
            return segm_centr_df_chunk

        with ThreadPoolExecutor(max_workers=128) as pool:
            segm = pd.concat(list(pool.map(_merge, keys)))
            del segm['segm_i']

        clean_from_cos(None, bucket, f'{centr_segm_prefix}/chunk/{segm_i}/', ibm_cos)
        centr_segm_df = segment_centr_df(segm, centr_segm_lower_bounds[segm_i])

        def _second_level_upload(args):
            segm_j, df = args
            base_id = sum([len(bounds) for bounds in centr_segm_lower_bounds[:segm_i]])
            id = base_id + segm_j
            print(f'Storing centroids segment {id}')
            ibm_cos.put_object(Bucket=bucket,
                               Key=f'{centr_segm_prefix}/{id}.msgpack',
                               Body=df.to_msgpack())

        with ThreadPoolExecutor(max_workers=128) as pool:
            pool.map(_second_level_upload, [(segm_i, df) for segm_i, df in centr_segm_df.groupby('segm_i')])

    memory_capacity_mb = 1024
    second_futures = pw.map(merge_centr_df_segments, range(len(centr_segm_lower_bounds)), runtime_memory=memory_capacity_mb)
    pw.get_result(second_futures)
    append_pywren_stats(second_futures, memory=memory_capacity_mb,
                        plus_objects=centr_segm_n, minus_objects=len(first_futures) * len(centr_segm_lower_bounds))

    return centr_segm_n
