//! Geometry kernels for the world server (server/fast.py wraps them, with the Python versions as the fallback).
//! All coordinates are metres in the anchor's local frame, f64.
use numpy::{PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3, IntoPyArray};
use pyo3::types::PyBytes;
use pyo3::prelude::*;

/// Even-odd point-in-polygon (the polygon's last vertex joins its first), as islands._inside.
#[inline]
fn inside(px: &[f64], py: &[f64], x: f64, y: f64) -> bool {
    let n = px.len();
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (px[i], py[i], px[j], py[j]);
        if (yi > y) != (yj > y) {
            let dy = if yj - yi == 0.0 { 1e-12 } else { yj - yi };
            if x < (xj - xi) * (y - yi) / dy + xi {
                c = !c;
            }
        }
        j = i;
    }
    c
}

/// Distance from (x, y) to segment a-b.
#[inline]
fn seg_dist(ax: f64, ay: f64, bx: f64, by: f64, x: f64, y: f64) -> f64 {
    let (dx, dy) = (bx - ax, by - ay);
    let l2 = (dx * dx + dy * dy).max(1e-12);
    let t = (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0);
    let (qx, qy) = (ax + dx * t - x, ay + dy * t - y);
    (qx * qx + qy * qy).sqrt()
}

/// Is (x, y) within its segment's half width of any segment (segs: (m, 4) ax ay bx by, hw: (m,))?
#[inline]
fn near_any(segs: &[[f64; 4]], hw: &[f64], x: f64, y: f64) -> bool {
    segs.iter().zip(hw).any(|(s, &h)| seg_dist(s[0], s[1], s[2], s[3], x, y) <= h)
}

fn to_segs(segs: &PyReadonlyArray2<f64>) -> Vec<[f64; 4]> {
    let a = segs.as_array();
    a.rows().into_iter().map(|r| [r[0], r[1], r[2], r[3]]).collect()
}

/// islands._island_ring's ray march: from (cx, cy) along `rays` evenly spaced directions, in steps of `step` up to
/// rmax, the last point inside poly and clear of every segment's half width. Returns (rays, 2).
#[pyfunction]
fn island_ring_rays<'py>(py: Python<'py>, poly: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                         hw: PyReadonlyArray1<'py, f64>, cx: f64, cy: f64, rays: usize, step: f64, rmax: f64)
                         -> Bound<'py, PyArray2<f64>> {
    let p = poly.as_array();
    let px: Vec<f64> = p.column(0).to_vec();
    let pys: Vec<f64> = p.column(1).to_vec();
    let s = to_segs(&segs);
    let h = hw.as_slice().unwrap().to_vec();
    let out = py.allow_threads(|| {
        let mut out = Vec::with_capacity(rays * 2);
        for k in 0..rays {
            let a = 2.0 * std::f64::consts::PI * (k as f64) / (rays as f64);
            let (ux, uy) = (a.cos(), a.sin());
            let mut r = 0.0;
            while r < rmax {
                let (qx, qy) = (cx + ux * (r + step), cy + uy * (r + step));
                if !inside(&px, &pys, qx, qy) || near_any(&s, &h, qx, qy) {
                    break;
                }
                r += step;
            }
            out.push(cx + ux * r);
            out.push(cy + uy * r);
        }
        out
    });
    numpy::ndarray::Array2::from_shape_vec((rays, 2), out).unwrap().into_pyarray(py)
}

/// Point-in-polygon for many points: pts (n, 2) -> bool (n,).
#[pyfunction]
fn points_inside<'py>(py: Python<'py>, poly: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                      -> Bound<'py, PyArray1<bool>> {
    let p = poly.as_array();
    let px: Vec<f64> = p.column(0).to_vec();
    let pys: Vec<f64> = p.column(1).to_vec();
    let q = pts.as_array();
    let v: Vec<bool> = q.rows().into_iter().map(|r| inside(&px, &pys, r[0], r[1])).collect();
    v.into_pyarray(py)
}

