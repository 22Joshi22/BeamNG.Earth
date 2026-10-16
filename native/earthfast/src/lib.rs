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

/// roads.cut_outside's test: which points (n, 2) lie within half width hw[j] of a segment of blocker j. segs (m, 4)
/// ax ay bx by of all blockers one after another, starts (B + 1,) where each blocker's begin, hw (B,).
#[pyfunction]
fn inside_blockers<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, segs: PyReadonlyArray2<'py, f64>,
                        starts: PyReadonlyArray1<'py, i64>, hw: PyReadonlyArray1<'py, f64>)
                        -> Bound<'py, PyArray1<bool>> {
    let p = pts.as_array();
    let sg = segs.as_array();
    let st = starts.as_array();
    let h = hw.as_array();
    let n = p.nrows();
    let nb = h.len();
    let mut boxes = Vec::with_capacity(nb);
    for b in 0..nb {
        let (s0, s1) = (st[b] as usize, st[b + 1] as usize);
        let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
        for k in s0..s1 {
            x0 = x0.min(sg[[k, 0]]).min(sg[[k, 2]]);
            x1 = x1.max(sg[[k, 0]]).max(sg[[k, 2]]);
            y0 = y0.min(sg[[k, 1]]).min(sg[[k, 3]]);
            y1 = y1.max(sg[[k, 1]]).max(sg[[k, 3]]);
        }
        boxes.push((x0 - h[b], y0 - h[b], x1 + h[b], y1 + h[b]));
    }
    let mut out = vec![false; n];
    for i in 0..n {
        let (x, y) = (p[[i, 0]], p[[i, 1]]);
        'blk: for b in 0..nb {
            let (bx0, by0, bx1, by1) = boxes[b];
            if x < bx0 || x > bx1 || y < by0 || y > by1 {
                continue;
            }
            for k in (st[b] as usize)..(st[b + 1] as usize) {
                let (ax, ay) = (sg[[k, 0]], sg[[k, 1]]);
                let (dx, dy) = (sg[[k, 2]] - ax, sg[[k, 3]] - ay);
                let l2 = (dx * dx + dy * dy).max(1e-12);
                let t = (((x - ax) * dx + (y - ay) * dy) / l2).clamp(0.0, 1.0);
                if (x - (ax + t * dx)).hypot(y - (ay + t * dy)) < h[b] {
                    out[i] = true;
                    break 'blk;
                }
            }
        }
    }
    out.into_pyarray(py)
}

/// roads.densify: nodes added so no two are more than spacing apart in xy (columns beyond x, y interpolated too).
#[pyfunction]
fn densify<'py>(py: Python<'py>, pts: PyReadonlyArray2<'py, f64>, spacing: f64) -> Bound<'py, PyArray2<f64>> {
    let p = pts.as_array();
    let (k, c) = (p.nrows(), p.ncols());
    let mut out: Vec<f64> = Vec::with_capacity(k * c * 2);
    if k > 0 {
        out.extend(p.row(0).iter());
    }
    for i in 0..k.saturating_sub(1) {
        let (a, b) = (p.row(i), p.row(i + 1));
        let n = ((b[0] - a[0]).hypot(b[1] - a[1]) / spacing).ceil() as i64;
        for s in 1..n {
            let f = s as f64 / n as f64;
            for j in 0..c {
                out.push(a[j] + (b[j] - a[j]) * f);
            }
        }
        out.extend(b.iter());
    }
    let rows = out.len() / c.max(1);
    numpy::ndarray::Array2::from_shape_vec((rows, c), out).unwrap().into_pyarray(py)
}

