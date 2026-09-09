"""Private negotiated block codec; object identity remains decoded SHA-256."""
import time
import zlib
from .model import WorkloadError

BLOCK_BYTES = 4*1024*1024
HEADER = 'X-Harmony-Block-Encoding'


def read_gzip_block(response, wire_length, decoded_length, deadline):
    if not 0 < decoded_length <= BLOCK_BYTES or not 0 < wire_length <= BLOCK_BYTES+65536:
        raise WorkloadError('compressed block exceeds size bound', 413)
    wire = bytearray()
    while len(wire) < wire_length:
        if time.monotonic() >= deadline:
            raise WorkloadError('download duration budget exhausted', 503)
        chunk = response.read1(min(65536, wire_length-len(wire)))
        if not chunk:
            raise ConnectionError('compressed block ended early')
        wire.extend(chunk)
    try:
        decoder = zlib.decompressobj(31)
        decoded = decoder.decompress(wire, decoded_length+1)
        if len(decoded) != decoded_length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise WorkloadError('compressed block decoded length mismatch', 409)
        return decoded
    except zlib.error as error:
        raise WorkloadError('compressed block content is corrupt', 409) from error