/// Distance from each point to the nearest of the polyline's segments: line (k, 2), pts (n, 2) -> (n,).
#[pyfunction]
fn polyline_dist<'py>(py: Python<'py>, line: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                      -> Bound<'py, PyArray1<f64>> {
    let l = line.as_array();
    let q = pts.as_array();
    let k = l.nrows();
    let v: Vec<f64> = q.rows().into_iter().map(|r| {
        let mut best = f64::INFINITY;
        for i in 0..k.saturating_sub(1) {
            let d = seg_dist(l[[i, 0]], l[[i, 1]], l[[i + 1, 0]], l[[i + 1, 1]], r[0], r[1]);
            if d < best { best = d; }
        }
        best
    }).collect();
    v.into_pyarray(py)
}

/// furniture._clear: is (px, py) farther than its segment's half width + margin from every segment not owned by
/// `own` (segs (m, 4), half (m,), owner (m,) as i64)?
#[pyfunction]
fn clear_of(segs: PyReadonlyArray2<f64>, half: PyReadonlyArray1<f64>, owner: PyReadonlyArray1<i64>, px: f64, py: f64,
            own: i64, margin: f64) -> bool {
    let s = segs.as_array();
    let h = half.as_slice().unwrap();
    let o = owner.as_slice().unwrap();
    for i in 0..s.nrows() {
        if o[i] == own {
            continue;
        }
        if seg_dist(s[[i, 0]], s[[i, 1]], s[[i, 2]], s[[i, 3]], px, py) < h[i] + margin {
            return false;
        }
    }
    true
}

/// roads._dual_side: (share of a's ~10 m samples with b beside it gap_min..gap_max apart and parallel within
/// cos_par, mean side of b over those: +1 left of a's direction, -1 right). a (k, 2), b (m, 2).
#[pyfunction]
fn dual_side(a: PyReadonlyArray2<f64>, b: PyReadonlyArray2<f64>, gap_min: f64, gap_max: f64, cos_par: f64)
             -> (f64, f64) {
    let a = a.as_array();
    let b = b.as_array();
    let ka = a.nrows();
    let kb = b.nrows();
    if ka < 2 || kb < 2 {
        return (0.0, 0.0);
    }
    let nseg = ka - 1;
    let mut s = vec![0.0f64; ka];
    let mut seg = vec![[0.0f64; 2]; nseg];
    let mut len = vec![0.0f64; nseg];
    for i in 0..nseg {
        let dx = a[[i + 1, 0]] - a[[i, 0]];
        let dy = a[[i + 1, 1]] - a[[i, 1]];
        seg[i] = [dx, dy];
        len[i] = (dx * dx + dy * dy).sqrt();
        s[i + 1] = s[i] + len[i];
    }
    let total = s[ka - 1];
    if total < 1.0 {
        return (0.0, 0.0);
    }
    let n = std::cmp::max(2, (total / 10.0).floor() as usize + 1);
    let mb = kb - 1;
    let mut bs = vec![[0.0f64; 2]; mb];
    let mut bl2 = vec![0.0f64; mb];
    for i in 0..mb {
        let dx = b[[i + 1, 0]] - b[[i, 0]];
        let dy = b[[i + 1, 1]] - b[[i, 1]];
        bs[i] = [dx, dy];
        bl2[i] = (dx * dx + dy * dy).max(1e-12);
    }
    let (mut ok_n, mut side_sum) = (0usize, 0.0f64);
    let step = total / ((n - 1) as f64);
    for si in 0..n {
        // np.linspace: i * step, the last exactly the end
        let t = if si == n - 1 { total } else { (si as f64) * step };
        // np.searchsorted(s, t, side="right") - 1, clipped to a segment
        let mut k = s.partition_point(|&v| v <= t) as isize - 1;
        k = k.clamp(0, nseg as isize - 1);
        let k = k as usize;
        // np.interp(t, s, a): linear within the segment holding t (the last point at the end)
        let (qx, qy) = if t >= total {
            (a[[ka - 1, 0]], a[[ka - 1, 1]])
        } else {
            let f = if len[k] > 0.0 { (t - s[k]) / len[k] } else { 0.0 };
            (a[[k, 0]] + seg[k][0] * f, a[[k, 1]] + seg[k][1] * f)
        };
        let lk = len[k].max(1e-9);
        let (tax, tay) = (seg[k][0] / lk, seg[k][1] / lk);
        let (mut best, mut bj, mut nx, mut ny) = (f64::INFINITY, 0usize, 0.0, 0.0);
        for j in 0..mb {
            let f = (((qx - b[[j, 0]]) * bs[j][0] + (qy - b[[j, 1]]) * bs[j][1]) / bl2[j]).clamp(0.0, 1.0);
            let (px, py) = (b[[j, 0]] + bs[j][0] * f, b[[j, 1]] + bs[j][1] * f);
            let d = (px - qx).hypot(py - qy);    // (as np.hypot: ties at a shared vertex pick the same segment)
            if d < best {
                best = d;
                bj = j;
                nx = px;
                ny = py;
            }
        }
        let bn = bl2[bj].sqrt();
        let (tbx, tby) = (bs[bj][0] / bn, bs[bj][1] / bn);
        let par = (tax * tbx + tay * tby).abs() >= cos_par;
        if best >= gap_min && best <= gap_max && par {
            ok_n += 1;
            let (vx, vy) = (nx - qx, ny - qy);
            let c = tax * vy - tay * vx;
            side_sum += if c > 0.0 { 1.0 } else if c < 0.0 { -1.0 } else { 0.0 };
        }
    }
    if ok_n == 0 {
        return (0.0, 0.0);
    }
    (ok_n as f64 / n as f64, side_sum / ok_n as f64)
}