/// roadwidth.Walls.rays for one ray: the distance to the first wall from (px, py) along unit (dx, dy) within
/// reach, or NaN. keys / first / counts / seg_of: the Walls bucket index, segs (m, 4).
fn wall_ray(keys: &[i64], first: &[i64], counts: &[i64], seg_of: &[i64], segs: &numpy::ndarray::ArrayView2<f64>,
            bucket: f64, px: f64, py: f64, dx: f64, dy: f64, reach: f64) -> f64 {
    let (qx, qy) = (px + dx * reach, py + dy * reach);
    let bx0 = (px.min(qx) / bucket).floor() as i64;
    let bx1 = (px.max(qx) / bucket).floor() as i64;
    let by0 = (py.min(qy) / bucket).floor() as i64;
    let by1 = (py.max(qy) / bucket).floor() as i64;
    let mut best = f64::INFINITY;
    for by in by0..=by1 {
        for bx in bx0..=bx1 {
            let key = (bx + (1i64 << 30)) * (1i64 << 31) + (by + (1i64 << 30));
            let Ok(i) = keys.binary_search(&key) else { continue };
            for j in first[i]..first[i] + counts[i] {
                let sgi = seg_of[j as usize] as usize;
                let (ax, ay) = (segs[[sgi, 0]], segs[[sgi, 1]]);
                let (ex, ey) = (segs[[sgi, 2]] - ax, segs[[sgi, 3]] - ay);
                let den = dx * ey - dy * ex;
                if den.abs() <= 1e-12 {
                    continue;
                }
                let (wx, wy) = (ax - px, ay - py);
                let t = (wx * ey - wy * ex) / den;
                let u = (wx * dy - wy * dx) / den;
                if t > 0.0 && t <= reach && u >= 0.0 && u <= 1.0 && t < best {
                    best = t;
                }
            }
        }
    }
    if best.is_finite() { best } else { f64::NAN }
}

/// numpy.interp at x of the increasing xp, fp.
fn np_interp(x: f64, xp: &[f64], fp: &[f64]) -> f64 {
    let n = xp.len();
    if x <= xp[0] {
        return fp[0];
    }
    if x >= xp[n - 1] {
        return fp[n - 1];
    }
    // the last j with xp[j] <= x
    let j = match xp.binary_search_by(|v| v.partial_cmp(&x).unwrap()) {
        Ok(mut k) => {
            while k + 1 < n && xp[k + 1] <= x {
                k += 1;
            }
            k
        }
        Err(k) => k - 1,
    };
    if j + 1 >= n {
        return fp[n - 1];
    }
    let slope = (fp[j + 1] - fp[j]) / (xp[j + 1] - xp[j]);
    let mut r = slope * (x - xp[j]) + fp[j];
    if r.is_nan() {
        r = slope * (x - xp[j + 1]) + fp[j + 1];
        if r.is_nan() && fp[j] == fp[j + 1] {
            r = fp[j];
        }
    }
    r
}

/// numpy.percentile (linear) of sorted v at q (0..100).
fn np_percentile(v: &[f64], q: f64) -> f64 {
    let n = v.len();
    let idx = q / 100.0 * (n as f64 - 1.0);
    let lo = idx.floor() as usize;
    let hi = (lo + 1).min(n - 1);
    let g = idx - lo as f64;
    // numpy's _lerp: a + (b - a) * t, and b - (b - a) * (1 - t) where t >= 0.5
    let (a, b) = (v[lo], v[hi]);
    let d = b - a;
    if g >= 0.5 { b - d * (1.0 - g) } else { a + d * g }
}

