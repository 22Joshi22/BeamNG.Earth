//! Geometry kernels for the world server (server/fast.py wraps them, with the Python versions as the fallback).
//! All coordinates are metres in the anchor's local frame, f64.
use numpy::{PyArray1, PyArray2, PyReadonlyArray1, PyReadonlyArray2, IntoPyArray};
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

#[pymodule]
fn earthfast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(island_ring_rays, m)?)?;
    m.add_function(wrap_pyfunction!(points_inside, m)?)?;
    m.add_function(wrap_pyfunction!(polyline_dist, m)?)?;
    m.add_function(wrap_pyfunction!(clear_of, m)?)?;
    m.add_function(wrap_pyfunction!(dual_side, m)?)?;
    m.add("VERSION", 3)?;
    Ok(())
}