/// roads._deck_crossings: (i, j, arc on i, arc on j) where polylines i < j cross away from their end points.
/// pts (n, 2) all lines one after another, starts (L + 1,) where each begins (the last: n).
#[pyfunction]
fn deck_crossings(pts: PyReadonlyArray2<f64>, starts: PyReadonlyArray1<i64>) -> Vec<(usize, usize, f64, f64)> {
    let p = pts.as_array();
    let st = starts.as_array();
    let nl = st.len().saturating_sub(1);
    let rng: Vec<(usize, usize)> = (0..nl).map(|i| (st[i] as usize, st[i + 1] as usize)).collect();
    let mut boxes = Vec::with_capacity(nl);
    let mut arcs: Vec<Vec<f64>> = Vec::with_capacity(nl);
    for &(a, b) in &rng {
        let (mut lo, mut hi) = ([f64::INFINITY; 2], [f64::NEG_INFINITY; 2]);
        let mut arc = Vec::with_capacity(b - a);
        let mut acc = 0.0;
        for k in a..b {
            for c in 0..2 {
                lo[c] = lo[c].min(p[[k, c]]);
                hi[c] = hi[c].max(p[[k, c]]);
            }
            if k > a {
                acc += (p[[k, 0]] - p[[k - 1, 0]]).hypot(p[[k, 1]] - p[[k - 1, 1]]);
            }
            arc.push(acc);
        }
        boxes.push((lo, hi));
        arcs.push(arc);
    }
    let mut out = Vec::new();
    for i in 0..nl {
        let (ai, bi) = rng[i];
        if bi - ai < 2 {
            continue;
        }
        for j in (i + 1)..nl {
            let (aj, bj) = rng[j];
            if bj - aj < 2 {
                continue;
            }
            let ((alo, ahi), (blo, bhi)) = (boxes[i], boxes[j]);
            if ahi[0] < blo[0] || bhi[0] < alo[0] || ahi[1] < blo[1] || bhi[1] < alo[1] {
                continue;
            }
            let (si_end, sj_end) = (arcs[i][bi - ai - 1], arcs[j][bj - aj - 1]);
            for q in 0..(bj - aj - 1) {
                let (ax, ay) = (p[[aj + q, 0]], p[[aj + q, 1]]);
                let (d0, d1) = (p[[aj + q + 1, 0]] - ax, p[[aj + q + 1, 1]] - ay);
                for k in 0..(bi - ai - 1) {
                    let (px, py) = (p[[ai + k, 0]], p[[ai + k, 1]]);
                    let (r0, r1) = (p[[ai + k + 1, 0]] - px, p[[ai + k + 1, 1]] - py);
                    let den = r0 * d1 - r1 * d0;
                    if den.abs() <= 1e-12 {
                        continue;
                    }
                    let (ap0, ap1) = (ax - px, ay - py);
                    let t = (ap0 * d1 - ap1 * d0) / den;
                    let u = (ap0 * r1 - ap1 * r0) / den;
                    if t > 1e-6 && t < 1.0 - 1e-6 && u > 1e-6 && u < 1.0 - 1e-6 {
                        let si = arcs[i][k] + t * (arcs[i][k + 1] - arcs[i][k]);
                        let sj = arcs[j][q] + u * (arcs[j][q + 1] - arcs[j][q]);
                        if si.min(si_end - si).min(sj).min(sj_end - sj) > 1.0 {
                            out.push((i, j, si, sj));
                        }
                    }
                }
            }
        }
    }
    out
}