/// roadwidth.measure_row: the right-of-way between the building walls of a street (centreline (k, >=2)), or None.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn measure_row(line: PyReadonlyArray2<f64>, keys: PyReadonlyArray1<i64>, first: PyReadonlyArray1<i64>,
               counts: PyReadonlyArray1<i64>, seg_of: PyReadonlyArray1<i64>, segs: PyReadonlyArray2<f64>,
               bucket: f64, step: f64, end_skip: f64, reach: f64, min_samples: usize, walled: f64, agree: f64)
               -> Option<f64> {
    let l = line.as_array();
    let k = l.nrows();
    if k < 2 {
        return None;
    }
    let xs: Vec<f64> = (0..k).map(|i| l[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| l[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if total < 2.0 * end_skip + step {
        return None;
    }
    // numpy.arange(end_skip, total - end_skip + 1e-9, step)
    let stop = total - end_skip + 1e-9;
    let count = ((stop - end_skip) / step).ceil().max(0.0) as usize;
    let (keys, first, counts, seg_of) = (keys.as_slice().unwrap(), first.as_slice().unwrap(),
                                         counts.as_slice().unwrap(), seg_of.as_slice().unwrap());
    let sg = segs.as_array();
    let mut w = Vec::with_capacity(count);
    let mut n = 0usize;
    for i in 0..count {
        let dd = end_skip + i as f64 * step;
        // searchsorted(s, dd) (left), clipped to 1..k-1
        let mut j = s.partition_point(|&v| v < dd);
        j = j.clamp(1, k - 1);
        let (tx, ty) = (xs[j] - xs[j - 1], ys[j] - ys[j - 1]);
        let len = tx.hypot(ty);
        if !(len >= 1e-6) {
            continue;
        }
        n += 1;
        let (nx, ny) = (-ty / len, tx / len);
        let (cx, cy) = (np_interp(dd, &s, &xs), np_interp(dd, &s, &ys));
        let a = wall_ray(keys, first, counts, seg_of, &sg, bucket, cx, cy, nx, ny, reach);
        let b = wall_ray(keys, first, counts, seg_of, &sg, bucket, cx, cy, -nx, -ny, reach);
        let v = a + b;
        if !v.is_nan() {
            w.push(v);
        }
    }
    if n == 0 || w.len() < min_samples || (w.len() as f64) / (n as f64) < walled {
        return None;
    }
    w.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let m = w.len();
    let med = if m % 2 == 1 { w[m / 2] } else { (w[m / 2 - 1] + w[m / 2]) / 2.0 };
    let (q1, q3) = (np_percentile(&w, 25.0), np_percentile(&w, 75.0));
    if (q3 - q1) / med > agree {
        return None;
    }
    Some(med)
}

/// grading.grade_points: base heights at the points (px, py) graded under the plans' segments a-b (heights za-zb,
/// core half width h): the weights, the inverse-distance blend and the max weight as the numpy code, the (segment,
/// point) pairs in its order (segments, bucket rows, points sorted by bucket).
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn grade_points<'py>(py: Python<'py>, px: PyReadonlyArray1<'py, f64>, pyy: PyReadonlyArray1<'py, f64>,
                     base: PyReadonlyArray1<'py, f64>, a: PyReadonlyArray2<'py, f64>, b: PyReadonlyArray2<'py, f64>,
                     za: PyReadonlyArray1<'py, f64>, zb: PyReadonlyArray1<'py, f64>, h: PyReadonlyArray1<'py, f64>,
                     lift: f64, slope_run: f64, t_min: f64, t_max: f64, blend_r: f64, bucket: f64)
                     -> Bound<'py, PyArray1<f64>> {
    let (px, pyy, base) = (px.as_array(), pyy.as_array(), base.as_array());
    let (a, b, za, zb, h) = (a.as_array(), b.as_array(), za.as_array(), zb.as_array(), h.as_array());
    let n = px.len();
    let mut out: Vec<f64> = base.iter().copied().collect();
    if n == 0 {
        return out.into_pyarray(py);
    }
    let (mut x0, mut y0, mut x1, mut y1) = (f64::INFINITY, f64::INFINITY, f64::NEG_INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        x0 = x0.min(px[i]); x1 = x1.max(px[i]);
        y0 = y0.min(pyy[i]); y1 = y1.max(pyy[i]);
    }
    let bxs: Vec<i64> = (0..n).map(|i| ((px[i] - x0) / bucket).floor() as i64).collect();
    let bys: Vec<i64> = (0..n).map(|i| ((pyy[i] - y0) / bucket).floor() as i64).collect();
    let nbx = bxs.iter().copied().max().unwrap() + 1;
    let keys: Vec<i64> = (0..n).map(|i| bys[i] * nbx + bxs[i]).collect();
    let mut order: Vec<usize> = (0..n).collect();
    order.sort_by_key(|&i| keys[i]);                    // stable, as argsort(kind="stable")
    let sk: Vec<i64> = order.iter().map(|&i| keys[i]).collect();
    let mut num = vec![0.0f64; n];
    let mut den = vec![0.0f64; n];
    let mut wmax = vec![0.0f64; n];
    for s in 0..h.len() {
        let reach = h[s] + t_max;
        let (ax, ay, bx, by) = (a[[s, 0]], a[[s, 1]], b[[s, 0]], b[[s, 1]]);
        let (lo_x, hi_x) = (ax.min(bx) - reach, ax.max(bx) + reach);
        let (lo_y, hi_y) = (ay.min(by) - reach, ay.max(by) + reach);
        if hi_x < x0 || lo_x > x1 || hi_y < y0 || lo_y > y1 {
            continue;
        }
        let cx0 = 0i64.max(((lo_x - x0) / bucket).floor() as i64);
        let cx1 = (nbx - 1).min(((hi_x - x0) / bucket).floor() as i64);
        let cy0 = 0i64.max(((lo_y - y0) / bucket).floor() as i64);
        let cy1 = ((hi_y - y0) / bucket).floor() as i64;
        let (abx, aby) = (bx - ax, by - ay);
        let l2 = abx * abx + aby * aby;
        for row in cy0..=cy1 {
            let i0 = sk.partition_point(|&k| k < row * nbx + cx0);
            let i1 = sk.partition_point(|&k| k <= row * nbx + cx1);
            for j in i0..i1.max(i0) {
                let idx = order[j];
                let (qx, qy) = (px[idx] - ax, pyy[idx] - ay);
                let t = if l2 > 1e-12 { ((qx * abx + qy * aby) / l2).clamp(0.0, 1.0) } else { 0.0 };
                let d = (qx - t * abx).hypot(qy - t * aby);
                let target = za[s] + (zb[s] - za[s]) * t;
                let tt = (slope_run * (target - base[idx]).abs()).clamp(t_min, t_max);
                let u = ((d - h[s]) / tt).clamp(0.0, 1.0);
                let w = 1.0 - u * u * (3.0 - 2.0 * u);
                if w > 0.0 {
                    let r = d / blend_r;
                    let wb = w / (1.0 + r * r);
                    num[idx] += wb * target;
                    den[idx] += wb;
                    if w > wmax[idx] {
                        wmax[idx] = w;
                    }
                }
            }
        }
    }
    for i in 0..n {
        if den[i] > 0.0 {
            out[i] = base[i] + wmax[i] * (num[i] / den[i] - base[i] + lift);
        }
    }
    out.into_pyarray(py)
}

/// grading.resample: points every step m along xy (k, 2), the last point kept, and their arc lengths.
#[pyfunction]
fn resample<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, step: f64)
                 -> (Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>) {
    let p = xy.as_array();
    let k = p.nrows();
    let xs: Vec<f64> = (0..k).map(|i| p[[i, 0]]).collect();
    let ys: Vec<f64> = (0..k).map(|i| p[[i, 1]]).collect();
    let mut s = vec![0.0f64; k];
    for i in 1..k {
        s[i] = s[i - 1] + (xs[i] - xs[i - 1]).hypot(ys[i] - ys[i - 1]);
    }
    let total = s[k - 1];
    if !(total > 0.0) {
        let a = numpy::ndarray::Array2::from_shape_vec((1, 2), vec![xs[0], ys[0]]).unwrap();
        return (a.into_pyarray(py), vec![0.0f64].into_pyarray(py));
    }
    let count = (total / step).ceil() as usize;          // numpy.arange(0, total, step)
    let mut t: Vec<f64> = (0..count).map(|i| i as f64 * step).collect();
    t.push(total);
    let mut out = Vec::with_capacity(t.len() * 2);
    for &v in &t {
        out.push(np_interp(v, &s, &xs));
        out.push(np_interp(v, &s, &ys));
    }
    let a = numpy::ndarray::Array2::from_shape_vec((t.len(), 2), out).unwrap();
    (a.into_pyarray(py), t.into_pyarray(py))
}

