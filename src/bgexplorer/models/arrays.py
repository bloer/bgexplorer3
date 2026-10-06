""" Storage of numpy arrays as raw bytes in bson documents """
from collections.abc import Mapping
from typing import Union
import zlib
import numpy as np

# arrays larger than this many bytes are zlib compressed for storage
ZLIB_THRESHOLD = 64 * 1024


def encode_array(arr: np.ndarray) -> Union[dict, list, float, int]:
    """ Encode a numpy array for storage as a bson subdocument with the raw
    bytes, which is much faster to encode and decode than npz or lists.
    Only large arrays are compressed; mongodb compresses documents on disk
    anyway.
    """
    if arr.ndim == 0:
        return arr.item()
    if arr.dtype.hasobject:
        return arr.tolist()
    data = arr.tobytes()
    result = dict(dtype=arr.dtype.str, shape=list(arr.shape))
    if len(data) > ZLIB_THRESHOLD:
        data = zlib.compress(data, 1)
        result['zlib'] = True
    result['data'] = data
    return result


def is_encoded_array(value) -> bool:
    return (isinstance(value, Mapping) and 'dtype' in value
            and 'data' in value)


def decode_array(value: Mapping) -> np.ndarray:
    """ Inverse of `encode_array` """
    data = value['data']
    if value.get('zlib'):
        data = zlib.decompress(data)
    # copy, since arrays backed by bytes are read-only
    return (np.frombuffer(data, dtype=value['dtype'])
            .reshape(value['shape']).copy())
