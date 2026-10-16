"""BC7 (mode 6) encoding in numpy, for the terrain's base-colour photo (detail near-visual mode).

The TerrainMaterial texture arrays are BC7 (sRGB for colour: DXGI 99, as the engine's own
temp/art/terrainmaterialcache/*.dds). Any other input - PNG, a DXT1 DDS - is encoded to BC7 by the game on
its main thread the first time it is used: 2-8 s for a 2048² photo, a stall at every terrain move. A DDS
that is already BC7 with its mip chain is taken as it is (~30 ms, in-game 2026-10-12). So the server
encodes the photo here, off the game's thread.

Mode 6 only: one RGBA endpoint pair per 4x4 block (7 bits + a shared p-bit per endpoint) and a 4-bit index
per texel. Endpoints: the block's extent along its main axis (the bounding-box diagonal, oriented to the
covariance), indices: the nearest of the 16 interpolated colours. Good for photos; no partitions (sharp
two-colour edges blur a little within a block). ~1.3 s for a 2048² image with its mips.

encode(rgb) -> bytes (blocks, row-major), decode(blocks, w, h) -> rgb (tests); dds(rgb) -> a DDS file's bytes
(DX10 header, BC7_UNORM_SRGB, full mip chain, box-filtered mips).
"""
import struct

import numpy as np

try:
    import earthfast as _ef     # the Rust encoder: the same blocks, byte for byte, ~8x faster (2048²: 1.2 s -> 0.14 s)
except ImportError:
    _ef = None

WEIGHTS = np.array([0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64], np.int32)
DXGI_BC7_UNORM_SRGB = 99


def _blocks(img):
    """(h, w, 3|4) uint8 -> (nby, nbx, 16, 4) int32 RGBA texels (edges padded by repetition)."""
    h, w = img.shape[:2]
    if img.shape[2] == 3:
        img = np.concatenate([img, np.full((h, w, 1), 255, np.uint8)], 2)
    ph, pw = (-h) % 4, (-w) % 4
    if ph or pw:
        img = np.pad(img, ((0, ph), (0, pw), (0, 0)), mode="edge")
    H, W = img.shape[:2]
    b = img.reshape(H // 4, 4, W // 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(H // 4, W // 4, 16, 4)
    return b.astype(np.int32)


def _quantise(e, opaque=None):
    """Endpoint (..., 4) in 0..255 -> 7-bit values (..., 4) and a p-bit (...) choosing the best of 0 / 1
    (opaque: (...) bool, blocks whose alpha must stay exactly 255: p-bit 1 there)."""
    best = None
    for p in (0, 1):
        q = np.clip(np.round((e - p) / 2.0), 0, 127).astype(np.int32)
        err = np.abs(((q << 1) | p) - e).sum(-1)
        if best is None:
            best = (q, np.full(e.shape[:-1], p, np.int32), err)
        else:
            take = err < best[2]
            best = (np.where(take[..., None], q, best[0]), np.where(take, p, best[1]), np.where(take, err, best[2]))
    q, pb = best[0], best[1]
    if opaque is not None and opaque.any():
        q1 = np.clip(np.round((e - 1) / 2.0), 0, 127).astype(np.int32)
        q = np.where(opaque[..., None], q1, q)
        q[..., 3] = np.where(opaque, 127, q[..., 3])
        pb = np.where(opaque, 1, pb)
    return q, pb


def encode(img):
    """BC7 mode-6 blocks of an (h, w, 3|4) uint8 image, row-major, 16 bytes each."""
    img = np.asarray(img, np.uint8)
    if _ef is not None and hasattr(_ef, "bc7_encode") and img.ndim == 3 and img.shape[2] in (3, 4):
        return bytes(_ef.bc7_encode(np.ascontiguousarray(img)))
    return _encode_py(img)


def _encode_py(img):
    t = _blocks(np.asarray(img, np.uint8))
    nby, nbx = t.shape[:2]
    t = t.reshape(-1, 16, 4)
    mean = t.mean(1, keepdims=True)
    d = t - mean
    # main axis: power iteration on the RGBA covariance (from the max-variance channel)
    cov = np.einsum("nki,nkj->nij", d, d).astype(np.float64)
    v = np.zeros((len(t), 4))
    v[np.arange(len(t)), np.argmax(np.diagonal(cov, axis1=1, axis2=2), 1)] = 1.0
    for _ in range(4):
        v = np.einsum("nij,nj->ni", cov, v)
        v /= np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-9)
    proj = np.einsum("nki,ni->nk", d, v)
    lo = mean[:, 0] + v * proj.min(1, keepdims=True)
    hi = mean[:, 0] + v * proj.max(1, keepdims=True)
    opaque = (t[:, :, 3] == 255).all(1)
    q0, p0 = _quantise(np.clip(lo, 0, 255), opaque)
    q1, p1 = _quantise(np.clip(hi, 0, 255), opaque)
    e0, e1 = (q0 << 1) | p0[:, None], (q1 << 1) | p1[:, None]
    # index: the texel's position between the (quantised) endpoints along their line, rounded to the
    # nearest of the 16 weights (the palette lies on that line; ~3x faster than a full nearest search)
    axis = (e1 - e0).astype(np.float64)
    den = np.maximum((axis * axis).sum(1), 1e-9)
    f = np.einsum("nki,ni->nk", t - e0[:, None, :], axis) / den[:, None] * 64.0
    idx = np.abs(f[..., None] - WEIGHTS[None, None, :]).argmin(-1)
    # the anchor (texel 0) index must have its top bit clear: else swap the endpoints, invert the indices
    flip = idx[:, 0] >= 8
    q0, q1 = np.where(flip[:, None], q1, q0), np.where(flip[:, None], q0, q1)
    p0, p1 = np.where(flip, p1, p0), np.where(flip, p0, p1)
    idx = np.where(flip[:, None], 15 - idx, idx)
    # pack 128 bits, LSB first: mode 6 (7 bits: 1 << 6), R0 R1 G0 G1 B0 B1 A0 A1 (7 each), P0, P1,
    # index 0 (3 bits), indices 1..15 (4 bits each)
    fields = [(np.full(len(t), 1 << 6, np.uint64), 7)]
    for c in range(4):
        fields += [(q0[:, c].astype(np.uint64), 7), (q1[:, c].astype(np.uint64), 7)]
    fields += [(p0.astype(np.uint64), 1), (p1.astype(np.uint64), 1), (idx[:, 0].astype(np.uint64), 3)]
    fields += [(idx[:, k].astype(np.uint64), 4) for k in range(1, 16)]
    lo64 = np.zeros(len(t), np.uint64)
    hi64 = np.zeros(len(t), np.uint64)
    pos = 0
    for val, nb in fields:                     # whole fields; one straddles the 64-bit boundary
        if pos + nb <= 64:
            lo64 |= val << np.uint64(pos)
        elif pos >= 64:
            hi64 |= val << np.uint64(pos - 64)
        else:
            k = 64 - pos
            lo64 |= (val & np.uint64((1 << k) - 1)) << np.uint64(pos)
            hi64 |= val >> np.uint64(k)
        pos += nb
    assert pos == 128
    out = np.empty((len(t), 2), "<u8")
    out[:, 0], out[:, 1] = lo64, hi64
    return out.tobytes()