/// grading.smooth_profile: Gaussian smoothing of a profile sampled every step m (reflected at the ends, edge-padded
/// when shorter than the kernel's radius).
#[pyfunction]
fn smooth_profile<'py>(py: Python<'py>, z: PyReadonlyArray1<'py, f64>, step: f64, sigma: f64)
                       -> Bound<'py, PyArray1<f64>> {
    let z = z.as_array();
    let n = z.len();
    if n < 3 || sigma <= 0.0 {
        return z.to_vec().into_pyarray(py);
    }
    let r = (3.0 * sigma / step).ceil() as usize;
    let mut k: Vec<f64> = (0..=2 * r).map(|i| {
        let x = (i as f64 - r as f64) * step / sigma;
        (-0.5 * (x * x)).exp()
    }).collect();
    let sum: f64 = k.iter().sum();
    for v in k.iter_mut() {
        *v /= sum;
    }
    let reflect = n > r;
    let at = |i: isize| -> f64 {
        let m = n as isize;
        if i < 0 {
            if reflect { z[(-i) as usize] } else { z[0] }
        } else if i >= m {
            if reflect { z[(2 * (m - 1) - i) as usize] } else { z[n - 1] }
        } else {
            z[i as usize]
        }
    };
    let w = 2 * r + 1;
    let out: Vec<f64> = (0..n).map(|j| {
        // numpy.convolve(pad, k, "valid"): out[j] = sum_i pad[j + i] * k[w - 1 - i]
        let mut acc = 0.0;
        for i in 0..w {
            acc += at(j as isize + i as isize - r as isize) * k[w - 1 - i];
        }
        acc
    }).collect();
    out.into_pyarray(py)
}

