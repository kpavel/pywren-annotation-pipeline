from io import BytesIO
from itertools import repeat
from ibm_botocore.client import ClientError
from concurrent.futures import ThreadPoolExecutor
import pywren_ibm_cloud as pywren
import pandas as pd
import pickle

from annotation_pipeline.formula_parser import safe_generate_ion_formula
from annotation_pipeline.utils import get_ibm_cos_client, append_pywren_stats, clean_from_cos


DECOY_ADDUCTS = ['+He', '+Li', '+Be', '+B', '+C', '+N', '+O', '+F', '+Ne', '+Mg', '+Al', '+Si', '+P', '+S', '+Cl', '+Ar', '+Ca', '+Sc', '+Ti', '+V', '+Cr', '+Mn', '+Fe', '+Co', '+Ni', '+Cu', '+Zn', '+Ga', '+Ge', '+As', '+Se', '+Br', '+Kr', '+Rb', '+Sr', '+Y', '+Zr', '+Nb', '+Mo', '+Ru', '+Rh', '+Pd', '+Ag', '+Cd', '+In', '+Sn', '+Sb', '+Te', '+I', '+Xe', '+Cs', '+Ba', '+La', '+Ce', '+Pr', '+Nd', '+Sm', '+Eu', '+Gd', '+Tb', '+Dy', '+Ho', '+Ir', '+Th', '+Pt', '+Os', '+Yb', '+Lu', '+Bi', '+Pb', '+Re', '+Tl', '+Tm', '+U', '+W', '+Au', '+Er', '+Hf', '+Hg', '+Ta']


def calculate_centroids(config, input_db, polarity='+', isocalc_sigma=0.001238):
    bucket = config["storage"]["db_bucket"]
    formulas_chunks_prefix = input_db["formulas_chunks"]
    centroids_chunks_prefix = input_db["centroids_chunks"]
    clean_from_cos(config, bucket, centroids_chunks_prefix)

    def calculate_peaks_for_formula(formula_i, formula):
        mzs, ints = isocalc_wrapper.centroids(formula)
        if mzs is not None:
            return list(zip(repeat(formula_i), range(len(mzs)), mzs, ints))
        else:
            return []

    def calculate_peaks_for_chunk(bucket, key, data_stream, ibm_cos):
        chunk_df = pd.read_msgpack(data_stream._raw_stream)
        peaks = [peak for formula_i, formula in chunk_df.formula.items()
                 for peak in calculate_peaks_for_formula(formula_i, formula)]
        peaks_df = pd.DataFrame(peaks, columns=['formula_i', 'peak_i', 'mz', 'int'])
        peaks_df.set_index('formula_i', inplace=True)

        chunk_i = '/'.join(key.split('/')[-2:]).split('.')[0]
        centroids_chunk_key = f'{centroids_chunks_prefix}/{chunk_i}.msgpack'
        ibm_cos.put_object(Bucket=bucket, Key=centroids_chunk_key, Body=peaks_df.to_msgpack())

        return peaks_df.shape[0]

    from annotation_pipeline.isocalc_wrapper import IsocalcWrapper # Import lazily so that the rest of the pipeline still works if the dependency is missing
    isocalc_wrapper = IsocalcWrapper({
        # These instrument settings are usually customized on a per-dataset basis out of a set of
        # 18 possible combinations, but most of EMBL's datasets are compatible with the following settings:
        'charge': {
            'polarity': polarity,
            'n_charges': 1,
        },
        'isocalc_sigma': float(f"{isocalc_sigma:f}") # Rounding to match production implementation
    })

    pw = pywren.ibm_cf_executor(config=config, runtime_memory=2048)
    iterdata = f'{bucket}/{formulas_chunks_prefix}'
    futures = pw.map(calculate_peaks_for_chunk, iterdata)
    centroids_chunks_n = pw.get_result(futures)
    append_pywren_stats(futures, pw.config['pywren']['runtime_memory'])

    num_centroids = sum(centroids_chunks_n)
    n_centroids_chunks = len(centroids_chunks_n)
    return num_centroids, n_centroids_chunks


def build_database(config, input_db):
    bucket = config["storage"]["db_bucket"]
    formulas_chunks_prefix = input_db["formulas_chunks"]
    clean_from_cos(config, bucket, formulas_chunks_prefix)

    def generate_formulas(key, data_stream, adduct, modifiers, ibm_cos):
        mols = pickle.loads(data_stream.read())
        formulas = set()

        for modifier in modifiers:
            formulas.update(map(safe_generate_ion_formula, mols, repeat(modifier), repeat(adduct)))

        if None in formulas:
            formulas.remove(None)

        chunk = pd.DataFrame(sorted(formulas), columns=['formula'])
        chunk.index.name = 'formula_i'
        database_name = key.split('/')[-1].split('.')[0]
        chunk_key = f'{formulas_chunks_prefix}/{database_name}/{adduct}.msgpack'
        ibm_cos.put_object(Bucket=bucket, Key=chunk_key, Body=chunk.to_msgpack())

        return len(formulas)

    adducts = [*input_db['adducts'], *DECOY_ADDUCTS]
    modifiers = input_db['modifiers']
    databases = input_db['databases']

    pw = pywren.ibm_cf_executor(config=config, runtime_memory=512)
    iterdata = [(f'{bucket}/{database}', adduct, modifiers) for database in databases for adduct in adducts]
    futures = pw.map(generate_formulas, iterdata)
    nums_formulas = pw.get_result(futures)
    append_pywren_stats(futures, pw.config['pywren']['runtime_memory'])

    num_formulas = sum(nums_formulas)
    n_formulas_chunks = len(nums_formulas)
    return num_formulas, n_formulas_chunks


def get_formula_id_dfs(ibm_cos, bucket, formulas_chunks_prefix, n_formulas_chunks=256):
    def get_formula_chunk(formula_chunk_key):
        data_stream = ibm_cos.get_object(Bucket=bucket, Key=formula_chunk_key)['Body']
        formula_chunk = pd.read_msgpack(data_stream._raw_stream)
        return formula_chunk

    with ThreadPoolExecutor(max_workers=128) as pool:
        iterdata = [f'{formulas_chunks_prefix}/{chunk_i}.msgpack' for chunk_i in range(n_formulas_chunks)]
        results = list(pool.map(get_formula_chunk, iterdata))

    formulas = pd.concat(results)
    formula_to_id = dict(zip(formulas.formula, formulas.index))
    id_to_formula = formulas.formula.to_dict()

    return formula_to_id, id_to_formula


def dump_mol_db(config, bucket, key, db_id, force=False):
    import requests
    ibm_cos = get_ibm_cos_client(config)
    try:
        ibm_cos.head_object(Bucket=bucket, Key=key)
        should_dump = force
    except ClientError:
        should_dump = True

    if should_dump:
        mols = requests.get(f'https://metaspace2020.eu/mol_db/v1/databases/{db_id}/molecules?limit=999999&fields=sf').json()['data']
        mols_df = sorted(set(mol['sf'] for mol in mols))
        with BytesIO() as fileobj:
            ibm_cos.put_object(Bucket=bucket, Key=key, Body=pickle.dumps(mols_df))