def decode(data, w, h):
    """RGBA uint8 (h, w, 4) of BC7 mode-6 blocks (the encoder's output; other modes are not decoded)."""
    nbx, nby = (w + 3) // 4, (h + 3) // 4
    a = np.frombuffer(data, "<u8").reshape(-1, 2)[: nbx * nby]
    lo64, hi64 = a[:, 0], a[:, 1]

    def bits(pos, nb):
        v = np.zeros(len(a), np.int64)
        for b in range(nb):
            p = pos + b
            src = lo64 if p < 64 else hi64
            v |= (((src >> np.uint64(p % 64)) & np.uint64(1)).astype(np.int64)) << b
        return v
    pos = 7
    q = []
    for _ in range(8):
        q.append(bits(pos, 7))
        pos += 7
    p0, p1 = bits(pos, 1), bits(pos + 1, 1)
    pos += 2
    e0 = np.stack([(q[2 * c] << 1) | p0 for c in range(4)], 1)
    e1 = np.stack([(q[2 * c + 1] << 1) | p1 for c in range(4)], 1)
    idx = [bits(pos, 3)]
    pos += 3
    for _ in range(15):
        idx.append(bits(pos, 4))
        pos += 4
    idx = np.stack(idx, 1)
    wgt = WEIGHTS[idx]
    col = (e0[:, None, :] * (64 - wgt[..., None]) + e1[:, None, :] * wgt[..., None] + 32) >> 6
    img = col.reshape(nby, nbx, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(nby * 4, nbx * 4, 4)
    return img[:h, :w].astype(np.uint8)


def mips(img):
    """The mip chain of a square power-of-two image down to 1x1 (imagery.mip_arrays: linear light, sharpened)."""
    import imagery
    return imagery.mip_arrays(np.asarray(img, np.uint8))


def dds(rgb):
    """A BC7_UNORM_SRGB DDS (DX10 header, the full mip chain) of a square power-of-two RGB image."""
    img = np.asarray(rgb, np.uint8)
    h, w = img.shape[:2]
    chain = mips(img)
    body = b"".join(encode(m) for m in chain)
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000      # CAPS HEIGHT WIDTH PIXELFORMAT MIPMAPCOUNT LINEARSIZE
    head = struct.pack("<4s7I", b"DDS ", 124, flags, h, w, max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * 16, 1, len(chain))
    head += b"\0" * 44                                                 # reserved
    head += struct.pack("<2I4s5I", 32, 0x4, b"DX10", 0, 0, 0, 0, 0)   # pixel format: FOURCC DX10
    head += struct.pack("<4I", 0x401008, 0, 0, 0)                     # caps: COMPLEX TEXTURE MIPMAP
    head += struct.pack("<I", 0)                                       # reserved2
    head += struct.pack("<5I", DXGI_BC7_UNORM_SRGB, 3, 0, 1, 0)       # DX10: format, TEXTURE2D, -, array 1, -
    return head + body