/// roads._seg_dist_min: the distance from (px, py) to the polyline line (k, >= 2).
#[pyfunction]
fn seg_dist_min(line: PyReadonlyArray2<f64>, px: f64, py: f64) -> f64 {
    let l = line.as_array();
    let mut best = f64::INFINITY;
    for i in 0..l.nrows().saturating_sub(1) {
        let (ax, ay) = (l[[i, 0]], l[[i, 1]]);
        let (dx, dy) = (l[[i + 1, 0]] - ax, l[[i + 1, 1]] - ay);
        let l2 = (dx * dx + dy * dy).max(1e-12);
        let t = (((px - ax) * dx + (py - ay) * dy) / l2).clamp(0.0, 1.0);
        let d = (ax + dx * t - px).hypot(ay + dy * t - py);
        if d < best {
            best = d;
        }
    }
    best
}

/// polygon.contains_points: which points (m, 2) lie inside the ring (n, 2) (even-odd crossings of a ray toward +x:
/// an edge counts when lo <= y < hi and it crosses y right of the point), with its horizontal bands.
#[pyfunction]
fn contains_points<'py>(py: Python<'py>, ring: PyReadonlyArray2<'py, f64>, pts: PyReadonlyArray2<'py, f64>)
                        -> Bound<'py, PyArray1<bool>> {
    let r = ring.as_array();
    let p = pts.as_array();
    let (n, m) = (r.nrows(), p.nrows());
    let mut out = vec![false; m];
    if n < 3 || m == 0 {
        return out.into_pyarray(py);
    }
    let (mut ex0, mut ey0, mut elo, mut ehi, mut esl) = (vec![], vec![], vec![], vec![], vec![]);
    for i in 0..n {
        let (x0, y0) = (r[[i, 0]], r[[i, 1]]);
        let (x1, y1) = (r[[(i + 1) % n, 0]], r[[(i + 1) % n, 1]]);
        if y0 == y1 {
            continue;
        }
        ex0.push(x0);
        ey0.push(y0);
        elo.push(y0.min(y1));
        ehi.push(y0.max(y1));
        esl.push((x1 - x0) / (y1 - y0));
    }
    let ne = ex0.len();
    if ne == 0 {
        return out.into_pyarray(py);
    }
    let (mut bx0, mut bx1, mut by0, mut by1) = (f64::INFINITY, f64::NEG_INFINITY, f64::INFINITY, f64::NEG_INFINITY);
    for i in 0..n {
        bx0 = bx0.min(r[[i, 0]]); bx1 = bx1.max(r[[i, 0]]);
        by0 = by0.min(r[[i, 1]]); by1 = by1.max(r[[i, 1]]);
    }
    let nb = 512usize.min(1usize.max(ne / 4));
    let mut h = (by1 - by0) / nb as f64;
    if h == 0.0 {
        h = 1.0;
    }
    let clip = |v: f64| -> usize { (v as i64).clamp(0, nb as i64 - 1) as usize };
    let mut bands: Vec<Vec<usize>> = vec![Vec::new(); nb];
    for e in 0..ne {
        let (b0, b1) = (clip((elo[e] - by0) / h), clip((ehi[e] - by0) / h));
        for b in b0..=b1 {
            bands[b].push(e);
        }
    }
    for j in 0..m {
        let (x, y) = (p[[j, 0]], p[[j, 1]]);
        if !(x >= bx0 && x <= bx1 && y >= by0 && y <= by1) {
            continue;
        }
        let b = (((y - by0) / h) as i64).min(nb as i64 - 1) as usize;
        let mut c = 0u32;
        for &e in &bands[b] {
            if y >= elo[e] && y < ehi[e] && ex0[e] + (y - ey0[e]) * esl[e] > x {
                c += 1;
            }
        }
        out[j] = c & 1 == 1;
    }
    out.into_pyarray(py)
}