/// One point in a polygon (n, >=2), even-odd, as islands._inside / buildings._inside.
#[pyfunction]
fn point_inside(poly: PyReadonlyArray2<f64>, x: f64, y: f64) -> bool {
    let p = poly.as_array();
    let n = p.nrows();
    if n == 0 {
        return false;
    }
    let mut c = false;
    let mut j = n - 1;
    for i in 0..n {
        let (xi, yi, xj, yj) = (p[[i, 0]], p[[i, 1]], p[[j, 0]], p[[j, 1]]);
        if (yi > y) != (yj > y) {
            let dy = if yj - yi == 0.0 { 1e-12 } else { yj - yi };
            if x < (xj - xi) * (y - yi) / dy + xi {
                c = !c;
            }
        }
        j = i;
    }
    c
}

/// geodesy.EnuFrame.geodetic_to_enu for 1-D lat, lon, h (degrees, metres): WGS84 to ECEF, then into the frame
/// (origin (3,) ECEF, rot (3, 3) rows E, N, U). -> (e, n, u).
#[pyfunction]
fn geodetic_to_enu<'py>(py: Python<'py>, lat: PyReadonlyArray1<'py, f64>, lon: PyReadonlyArray1<'py, f64>,
                        h: PyReadonlyArray1<'py, f64>, origin: PyReadonlyArray1<'py, f64>,
                        rot: PyReadonlyArray2<'py, f64>)
                        -> (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>) {
    const A: f64 = 6378137.0;
    const F: f64 = 1.0 / 298.257223563;
    const E2: f64 = F * (2.0 - F);
    let (la, lo, hh, o, r) = (lat.as_array(), lon.as_array(), h.as_array(), origin.as_array(), rot.as_array());
    let n = la.len();
    let (mut e, mut nn, mut u) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
    for i in 0..n {
        let (phi, lam, hv) = (la[i].to_radians(), lo[i].to_radians(), hh[i]);
        let (sl, cl) = (phi.sin(), phi.cos());
        let nr = A / (1.0 - E2 * sl * sl).sqrt();
        let rr = (nr + hv) * cl;
        let x = rr * lam.cos() - o[0];
        let y = rr * lam.sin() - o[1];
        let z = (nr * (1.0 - E2) + hv) * sl - o[2];
        e.push(r[[0, 0]] * x + r[[0, 1]] * y + r[[0, 2]] * z);
        nn.push(r[[1, 0]] * x + r[[1, 1]] * y + r[[1, 2]] * z);
        u.push(r[[2, 0]] * x + r[[2, 1]] * y + r[[2, 2]] * z);
    }
    (e.into_pyarray(py), nn.into_pyarray(py), u.into_pyarray(py))
}