fn rf_hash3(i: i64, j: i64, k: i64) -> f64 {
    let mut x = (i as u64).wrapping_mul(0x9E3779B97F4A7C15) ^ (j as u64).wrapping_mul(0xC2B2AE3D27D4EB4F);
    x ^= (k as u64).wrapping_mul(0x165667B19E3779F9);
    x ^= x >> 31;
    x = x.wrapping_mul(0xBF58476D1CE4E5B9);
    x ^= x >> 29;
    (x >> 11) as f64 / (1u64 << 53) as f64
}

fn rf_noise3(x: f64, y: f64, z: f64) -> f64 {
    let (xi, yi, zi) = (x.floor(), y.floor(), z.floor());
    let s = |f: f64| f * f * (3.0 - 2.0 * f);
    let (fx, fy, fz) = (s(x - xi), s(y - yi), s(z - zi));
    let (xi, yi, zi) = (xi as i64, yi as i64, zi as i64);
    let mut out = 0.0;
    for dx in 0..2 {
        let wx = if dx == 1 { fx } else { 1.0 - fx };
        for dy in 0..2 {
            let wy = if dy == 1 { fy } else { 1.0 - fy };
            for dz in 0..2 {
                let wz = if dz == 1 { fz } else { 1.0 - fz };
                out = out + wx * wy * wz * rf_hash3(xi + dx, yi + dy, zi + dz);
            }
        }
    }
    out
}

/// rockface.rock: the tile photo rgb (px, px, 3) with its steep texels drawn toward rock, or None when nothing is
/// steep. U: the tile's height grid [j, i], origin (ux0, uy0), spacing usp. steep_a / steep_b: the slopes
/// (rise / run) of STEEP_DEG; octaves (k, 2) (scale m, weight); m_lat / m_lon: metres per degree.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn rock<'py>(py: Python<'py>, rgb: PyReadonlyArray3<'py, u8>, u: PyReadonlyArray2<'py, f64>, x0: f64, y0: f64,
             size: f64, ux0: f64, uy0: f64, usp: f64, lat: f64, lon: f64, m_lat: f64, m_lon: f64, steep_a: f64,
             steep_b: f64, octaves: PyReadonlyArray2<'py, f64>, strata_m: f64, strata_w: f64,
             tint: (f32, f32, f32), desat: f64, darken: f64) -> Option<Bound<'py, numpy::PyArray3<u8>>> {
    let img = rgb.as_array();
    let u = u.as_array();
    let oct = octaves.as_array();
    let px = img.shape()[0];
    let (ny, nx) = (u.nrows(), u.ncols());
    let t = size / px as f64;
    let c: Vec<f64> = (0..px).map(|k| (k as f64 + 0.5) * t).collect();
    let xs: Vec<f64> = c.iter().map(|v| x0 + v).collect();
    let ys: Vec<f64> = c.iter().map(|v| y0 + size - v).collect();
    let fi: Vec<(usize, f64)> = xs.iter().map(|x| {
        let f = ((x - ux0) / usp).clamp(0.0, nx as f64 - 1.000001);
        let i0 = f.floor();
        (i0 as usize, f - i0)
    }).collect();
    let fj: Vec<(usize, f64)> = ys.iter().map(|y| {
        let f = ((y - uy0) / usp).clamp(0.0, ny as f64 - 1.000001);
        let j0 = f.floor();
        (j0 as usize, f - j0)
    }).collect();
    let mut h = vec![0.0f64; px * px];
    for j in 0..px {
        let (j0, wj) = fj[j];
        for i in 0..px {
            let (i0, wi) = fi[i];
            h[j * px + i] = (u[[j0, i0]] * (1.0 - wi) + u[[j0, i0 + 1]] * wi) * (1.0 - wj)
                + (u[[j0 + 1, i0]] * (1.0 - wi) + u[[j0 + 1, i0 + 1]] * wi) * wj;
        }
    }
    // numpy.gradient(H, t): central inside, one-sided at the edges
    let grad = |a: usize, b: usize, n: usize, f: &dyn Fn(usize) -> f64| -> f64 {
        let _ = b;
        if n < 2 {
            0.0
        } else if a == 0 {
            (f(1) - f(0)) / t
        } else if a == n - 1 {
            (f(n - 1) - f(n - 2)) / t
        } else {
            (f(a + 1) - f(a - 1)) / (2.0 * t)
        }
    };
    let mut slope = vec![0.0f64; px * px];
    let mut kk = vec![0.0f64; px * px];
    let mut any = false;
    for j in 0..px {
        for i in 0..px {
            let gx = grad(i, 0, px, &|q| h[j * px + q]);
            let gy = grad(j, 0, px, &|q| h[q * px + i]);
            let sl = gx.hypot(gy);
            let tt = ((sl - steep_a) / (steep_b - steep_a)).clamp(0.0, 1.0);
            let k = tt * tt * (3.0 - 2.0 * tt);
            slope[j * px + i] = sl;
            kk[j * px + i] = k;
            if k > 0.01 {
                any = true;
            }
        }
    }
    if !any {
        return None;
    }
    let wsum: f64 = (0..oct.nrows()).map(|o| oct[[o, 1]]).sum();
    let mut out = img.to_owned();
    let (one_m_desat, desat32, darken32) = ((1.0 - desat) as f32, desat as f32, darken as f32);
    let lw = (0.299f32, 0.587f32, 0.114f32);
    for j in 0..px {
        for i in 0..px {
            let q = j * px + i;
            let k = kk[q];
            if !(k > 0.01) {
                continue;
            }
            let (hs, sl) = (h[q], slope[q]);
            let ex = lon * m_lon + xs[i];
            let nyv = lat * m_lat + ys[j];
            let foot = t * sl.max(1.0);
            let mut n = 0.0f64;
            for o in 0..oct.nrows() {
                let (scale, w) = (oct[[o, 0]], oct[[o, 1]]);
                let a = (1.0 - 1.5 * foot / scale).clamp(0.0, 1.0) * w;
                if a > 0.0 {
                    n += a * (rf_noise3(ex / scale, nyv / scale, hs / scale) - 0.5);
                }
            }
            n /= wsum;
            let sa = (1.0 - 1.5 * foot / strata_m).clamp(0.0, 1.0);
            let strata = sa * (2.0 * std::f64::consts::PI * hs / strata_m + 3.0 * n).sin() * strata_w;
            let f = [img[[j, i, 0]] as f32 / 255.0, img[[j, i, 1]] as f32 / 255.0, img[[j, i, 2]] as f32 / 255.0];
            let lum = f[0] * lw.0 + f[1] * lw.1 + f[2] * lw.2;
            let tints = [tint.0, tint.1, tint.2];
            let mult = 1.0 + 0.55 * n + strata;
            for ch in 0..3 {
                let rockc = (f[ch] * one_m_desat + lum * desat32) * tints[ch] * darken32;
                let rc = rockc as f64 * mult;
                let mixed = f[ch] as f64 * (1.0 - k) + rc * k;
                out[[j, i, ch]] = (mixed * 255.0).round_ties_even().clamp(0.0, 255.0) as u8;
            }
        }
    }
    Some(out.into_pyarray(py))
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
    m.add_function(wrap_pyfunction!(inside_blockers, m)?)?;
    m.add_function(wrap_pyfunction!(densify, m)?)?;
    m.add_function(wrap_pyfunction!(measure_row, m)?)?;
    m.add_function(wrap_pyfunction!(grade_points, m)?)?;
    m.add_function(wrap_pyfunction!(resample, m)?)?;
    m.add_function(wrap_pyfunction!(smooth_profile, m)?)?;
    m.add_function(wrap_pyfunction!(seg_dist_min, m)?)?;
    m.add_function(wrap_pyfunction!(contains_points, m)?)?;
    m.add_function(wrap_pyfunction!(rock, m)?)?;
    m.add("VERSION", 13)?;
    Ok(())
}