/// bc7.encode: BC7 mode-6 blocks (16 bytes each, row-major) of an (h, w, 3|4) uint8 image, edges padded by
/// repetition. The same encoder as the numpy one: endpoints along the block's main axis (power iteration on the
/// RGBA covariance), 7-bit endpoints with the best p-bit, indices by projection onto the quantised endpoints.
const BC7_W: [f64; 16] = [0., 4., 9., 13., 17., 21., 26., 30., 34., 38., 43., 47., 51., 55., 60., 64.];

fn bc7_quantise(e: [f64; 4], opaque: bool) -> ([i32; 4], i32) {
    let mut best_q = [0i32; 4];
    let mut best_p = 0;
    let mut best_err = f64::INFINITY;
    for p in 0..2 {
        let mut q = [0i32; 4];
        let mut err = 0.0;
        for c in 0..4 {
            q[c] = ((e[c] - p as f64) / 2.0).round_ties_even().clamp(0.0, 127.0) as i32;
            err += (((q[c] << 1) | p) as f64 - e[c]).abs();
        }
        if err < best_err {
            best_q = q;
            best_p = p;
            best_err = err;
        }
    }
    if opaque {
        for c in 0..4 {
            best_q[c] = ((e[c] - 1.0) / 2.0).round_ties_even().clamp(0.0, 127.0) as i32;
        }
        best_q[3] = 127;
        best_p = 1;
    }
    (best_q, best_p)
}

fn bc7_block(t: &[[i32; 4]; 16]) -> [u8; 16] {
    let mut mean = [0f64; 4];
    for k in 0..16 {
        for c in 0..4 {
            mean[c] += t[k][c] as f64;
        }
    }
    for c in 0..4 {
        mean[c] /= 16.0;
    }
    let mut d = [[0f64; 4]; 16];
    for k in 0..16 {
        for c in 0..4 {
            d[k][c] = t[k][c] as f64 - mean[c];
        }
    }
    let mut cov = [[0f64; 4]; 4];
    for i in 0..4 {
        for j in 0..4 {
            let mut s = 0.0;
            for k in 0..16 {
                s += d[k][i] * d[k][j];
            }
            cov[i][j] = s;
        }
    }
    let mut v = [0f64; 4];
    let mut m = 0;
    for i in 1..4 {
        if cov[i][i] > cov[m][m] {
            m = i;
        }
    }
    v[m] = 1.0;
    for _ in 0..4 {
        let mut w = [0f64; 4];
        for i in 0..4 {
            for j in 0..4 {
                w[i] += cov[i][j] * v[j];
            }
        }
        let n = (w[0] * w[0] + w[1] * w[1] + w[2] * w[2] + w[3] * w[3]).sqrt().max(1e-9);
        for i in 0..4 {
            v[i] = w[i] / n;
        }
    }
    let (mut pmin, mut pmax) = (f64::INFINITY, f64::NEG_INFINITY);
    for k in 0..16 {
        let p = d[k][0] * v[0] + d[k][1] * v[1] + d[k][2] * v[2] + d[k][3] * v[3];
        pmin = pmin.min(p);
        pmax = pmax.max(p);
    }
    let opaque = t.iter().all(|x| x[3] == 255);
    let mut lo = [0f64; 4];
    let mut hi = [0f64; 4];
    for c in 0..4 {
        lo[c] = (mean[c] + v[c] * pmin).clamp(0.0, 255.0);
        hi[c] = (mean[c] + v[c] * pmax).clamp(0.0, 255.0);
    }
    let (mut q0, mut p0) = bc7_quantise(lo, opaque);
    let (mut q1, mut p1) = bc7_quantise(hi, opaque);
    let mut e0 = [0i32; 4];
    let mut e1 = [0i32; 4];
    for c in 0..4 {
        e0[c] = (q0[c] << 1) | p0;
        e1[c] = (q1[c] << 1) | p1;
    }
    let mut axis = [0f64; 4];
    let mut den = 0.0;
    for c in 0..4 {
        axis[c] = (e1[c] - e0[c]) as f64;
        den += axis[c] * axis[c];
    }
    let den = den.max(1e-9);
    let mut idx = [0u8; 16];
    for k in 0..16 {
        let mut dot = 0.0;
        for c in 0..4 {
            dot += (t[k][c] - e0[c]) as f64 * axis[c];
        }
        let f = dot / den * 64.0;
        let mut bi = 0;
        let mut be = f64::INFINITY;
        for (w, wv) in BC7_W.iter().enumerate() {
            let e = (f - wv).abs();
            if e < be {
                be = e;
                bi = w;
            }
        }
        idx[k] = bi as u8;
    }
    if idx[0] >= 8 {
        std::mem::swap(&mut q0, &mut q1);
        std::mem::swap(&mut p0, &mut p1);
        for k in 0..16 {
            idx[k] = 15 - idx[k];
        }
    }
    let mut bits: u128 = 1 << 6;
    let mut pos = 7;
    let mut put = |v: u128, n: u32, pos: &mut u32| {
        bits |= v << *pos;
        *pos += n;
    };
    for c in 0..4 {
        put(q0[c] as u128, 7, &mut pos);
        put(q1[c] as u128, 7, &mut pos);
    }
    put(p0 as u128, 1, &mut pos);
    put(p1 as u128, 1, &mut pos);
    put(idx[0] as u128, 3, &mut pos);
    for k in 1..16 {
        put(idx[k] as u128, 4, &mut pos);
    }
    bits.to_le_bytes()
}

#[pyfunction]
fn bc7_encode<'py>(py: Python<'py>, img: PyReadonlyArray3<'py, u8>) -> Bound<'py, PyBytes> {
    let a = img.as_array();
    let (h, w, ch) = (a.shape()[0], a.shape()[1], a.shape()[2]);
    let (nby, nbx) = ((h + 3) / 4, (w + 3) / 4);
    let mut out = vec![0u8; nby * nbx * 16];
    let mut t = [[0i32; 4]; 16];
    for by in 0..nby {
        for bx in 0..nbx {
            for yy in 0..4 {
                let y = (by * 4 + yy).min(h - 1);
                for xx in 0..4 {
                    let x = (bx * 4 + xx).min(w - 1);
                    let k = yy * 4 + xx;
                    for c in 0..4 {
                        t[k][c] = if c < ch { a[[y, x, c]] as i32 } else { 255 };
                    }
                }
            }
            let blk = bc7_block(&t);
            let o = (by * nbx + bx) * 16;
            out[o..o + 16].copy_from_slice(&blk);
        }
    }
    PyBytes::new(py, &out)
}

#[pymodule]
fn earthfast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(island_ring_rays, m)?)?;
    m.add_function(wrap_pyfunction!(points_inside, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_dist, m)?)?;
    m.add_function(wrap_pyfunction!(clear_of, m)?)?;
    m.add_function(wrap_pyfunction!(dual_side, m)?)?;
    m.add_function(wrap_pyfunction!(deck_crossings, m)?)?;
    m.add_function(wrap_pyfunction!(point_inside, m)?)?;
    m.add_function(wrap_pyfunction!(geodetic_to_enu, m)?)?;
    m.add_function(wrap_pyfunction!(bc7_encode, m)?)?;
    m.add("VERSION", 6)?;
    Ok(())
}
