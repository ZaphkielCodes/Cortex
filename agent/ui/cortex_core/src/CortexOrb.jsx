import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

/* ------------------------------------------------------------------ *
 *  CortexOrb
 *  <CortexOrb status="idle|listening|working|speaking" audioLevel={0..1}
 *             emotion="neutral|happy|excited|sad|angry|love" eye onPoke={fn} />
 *
 *  - status is the only thing you need to drive it (sync it from your cloud state)
 *  - audioLevel (optional, 0..1) makes the orb follow real speech amplitude;
 *    if omitted while speaking, a simulated speech rhythm is used
 *  - npm i three
 * ------------------------------------------------------------------ */

const STATES = {
  idle: {
    energy: 0.28, ring: 0.35, core: 1.0, halo: 0.5, plate: 0.28,
    flow: 0.3, pulse: 0.35, freq: 1.1, breathe: 0.03, chaos: 0, wave: 0.25,
    mote: 0.3, swirl: 0.3, beam: 0.22,
    coreC: "#6366f1", rimC: "#a855f7", deepC: "#0f0c29",
  },
  listening: {
    energy: 0.68, ring: 0.9, core: 1.08, halo: 0.75, plate: 0.55,
    flow: -1, pulse: 0.8, freq: 1.4, breathe: 0.015, chaos: 0, wave: 0,
    mote: -0.35, swirl: 0.2, beam: 0.3,
    coreC: "#06b6d4", rimC: "#3b82f6", deepC: "#061826",
  },
  thinking: {
    energy: 0.72, ring: 1.6, core: 0.96, halo: 0.65, plate: 0.6,
    flow: 1.2, pulse: 1.6, freq: 2.5, breathe: 0.012, chaos: 0.5, wave: 0.1,
    mote: 0.6, swirl: 2.2, beam: 0.3,
    coreC: "#8b5cf6", rimC: "#06b6d4", deepC: "#1e1035",
  },
  speaking: {
    energy: 0.75, ring: 1.0, core: 1.05, halo: 0.8, plate: 0.7,
    flow: 1.0, pulse: 1.2, freq: 1.6, breathe: 0.005, chaos: 0, wave: 1.0,
    mote: 1.1, swirl: 0.4, beam: 0.45,
    coreC: "#ec4899", rimC: "#f43f5e", deepC: "#3b0724",
  },
  acting: {
    energy: 0.85, ring: 1.8, core: 1.1, halo: 0.85, plate: 0.75,
    flow: 1.5, pulse: 1.8, freq: 2.2, breathe: 0.01, chaos: 0.3, wave: 0.4,
    mote: 0.8, swirl: 1.8, beam: 0.5,
    coreC: "#10b981", rimC: "#06b6d4", deepC: "#042f2e",
  },
  waiting: {
    energy: 0.35, ring: 0.4, core: 0.95, halo: 0.55, plate: 0.35,
    flow: 0.2, pulse: 0.5, freq: 0.8, breathe: 0.04, chaos: 0, wave: 0.1,
    mote: 0.2, swirl: 0.2, beam: 0.2,
    coreC: "#f59e0b", rimC: "#fbbf24", deepC: "#382005",
  },
  approval_required: {
    energy: 0.8, ring: 0.8, core: 1.15, halo: 1.1, plate: 0.7,
    flow: 0.5, pulse: 2.0, freq: 2.0, breathe: 0.02, chaos: 0.2, wave: 0.8,
    mote: 0.4, swirl: 0.5, beam: 0.6,
    coreC: "#f97316", rimC: "#ef4444", deepC: "#450a0a",
  },
  error: {
    energy: 0.82, ring: 1.2, core: 0.9, halo: 0.9, plate: 0.6,
    flow: -0.5, pulse: 2.4, freq: 3.0, breathe: 0.01, chaos: 0.85, wave: 0.3,
    mote: 0.7, swirl: -1.5, beam: 0.4,
    coreC: "#ef4444", rimC: "#dc2626", deepC: "#3f0a0a",
  },
  offline: {
    energy: 0.08, ring: 0.1, core: 0.8, halo: 0.15, plate: 0.08,
    flow: 0.05, pulse: 0.1, freq: 0.4, breathe: 0.01, chaos: 0, wave: 0,
    mote: 0.05, swirl: 0.05, beam: 0.05,
    coreC: "#475569", rimC: "#64748b", deepC: "#0f172a",
  },
};
const ALIASES = {
  working: "acting",
  "approval required": "approval_required",
  approval: "approval_required",
  needs_approval: "approval_required",
  pending_approval: "approval_required",
  failed: "error",
  disconnected: "offline",
};

// Emotions: bounce size (amp), bounce speed (freq), sway, ring speed (spd), eyelid droop (lid),
// pupil size offset, resting sag (droop), tremble (jitter) + a colour palette (neutral = use the state's colours)
const EM_KEYS = ["amp", "freq", "sway", "spd", "lid", "pupil", "droop", "jitter"];
const EMOTIONS = {
  neutral: { amp: 0.10, freq: 6, sway: 0.02, spd: 1.0, lid: 0.0, pupil: 0.0, droop: 0, jitter: 0 },
  happy: { amp: 0.22, freq: 9, sway: 0.06, spd: 1.2, lid: 0.12, pupil: 0.06, droop: 0.04, jitter: 0, coreC: "#ffb84d", rimC: "#ff6fa8", deepC: "#4a1a3a" },
  excited: { amp: 0.30, freq: 13, sway: 0.10, spd: 1.7, lid: 0.0, pupil: 0.12, droop: 0.05, jitter: 0.004, coreC: "#ff4fd8", rimC: "#ffd24d", deepC: "#4a0f5c" },
  sad: { amp: 0.05, freq: 2.5, sway: 0.0, spd: 0.5, lid: 0.45, pupil: 0.1, droop: -0.14, jitter: 0, coreC: "#3f6fd0", rimC: "#7d9bd6", deepC: "#0a1640" },
  angry: { amp: 0.12, freq: 17, sway: 0.0, spd: 1.5, lid: 0.35, pupil: -0.14, droop: 0, jitter: 0.02, coreC: "#ff3b3b", rimC: "#ff8a2a", deepC: "#4a0a0a" },
  love: { amp: 0.12, freq: 4, sway: 0.14, spd: 0.8, lid: 0.25, pupil: 0.14, droop: 0.02, jitter: 0, coreC: "#ff5fa2", rimC: "#c26bff", deepC: "#4a0f3a" },
};
const NUM_KEYS = ["energy", "ring", "core", "halo", "plate", "flow", "pulse", "freq", "breathe", "chaos", "wave", "mote", "swirl", "beam"];

// Orbits: 'loop' = comet ring with bead(s), 'band' = dashed glass band. tilt = lean, axis = precession axis
const RINGS = [
  { type: "loop", r: 1.32, tube: 0.022, tilt: [0.0, 0.0], axis: "y", speed: 0.55, spin: 0.7, hue: 0.0, beads: 1, bead: 0.05 },
  { type: "band", r: 1.55, half: 0.10, tilt: [0.95, 0.5], axis: "y", speed: -0.38, spin: 1.1, hue: 0.35, beads: 0, dash: 36 },
  { type: "loop", r: 1.86, tube: 0.016, tilt: [0.35, -0.9], axis: "x", speed: 0.28, spin: 0.5, hue: 1.0, beads: 1, bead: 0.09 },
];

const simSpeech = (t) => {
  const phrase = Math.min(1, Math.max(0, 0.5 + Math.sin(t * 1.1) * 1.2));
  const syl = 0.5 + 0.5 * Math.sin(t * 9.5 + Math.sin(t * 3.1) * 2);
  return phrase * (0.2 + 0.8 * syl * (0.65 + 0.35 * Math.sin(t * 4.3)));
};

const PLATE_R = 2.7;
const MIN_W = 0.012; // thinnest nerve fibre
const PLATE_Y = -1.95;

/* ---------- helpers ---------- */

function rng(seed) {
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Nerve-like network: wavy fibres that taper as they branch, built as ribbon meshes
function buildNerves(R, seed = 11) {
  const rand = rng(seed);
  const pos = [], dist = [], rnd = [], side = [], idx = [], nodes = [];
  let vc = 0;

  const ribbon = (pts, ds, ws, r) => {
    const n = pts.length;
    for (let i = 0; i < n; i++) {
      const a = pts[Math.max(i - 1, 0)], b = pts[Math.min(i + 1, n - 1)];
      let tx = b[0] - a[0], tz = b[1] - a[1];
      const l = Math.hypot(tx, tz) || 1;
      tx /= l; tz /= l;
      const nx = -tz, nz = tx, w = ws[i] / 2;
      pos.push(pts[i][0] + nx * w, 0, pts[i][1] + nz * w, pts[i][0] - nx * w, 0, pts[i][1] - nz * w);
      dist.push(ds[i], ds[i]);
      rnd.push(r, r);
      side.push(1, -1);
      if (i < n - 1) {
        const k = vc + i * 2;
        idx.push(k, k + 1, k + 2, k + 1, k + 3, k + 2);
      }
    }
    vc += n * 2;
  };

  const grow = (x, z, ang, len, depth, d0, w0, r) => {
    const steps = 6;
    const pts = [[x, z]], ds = [d0], ws = [Math.max(w0, MIN_W)];
    const spawn = [];
    let cx = x, cz = z, a = ang, d = d0;
    for (let i = 1; i <= steps; i++) {
      a += (rand() - 0.5) * 0.7; // wobble
      const step = len / steps;
      const nx = cx + Math.cos(a) * step, nz = cz + Math.sin(a) * step;
      if (Math.hypot(nx, nz) > R) break;
      d += step; cx = nx; cz = nz;
      pts.push([cx, cz]); ds.push(d);
      ws.push(Math.max(w0 * (1 - 0.45 * (i / steps)), MIN_W)); // taper
      if (depth > 1 && i > 1 && i < steps && rand() < 0.3) spawn.push([cx, cz, a, d, ws[i]]);
    }
    if (pts.length < 2) return;
    ribbon(pts, ds, ws, r);
    if (depth <= 0 || pts.length <= steps) { nodes.push(cx, 0, cz); return; } // terminal bulb

    // side branches sprouting mid-fibre
    spawn.forEach(([sx, sz, sa, sd, sw]) =>
      grow(sx, sz, sa + (rand() < 0.5 ? -1 : 1) * (0.5 + rand() * 0.6), len * 0.55, depth - 2, sd, sw * 0.75, rand()));
    // fork at the tip
    const ew = ws[ws.length - 1];
    const n = rand() < 0.8 ? 2 : 1;
    for (let k = 0; k < n; k++)
      grow(cx, cz, a + (k === 0 ? -1 : 1) * (0.3 + rand() * 0.5), len * (0.72 + rand() * 0.12), depth - 1, d, ew * 0.85, rand());
  };

  const P = 9;
  for (let i = 0; i < P; i++) grow(0, 0, (i / P) * Math.PI * 2 + rand() * 0.4, R * 0.34, 4, 0, 0.09, rand());
  return {
    pos: new Float32Array(pos), dist: new Float32Array(dist), rnd: new Float32Array(rnd),
    side: new Float32Array(side), idx: new Uint32Array(idx), nodes: new Float32Array(nodes),
  };
}

function makeGlowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 256;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(128, 128, 0, 128, 128, 128);
  grad.addColorStop(0, "rgba(255,255,255,1)");
  grad.addColorStop(0.25, "rgba(255,255,255,0.45)");
  grad.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 256, 256);
  return new THREE.CanvasTexture(c);
}

/* ---------- shaders ---------- */

const SHELL_VERT = /* glsl */ `
uniform float uTime; uniform float uEnergy;
varying vec3 vN; varying vec3 vV; varying vec3 vP;
void main(){
  vP = position;
  float d = sin(position.x*4.0+uTime*1.3)*sin(position.y*4.0-uTime*1.1)*sin(position.z*4.0+uTime*0.9);
  vec3 p = position + normal * d * (0.015 + 0.035*uEnergy);
  vec4 mv = modelViewMatrix * vec4(p,1.0);
  vN = normalize(normalMatrix * normal);
  vV = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;

const SHELL_FRAG = /* glsl */ `
uniform float uTime; uniform float uEnergy;
uniform vec3 uCore; uniform vec3 uRim; uniform vec3 uDeep; uniform vec3 uAccent;
varying vec3 vN; varying vec3 vV; varying vec3 vP;
void main(){
  float f = 1.0 - max(dot(normalize(vN), normalize(vV)), 0.0);
  float rim = pow(f, 2.2);
  float sw = sin(vP.x*5.0 + uTime*0.7 + sin(vP.y*4.0 - uTime*0.5)*1.5)
           * sin(vP.z*5.0 - uTime*0.6 + sin(vP.x*3.0 + uTime*0.4)*1.5);
  float swirl = 0.5 + 0.5*sw;
  float deep = pow(1.0 - f, 3.0);
  float band = 0.5 + 0.5*sin(vP.y*3.0 + uTime*0.5 + swirl*2.5);
  vec3 tint = mix(mix(uCore, uRim, band), uAccent, 0.22*swirl);
  float fil = pow(abs(sin(vP.x*9.0 + sin(vP.z*6.0 + uTime*0.9)*2.0 + uTime*0.6)), 14.0) * (0.3 + deep);
  vec3 col = mix(uRim, uAccent, 0.35)*rim*1.7
           + uDeep*(1.0 - rim)*0.9
           + tint*deep*(0.5 + 0.9*swirl)*(0.7 + uEnergy)*0.7
           + mix(tint, vec3(1.0), 0.25)*fil*(0.35 + 0.5*uEnergy);
  float lat = abs(fract(vP.y*5.0) - 0.5);
  float lon = abs(fract(atan(vP.z, vP.x)*3.8197) - 0.5);
  float grid = max(smoothstep(0.46, 0.5, lat), smoothstep(0.46, 0.5, lon));
  col += mix(uRim, uAccent, 0.5) * grid * (0.1 + 0.3*f) * (0.5 + uEnergy);
  float a = clamp(0.1 + rim*0.9 + deep*0.55*(0.5 + swirl*0.5) + fil*0.4 + grid*0.08, 0.0, 1.0);
  gl_FragColor = vec4(col, a);
}`;

const LINE_VERT = /* glsl */ `
attribute float aDist; attribute float aRnd; attribute float aSide;
varying float vDist; varying float vRnd; varying float vR; varying float vSide;
void main(){
  vDist = aDist; vRnd = aRnd; vSide = aSide; vR = length(position.xz);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0);
}`;

const LINE_FRAG = /* glsl */ `
uniform float uPhase; uniform float uEnergy; uniform float uFreq; uniform float uR;
uniform vec3 uBase; uniform vec3 uEdge; uniform vec3 uPulse;
varying float vDist; varying float vRnd; varying float vR; varying float vSide;
void main(){
  float w = fract(vDist*uFreq - uPhase + vRnd);
  float p = pow(w, 7.0);
  float fade = 1.0 - smoothstep(0.55*uR, uR, vR);
  float soft = 1.0 - smoothstep(0.35, 1.0, abs(vSide));
  float inten = (0.42 + uEnergy*1.8*p) * (0.4 + 0.6*fade) * soft;
  vec3 base = mix(uBase, uEdge, smoothstep(0.0, uR, vR));
  vec3 col = mix(base, uPulse, clamp(p*uEnergy*1.5, 0.0, 1.0));
  gl_FragColor = vec4(col*inten, 1.0);
}`;

const UV_VERT = /* glsl */ `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`;

const NEBULA_FRAG = /* glsl */ `
uniform float uTime; uniform float uEnergy; uniform vec3 uA; uniform vec3 uB; uniform vec3 uC;
varying vec2 vUv;
float h(vec2 p){ return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
float n(vec2 p){
  vec2 i = floor(p), f = fract(p); f = f*f*(3.0 - 2.0*f);
  return mix(mix(h(i), h(i + vec2(1.0, 0.0)), f.x), mix(h(i + vec2(0.0, 1.0)), h(i + vec2(1.0, 1.0)), f.x), f.y);
}
float fbm(vec2 p){
  float v = 0.0, a = 0.5;
  for (int i = 0; i < 4; i++) { v += a*n(p); p = p*2.03 + vec2(1.7, 9.2); a *= 0.5; }
  return v;
}
void main(){
  vec2 p = vUv * vec2(3.0, 2.0);
  float t = uTime * 0.02;
  float a = fbm(p + vec2(t, -t*0.6));
  float b = fbm(p*1.7 - vec2(t*1.3, t) + a*1.5);
  vec3 col = mix(uA, uB, smoothstep(0.3, 0.8, a)) + uC * smoothstep(0.55, 0.9, b) * 0.6;
  float vig = 1.0 - smoothstep(0.25, 0.75, length(vUv - vec2(0.5, 0.45)));
  float alpha = (0.2 + 0.12*uEnergy) * (0.5 + b) * (0.4 + vig);
  gl_FragColor = vec4(col*alpha, alpha);
}`;

const FLOOR_VERT = /* glsl */ `
varying vec2 vW;
void main(){ vW = position.xy; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`;

const FLOOR_FRAG = /* glsl */ `
uniform vec3 uColor; uniform float uGlow; uniform float uRip;
varying vec2 vW;
void main(){
  float r = length(vW);
  vec2 g = abs(fract(vW*0.7) - 0.5);
  float line = max(smoothstep(0.47, 0.5, g.x), smoothstep(0.47, 0.5, g.y));
  float fade = exp(-r*0.22) * (1.0 - smoothstep(9.0, 14.0, r)) * smoothstep(2.6, 3.4, r);
  float p = exp(-pow((r - uRip*9.0)*0.8, 2.0)) * (1.0 - uRip);
  float a = line * fade * (0.16 + uGlow*0.25 + p);
  gl_FragColor = vec4(uColor*a*1.4, a);
}`;

const BEAM_VERT = /* glsl */ `
varying vec3 vN; varying vec3 vV; varying vec2 vUv;
void main(){
  vUv = uv;
  vec4 mv = modelViewMatrix * vec4(position,1.0);
  vN = normalize(normalMatrix * normal);
  vV = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;

const BEAM_FRAG = /* glsl */ `
uniform float uTime; uniform vec3 uColor; uniform float uBeam;
varying vec3 vN; varying vec3 vV; varying vec2 vUv;
void main(){
  float edge = pow(abs(dot(normalize(vN), normalize(vV))), 1.5);
  float rays = 0.55 + 0.45*sin(vUv.x*56.5 + uTime*0.4)*sin(vUv.x*31.4 - uTime*0.25);
  float a = pow(1.0 - vUv.y, 1.7) * edge * rays * uBeam;
  gl_FragColor = vec4(uColor*a, a);
}`;

const MOTE_VERT = /* glsl */ `
attribute vec4 aSeed;
uniform float uT; uniform float uSwirl; uniform float uSize; uniform float uPx;
varying float vA; varying float vMix;
void main(){
  float u = fract(aSeed.z + uT*aSeed.w);
  float rad = (1.2 + aSeed.x*2.2) * (1.0 - 0.3*u);
  float ang = aSeed.y + uSwirl*u*2.0 + uT*0.1*aSeed.w*uSwirl;
  vec3 p = vec3(cos(ang)*rad, -1.9 + u*5.0, sin(ang)*rad);
  p.x += sin(uT*0.7 + aSeed.y*5.0)*0.08;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uSize * uPx / -mv.z * (0.6 + 0.8*fract(aSeed.y*9.0));
  vA = smoothstep(0.0, 0.12, u) * (1.0 - smoothstep(0.65, 1.0, u));
  vMix = fract(aSeed.x*17.0);
}`;

const MOTE_FRAG = /* glsl */ `
uniform vec3 uCore; uniform vec3 uRim; uniform float uAlpha;
varying float vA; varying float vMix;
void main(){
  float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
  vec3 col = mix(mix(uCore, uRim, vMix), vec3(1.0), 0.2);
  gl_FragColor = vec4(col*a, a*vA*uAlpha);
}`;

const CORE_VERT = /* glsl */ `
varying vec3 vU; varying vec3 vN; varying vec3 vV;
void main(){
  vU = normalize(position);
  vec4 mv = modelViewMatrix * vec4(position,1.0);
  vN = normalize(normalMatrix * normal);
  vV = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;

// Core: oscilloscope-style waveform lines (speaking) + spinning plasma streaks (working)
const CORE_FRAG = /* glsl */ `
uniform float uTime; uniform float uAmp; uniform float uWave; uniform float uChaos; uniform float uVis;
uniform vec3 uCore; uniform vec3 uRim;
varying vec3 vU; varying vec3 vN; varying vec3 vV;
void main(){
  float f = 1.0 - max(dot(normalize(vN), normalize(vV)), 0.0);
  float lines = 0.0;
  for (int k = 0; k < 4; k++) {
    float fk = float(k);
    float wy = (fk - 1.5) * 0.34 + sin(vU.x*(4.0 + fk*2.5) + uTime*(2.2 + fk*0.7) + fk) * (0.04 + uAmp*0.30);
    lines += exp(-pow((vU.y - wy) * (14.0 + uAmp*10.0), 2.0));
  }
  float ang = atan(vU.y, vU.x);
  float streak = pow(abs(sin(ang*5.0 + uTime*6.0 + length(vU.xy)*9.0)), 6.0) * (1.0 - 0.5*f);
  float plasma = 0.5 + 0.5*sin(vU.x*7.0 + uTime*3.0)*sin(vU.y*7.0 - uTime*2.4)*sin(vU.z*7.0 + uTime*2.0);
  vec3 hot = mix(uCore, vec3(1.0), 0.2);
  vec3 col = hot*(0.55 + 0.45*(1.0 - f))*0.75 + uRim*f*0.8;
  col += mix(hot, uRim, 0.3) * lines * uWave * 1.6;
  col += mix(uRim, vec3(1.0), 0.1) * streak * uChaos * 1.3;
  col += uCore * plasma * uChaos * 0.5;
  float a = clamp(0.55 + lines*uWave*0.5 + streak*uChaos*0.4 + f*0.3, 0.0, 1.0);
  gl_FragColor = vec4(col, a*uVis);
}`;

// Eyeball: glowing iris with radial fibres, dilating pupil, highlight (opaque so the pupil is truly dark)
const EYE_FRAG = /* glsl */ `
uniform float uTime; uniform float uPupil;
uniform vec3 uIrisA; uniform vec3 uIrisB; uniform vec3 uSclera;
varying vec3 vU; varying vec3 vN; varying vec3 vV;
void main(){
  float th = acos(clamp(vU.z, -1.0, 1.0));
  float ang = atan(vU.y, vU.x);
  float irisR = 0.62;
  float pupilR = irisR * uPupil;
  float f = 1.0 - max(dot(normalize(vN), normalize(vV)), 0.0);
  vec3 col = uSclera*(0.5 + 0.5*(1.0 - f)) + mix(uIrisB, vec3(1.0), 0.2)*pow(f, 3.0)*0.5;
  if (th < irisR) {
    float r = th/irisR;
    float rays = 0.65 + 0.35*sin(ang*26.0 + sin(ang*7.0 + uTime*0.6)*1.5);
    vec3 iris = mix(uIrisA, uIrisB, smoothstep(0.15, 0.95, r)) * rays * (1.25 - 0.5*r);
    iris *= 1.0 - 0.7*smoothstep(0.82, 1.0, r);
    iris += uIrisA * 0.35 * exp(-pow((r - 0.45)*9.0, 2.0));
    col = mix(vec3(0.01, 0.0, 0.03), iris, smoothstep(pupilR*0.92, pupilR*1.12, th));
  }
  col += vec3(1.0) * smoothstep(0.17, 0.0, length(vU.xy - vec2(-0.2, 0.22))) * step(0.0, vU.z) * 0.9;
  gl_FragColor = vec4(col, 1.0);
}`;

const SWARM_VERT = /* glsl */ `
attribute vec4 aSeed;
uniform float uT; uniform float uPhase; uniform float uSpread; uniform float uPull; uniform float uSize; uniform float uPx; uniform float uBurst;
varying float vMix;
void main(){
  float th = aSeed.y + uT*aSeed.w;
  float ph = aSeed.z + sin(uT*0.4*aSeed.w + aSeed.y)*0.25;
  float r = aSeed.x + uSpread*0.14*sin(uPhase*3.0 - aSeed.x*8.0);
  r *= 1.0 - uPull*0.25*(0.5 + 0.5*sin(uT*3.0 + aSeed.y*5.0));
  r *= 1.0 + uBurst*0.9*(0.4 + 0.6*fract(aSeed.y*13.0));
  vec3 p = r * vec3(sin(ph)*cos(th), cos(ph), sin(ph)*sin(th));
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * mv;
  gl_PointSize = uSize * uPx / -mv.z;
  vMix = fract(aSeed.y*7.0);
}`;

const SWARM_FRAG = /* glsl */ `
uniform vec3 uCore; uniform vec3 uRim; uniform float uAlpha;
varying float vMix;
void main(){
  float a = smoothstep(0.5, 0.0, length(gl_PointCoord - 0.5));
  vec3 col = mix(mix(uCore, uRim, vMix), vec3(1.0), 0.3);
  gl_FragColor = vec4(col*a, a*uAlpha);
}`;

const SHOCK_VERT = /* glsl */ `
varying vec3 vN; varying vec3 vV;
void main(){
  vec4 mv = modelViewMatrix * vec4(position,1.0);
  vN = normalize(normalMatrix * normal);
  vV = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;

const SHOCK_FRAG = /* glsl */ `
uniform float uAlpha; uniform vec3 uColor;
varying vec3 vN; varying vec3 vV;
void main(){
  float f = 1.0 - max(dot(normalize(vN), normalize(vV)), 0.0);
  float a = (0.25 + pow(f, 1.6)) * uAlpha;
  gl_FragColor = vec4(uColor*a*1.5, a);
}`;

const RING_VERT = /* glsl */ `
varying float vA; varying float vR; varying vec3 vN; varying vec3 vV;
void main(){
  vA = atan(position.y, position.x);
  vR = length(position.xy);
  vN = normalize(normalMatrix * normal);
  vV = normalize(-(modelViewMatrix * vec4(position,1.0)).xyz);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0);
}`;

// Neon-tube loops: bright centre line, a comet tail behind each bead, energy pulses running around
const RING_FRAG = /* glsl */ `
uniform vec3 uA; uniform vec3 uB; uniform vec3 uC;
uniform float uHue; uniform float uDir; uniform float uCount; uniform float uTail; uniform float uTime;
varying float vA; varying float vR; varying vec3 vN; varying vec3 vV;
void main(){
  float s = 0.5 + 0.5*sin(vA + uHue*6.2831);
  float t = 0.5 + 0.5*sin(vA*2.0 - uHue*4.0);
  vec3 col = mix(mix(uA, uB, s), uC, t*0.6);
  float tail = 0.0;
  for (int k = 0; k < 3; k++) {
    if (float(k) < uCount) {
      float d = mod((float(k)*6.2831/uCount - vA)*uDir, 6.2831);
      tail = max(tail, exp(-d*1.4));
    }
  }
  float neon = 0.45 + pow(max(dot(normalize(vN), normalize(vV)), 0.0), 2.0);
  float pulse = pow(0.5 + 0.5*sin(vA*3.0 - uTime*2.2*uDir + uHue*6.0), 10.0);
  col = col*mix(1.0, 0.35, uTail) + mix(uC, vec3(1.0), 0.5)*tail*uTail*1.6;
  col += mix(uB, vec3(1.0), 0.3)*pulse*0.9;
  gl_FragColor = vec4(col*1.25*neon, 0.95);
}`;

// Data band: blocks of "code" that flicker (faster while working/speaking) between glowing edges
const BAND_FRAG = /* glsl */ `
uniform vec3 uA; uniform vec3 uB; uniform vec3 uC;
uniform float uHue; uniform float uMid; uniform float uHalf; uniform float uDash; uniform float uTime; uniform float uActive;
varying float vA; varying float vR;
float hash(float n){ return fract(sin(n*127.1)*43758.5453); }
void main(){
  float e = (vR - uMid)/uHalf;
  float ae = abs(e);
  float edge = smoothstep(0.8, 1.0, ae);
  float u = (vA + 3.14159)/6.28318;
  float seg = floor(u*uDash);
  float on = step(0.42, hash(seg + uHue*10.0));
  float flick = step(0.5, hash(seg*3.7 + floor(uTime*(2.0 + uActive*6.0))));
  float inner = step(ae, 0.55) * on * (0.25 + 0.55*flick);
  float ticks = step(0.5, fract(u*uDash*4.0)) * smoothstep(0.55, 0.7, ae) * (1.0 - edge) * 0.5;
  float s = 0.5 + 0.5*sin(vA + uHue*6.2831);
  vec3 col = mix(mix(uA, uB, s), uC, 0.35);
  float a = edge*0.9 + inner + ticks + 0.03;
  gl_FragColor = vec4(col*1.3, clamp(a, 0.0, 1.0));
}`;

const PLATE_VERT = /* glsl */ `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }`;

const PLATE_FRAG = /* glsl */ `
uniform vec3 uColor; uniform float uGlow; uniform float uRip; uniform vec2 uRipC;
varying vec2 vUv;
void main(){
  float r = length(vUv - 0.5) * 2.0;
  float body  = pow(max(1.0 - r, 0.0), 2.0) * 0.30 * (0.4 + uGlow);
  float rim   = smoothstep(0.93, 0.985, r) * (1.0 - smoothstep(0.985, 1.0, r)) * (0.5 + uGlow);
  float rings = (0.5 + 0.5*sin(r*60.0)) * 0.04 * (1.0 - r);
  float rr    = length(vUv - uRipC) * 2.0;
  float rip   = exp(-pow((rr - uRip*1.05)*10.0, 2.0)) * (1.0 - uRip) * 1.2;
  float a = body + rim + rings + rip;
  gl_FragColor = vec4(uColor * a, clamp(a, 0.0, 1.0));
}`;

/* ---------- component ---------- */

export function CortexOrb({ status = "idle", audioLevel, emotion = "neutral", eye = true, onPoke, className, style }) {
  const mountRef = useRef(null);
  const statusRef = useRef("idle");
  const audioRef = useRef(audioLevel);
  const rippleRef = useRef(1);
  const onPokeRef = useRef(onPoke);
  const emoRef = useRef(emotion);
  const eyeRef = useRef(eye);
  const ripCRef = useRef(new THREE.Vector2(0.5, 0.5));

  audioRef.current = audioLevel;
  onPokeRef.current = onPoke;
  emoRef.current = emotion;
  eyeRef.current = eye;

  // Restart the plate ripple whenever the state changes
  useEffect(() => {
    statusRef.current = ALIASES[status] || status;
    rippleRef.current = 0;
    ripCRef.current.set(0.5, 0.5);
  }, [status]);

  // a ripple whenever the mood changes
  useEffect(() => {
    rippleRef.current = 0;
    ripCRef.current.set(0.5, 0.5);
  }, [emotion]);

  useEffect(() => {
    const mount = mountRef.current;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setClearColor(0x000000, 0);
    mount.appendChild(renderer.domElement);
    renderer.domElement.style.display = "block";

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
    camera.position.set(0, 1.5, 7.4);
    camera.lookAt(0, -0.25, 0);

    const glowTex = makeGlowTexture();
    const additive = { transparent: true, depthWrite: false, blending: THREE.AdditiveBlending };

    // Stars
    const rand = rng(42);
    const starPos = new Float32Array(600 * 3);
    for (let i = 0; i < 600; i++) {
      const r = 12 + rand() * 18, th = rand() * Math.PI * 2, ph = Math.acos(2 * rand() - 1);
      starPos.set([r * Math.sin(ph) * Math.cos(th), r * Math.cos(ph), r * Math.sin(ph) * Math.sin(th)], i * 3);
    }
    const starGeo = new THREE.BufferGeometry();
    starGeo.setAttribute("position", new THREE.BufferAttribute(starPos, 3));
    const stars = new THREE.Points(
      starGeo,
      new THREE.PointsMaterial({ color: 0x9fa8ff, size: 0.09, map: glowTex, opacity: 0.7, ...additive })
    );
    scene.add(stars);

    const world = new THREE.Group();
    scene.add(world);

    // Environment: nebula backdrop, glowing floor grid, light beam, rising motes
    const col3 = () => ({ value: new THREE.Color() });
    const nebMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: { uTime: { value: 0 }, uEnergy: { value: 0.3 }, uA: col3(), uB: col3(), uC: col3() },
      vertexShader: UV_VERT, fragmentShader: NEBULA_FRAG,
    });
    const nebula = new THREE.Mesh(new THREE.PlaneGeometry(64, 38), nebMat);
    nebula.position.set(0, 1, -18);
    scene.add(nebula);

    const floorMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: { uColor: col3(), uGlow: { value: 0.3 }, uRip: { value: 1 } },
      vertexShader: FLOOR_VERT, fragmentShader: FLOOR_FRAG,
    });
    const floor = new THREE.Mesh(new THREE.PlaneGeometry(40, 40), floorMat);
    floor.rotation.x = -Math.PI / 2;
    floor.position.y = PLATE_Y - 0.03;
    world.add(floor);

    const beamMat = new THREE.ShaderMaterial({
      ...additive,
      side: THREE.DoubleSide,
      uniforms: { uTime: { value: 0 }, uColor: col3(), uBeam: { value: 0.2 } },
      vertexShader: BEAM_VERT, fragmentShader: BEAM_FRAG,
    });
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.85, 2.4, 5.4, 64, 1, true), beamMat);
    beam.position.y = PLATE_Y + 2.7;
    world.add(beam);

    const MN = 320;
    const mseeds = new Float32Array(MN * 4);
    for (let i = 0; i < MN; i++) mseeds.set([rand(), rand() * Math.PI * 2, rand(), 0.25 + rand() * 0.5], i * 4);
    const moteGeo = new THREE.BufferGeometry();
    moteGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(MN * 3), 3));
    moteGeo.setAttribute("aSeed", new THREE.BufferAttribute(mseeds, 4));
    const moteMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: {
        uT: { value: 0 }, uSwirl: { value: 0.3 }, uSize: { value: 0.06 }, uPx: { value: 900 }, uAlpha: { value: 0.7 },
        uCore: col3(), uRim: col3(),
      },
      vertexShader: MOTE_VERT, fragmentShader: MOTE_FRAG,
    });
    const motes = new THREE.Points(moteGeo, moteMat);
    motes.frustumCulled = false;
    world.add(motes);

    // Orb
    const orb = new THREE.Group();
    orb.position.y = 0.15;
    world.add(orb);

    const shellMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: {
        uTime: { value: 0 }, uEnergy: { value: 0.2 },
        uCore: { value: new THREE.Color() }, uRim: { value: new THREE.Color() },
        uDeep: { value: new THREE.Color() }, uAccent: { value: new THREE.Color("#3ad4ff") },
      },
      vertexShader: SHELL_VERT, fragmentShader: SHELL_FRAG,
    });
    orb.add(new THREE.Mesh(new THREE.SphereGeometry(1, 64, 64), shellMat));

    const heartMat = new THREE.SpriteMaterial({ map: glowTex, color: 0xffffff, ...additive, opacity: 0.5 });
    const heart = new THREE.Sprite(heartMat);
    heart.scale.set(1.5, 1.5, 1);
    orb.add(heart);

    const coreMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: {
        uTime: { value: 0 }, uAmp: { value: 0 }, uWave: { value: 0 }, uChaos: { value: 0 }, uVis: { value: 1 },
        uCore: { value: new THREE.Color() }, uRim: { value: new THREE.Color() },
      },
      vertexShader: CORE_VERT, fragmentShader: CORE_FRAG,
    });
    const coreMesh = new THREE.Mesh(new THREE.SphereGeometry(0.44, 48, 48), coreMat);
    orb.add(coreMesh);

    const eyeMat = new THREE.ShaderMaterial({
      uniforms: {
        uTime: { value: 0 }, uPupil: { value: 0.46 },
        uIrisA: { value: new THREE.Color() }, uIrisB: { value: new THREE.Color() }, uSclera: { value: new THREE.Color() },
      },
      vertexShader: CORE_VERT, fragmentShader: EYE_FRAG,
    });
    const eyeMesh = new THREE.Mesh(new THREE.SphereGeometry(0.34, 48, 48), eyeMat);
    orb.add(eyeMesh);

    const haloMat = new THREE.SpriteMaterial({ map: glowTex, color: 0x6a5cff, ...additive, opacity: 0.5 });
    const halo = new THREE.Sprite(haloMat);
    halo.scale.set(5.2, 5.2, 1);
    orb.add(halo);

    // Particle swarm orbiting inside the shell
    const N = 650;
    const seeds = new Float32Array(N * 4);
    for (let i = 0; i < N; i++) {
      seeds.set([0.5 + Math.pow(rand(), 0.7) * 0.47, rand() * Math.PI * 2, Math.acos(2 * rand() - 1),
        (0.3 + rand() * 0.9) * (rand() < 0.5 ? -1 : 1)], i * 4);
    }
    const swarmGeo = new THREE.BufferGeometry();
    swarmGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    swarmGeo.setAttribute("aSeed", new THREE.BufferAttribute(seeds, 4));
    const pMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: {
        uT: { value: 0 }, uPhase: { value: 0 }, uSpread: { value: 0 }, uPull: { value: 0 }, uBurst: { value: 0 },
        uSize: { value: 0.045 }, uPx: { value: 900 }, uAlpha: { value: 0.5 },
        uCore: { value: new THREE.Color() }, uRim: { value: new THREE.Color() },
      },
      vertexShader: SWARM_VERT, fragmentShader: SWARM_FRAG,
    });
    const swarm = new THREE.Points(swarmGeo, pMat);
    swarm.frustumCulled = false;
    orb.add(swarm);

    // Wireframe lattice inside the shell
    const latMat = new THREE.LineBasicMaterial({ color: 0x8b7bff, ...additive, opacity: 0.2 });
    const lattice = new THREE.LineSegments(new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(0.82, 1)), latMat);
    orb.add(lattice);

    // HUD tick ring around the equator
    const tickPts = [];
    for (let i = 0; i < 96; i++) {
      const a = (i / 96) * Math.PI * 2, r1 = i % 8 === 0 ? 2.16 : 2.09;
      tickPts.push(Math.cos(a) * 2.02, 0, Math.sin(a) * 2.02, Math.cos(a) * r1, 0, Math.sin(a) * r1);
    }
    const tickGeo = new THREE.BufferGeometry();
    tickGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(tickPts), 3));
    const tickMat = new THREE.LineBasicMaterial({ color: 0x8b7bff, ...additive, opacity: 0.4 });
    const ticks = new THREE.LineSegments(tickGeo, tickMat);
    orb.add(ticks);

    // Scanner ring that sweeps over the orb while working
    const scanMat = new THREE.MeshBasicMaterial({ color: 0xffffff, ...additive, opacity: 0 });
    const scan = new THREE.Mesh(new THREE.TorusGeometry(1, 0.012, 8, 128), scanMat);
    scan.rotation.x = Math.PI / 2;
    orb.add(scan);

    // Shockwaves rolling out from the core
    const shockGeo = new THREE.SphereGeometry(1, 48, 48);
    const shocks = Array.from({ length: 4 }, () => {
      const mat = new THREE.ShaderMaterial({
        ...additive,
        uniforms: { uAlpha: { value: 0 }, uColor: { value: new THREE.Color() } },
        vertexShader: SHOCK_VERT, fragmentShader: SHOCK_FRAG,
      });
      const mesh = new THREE.Mesh(shockGeo, mat);
      mesh.visible = false;
      orb.add(mesh);
      return { mesh, mat, life: -1, dir: 1 };
    });
    const spawnShock = (dir = 1) => { // dir 1 = outward (speaking), -1 = inward (working)
      const sh = shocks.find((x) => x.life < 0);
      if (sh) { sh.life = 0; sh.dir = dir; sh.mesh.visible = true; }
    };

    // Neural sparks: jagged arcs that fire from the core out to the shell
    const SPARK_N = 20;
    const sparks = Array.from({ length: 3 }, () => {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(SPARK_N * 3), 3));
      const mat = new THREE.LineBasicMaterial({ color: 0xffffff, ...additive, opacity: 0 });
      const line = new THREE.Line(geo, mat);
      line.frustumCulled = false;
      line.visible = false;
      orb.add(line);
      return { geo, mat, line, life: -1, dir: new THREE.Vector3() };
    });
    const fireSpark = () => {
      const sp = sparks.find((x) => x.life < 0);
      if (!sp) return;
      sp.life = 0;
      sp.line.visible = true;
      sp.dir.set(rand() * 2 - 1, rand() * 2 - 1, rand() * 2 - 1).normalize();
    };
    const updateSpark = (sp) => {
      const pos = sp.geo.attributes.position;
      for (let i = 0; i < SPARK_N; i++) {
        const u = i / (SPARK_N - 1), r = 0.35 + u * 0.62, j = Math.sin(u * Math.PI) * 0.14;
        pos.setXYZ(i, sp.dir.x * r + (rand() - 0.5) * j, sp.dir.y * r + (rand() - 0.5) * j, sp.dir.z * r + (rand() - 0.5) * j);
      }
      pos.needsUpdate = true;
    };

    // Orbits: neon comet loops (one with a ringed moon) and a flickering data band
    const beadMat = new THREE.MeshBasicMaterial({ color: 0xff8be8, ...additive, opacity: 1 });
    const beadGlowMat = new THREE.SpriteMaterial({ map: glowTex, color: 0xff5fd8, ...additive, opacity: 0.9 });
    const moonRingMat = new THREE.MeshBasicMaterial({ color: 0xff8be8, ...additive, opacity: 0.9 });
    const moonRings = [];
    const ringRigs = RINGS.map((cfg) => {
      const band = cfg.type === "band";
      const mat = new THREE.ShaderMaterial({
        ...additive,
        side: THREE.DoubleSide,
        uniforms: {
          uA: { value: new THREE.Color() }, uB: { value: new THREE.Color() }, uC: { value: new THREE.Color() },
          uHue: { value: cfg.hue }, uDir: { value: Math.sign(cfg.spin) || 1 },
          uCount: { value: Math.max(cfg.beads, 1) }, uTail: { value: cfg.beads > 0 ? 1 : 0 },
          uMid: { value: cfg.r }, uHalf: { value: cfg.half || 0.01 }, uDash: { value: cfg.dash || 0 },
          uTime: { value: 0 }, uActive: { value: 0.3 },
        },
        vertexShader: RING_VERT, fragmentShader: band ? BAND_FRAG : RING_FRAG,
      });
      const geo = band
        ? new THREE.RingGeometry(cfg.r - cfg.half, cfg.r + cfg.half, 160, 1)
        : new THREE.TorusGeometry(cfg.r, cfg.tube, 12, 180);
      const mesh = new THREE.Mesh(geo, mat);
      for (let k = 0; k < cfg.beads; k++) {
        const a = (k / cfg.beads) * Math.PI * 2;
        const bead = new THREE.Mesh(new THREE.SphereGeometry(cfg.bead, 16, 16), beadMat);
        const glow = new THREE.Sprite(beadGlowMat);
        glow.scale.setScalar(cfg.bead * 7);
        bead.add(glow);
        if (cfg.bead >= 0.08) { // the moon wears its own little ring
          const mr = new THREE.Mesh(new THREE.TorusGeometry(cfg.bead * 2.1, 0.006, 8, 48), moonRingMat);
          bead.add(mr);
          moonRings.push(mr);
        }
        bead.position.set(Math.cos(a) * cfg.r, Math.sin(a) * cfg.r, 0);
        mesh.add(bead);
      }
      const pivot = new THREE.Group();
      const tilted = new THREE.Group();
      tilted.rotation.set(cfg.tilt[0], 0, cfg.tilt[1]);
      tilted.add(mesh);
      pivot.add(tilted);
      orb.add(pivot);
      return { cfg, pivot, tilted, mesh, mat };
    });

    // Base plate + synapse network
    const plate = new THREE.Group();
    plate.position.y = PLATE_Y;
    world.add(plate);

    const under = new THREE.Mesh(
      new THREE.CircleGeometry(PLATE_R * 1.02, 96),
      new THREE.MeshBasicMaterial({ color: 0x05061a, transparent: true, opacity: 0.85 })
    );
    under.rotation.x = -Math.PI / 2;
    under.position.y = -0.01;
    plate.add(under);

    const plateMat = new THREE.ShaderMaterial({
      ...additive,
      uniforms: { uColor: { value: new THREE.Color() }, uGlow: { value: 0.3 }, uRip: { value: 1 }, uRipC: { value: new THREE.Vector2(0.5, 0.5) } },
      vertexShader: PLATE_VERT, fragmentShader: PLATE_FRAG,
    });
    const disc = new THREE.Mesh(new THREE.CircleGeometry(PLATE_R, 96), plateMat);
    disc.rotation.x = -Math.PI / 2;
    plate.add(disc);

    const net = buildNerves(PLATE_R * 0.97);
    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute("position", new THREE.BufferAttribute(net.pos, 3));
    lineGeo.setAttribute("aDist", new THREE.BufferAttribute(net.dist, 1));
    lineGeo.setAttribute("aRnd", new THREE.BufferAttribute(net.rnd, 1));
    lineGeo.setAttribute("aSide", new THREE.BufferAttribute(net.side, 1));
    lineGeo.setIndex(new THREE.BufferAttribute(net.idx, 1));
    const lineMat = new THREE.ShaderMaterial({
      ...additive,
      side: THREE.DoubleSide,
      uniforms: {
        uPhase: { value: 0 }, uEnergy: { value: 0.2 }, uFreq: { value: 1.1 }, uR: { value: PLATE_R },
        uBase: { value: new THREE.Color() }, uEdge: { value: new THREE.Color() }, uPulse: { value: new THREE.Color() },
      },
      vertexShader: LINE_VERT, fragmentShader: LINE_FRAG,
    });
    const lines = new THREE.Mesh(lineGeo, lineMat);
    lines.position.y = 0.01;
    plate.add(lines);

    const nodeGeo = new THREE.BufferGeometry();
    nodeGeo.setAttribute("position", new THREE.BufferAttribute(net.nodes, 3));
    const nodeMat = new THREE.PointsMaterial({ color: 0x9d8cff, size: 0.06, map: glowTex, opacity: 0.4, ...additive });
    const nodePts = new THREE.Points(nodeGeo, nodeMat);
    nodePts.position.y = 0.012;
    plate.add(nodePts);

    // glowing hub where all the fibres meet
    const hubGeo = new THREE.BufferGeometry();
    hubGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array([0, 0.02, 0]), 3));
    const hubMat = new THREE.PointsMaterial({ color: 0xc9b8ff, size: 0.42, map: glowTex, opacity: 0.7, ...additive });
    plate.add(new THREE.Points(hubGeo, hubMat));

    // Animation state
    const cur = { ...STATES.idle, coreC: new THREE.Color(STATES.idle.coreC), rimC: new THREE.Color(STATES.idle.rimC), deepC: new THREE.Color(STATES.idle.deepC) };
    const tmpCore = new THREE.Color(), tmpRim = new THREE.Color(), tmpDeep = new THREE.Color(), tmpLav = new THREE.Color();
    const em = Object.fromEntries(EM_KEYS.map((n) => [n, EMOTIONS.neutral[n]]));
    const eyeDir = new THREE.Vector3(), zAxis = new THREE.Vector3(0, 0, 1);
    const eyeQ = new THREE.Quaternion(), invQ = new THREE.Quaternion();
    const white = new THREE.Color(0xffffff);
    let t = 0, ringT = 0, phase = 0, ampS = 0, swarmT = 0, shockCool = 0, workT = 0, prevAmp = 0, moteT = 0;
    let wobW = 0, eyeW = 0, blink = 0, blinkT = 3;
    let sparkT = 0, hover = false, hoverW = 0, down = null, spinY = 0, spinV = 0, sq = 0, sqV = 0;
    let burst = 0, ringKick = 0, pokeQueue = 0, tapFlash = 0, party = 0, pokes = [];
    const ripC = ripCRef.current;
    const pointer = { x: 0, y: 0, sx: 0, sy: 0 };
    const clock = new THREE.Clock();
    let raf;

    // --- interaction: hover, click to poke, drag to spin, tap the plate ---
    const raycaster = new THREE.Raycaster();
    const orbSphere = new THREE.Sphere(new THREE.Vector3(), 1.3);
    const ndc = new THREE.Vector2(), hitPt = new THREE.Vector3();
    const inside = () => Math.abs(pointer.x) <= 1 && Math.abs(pointer.y) <= 1;
    const hitOrb = () => {
      if (!inside()) return false;
      ndc.set(pointer.x, -pointer.y);
      raycaster.setFromCamera(ndc, camera);
      orb.getWorldPosition(orbSphere.center);
      return raycaster.ray.intersectSphere(orbSphere, hitPt) !== null;
    };
    const poke = () => {
      const now = performance.now();
      pokes = pokes.filter((x) => now - x < 2500);
      pokes.push(now);
      if (pokes.length >= 5) { party = 5; pokes = []; } // easter egg: poke it 5x fast
      spawnShock(); pokeQueue = 0.18;
      rippleRef.current = 0; ripC.set(0.5, 0.5);
      sqV = -5; burst = 1; ringKick = 1;
      fireSpark(); fireSpark(); fireSpark();
      onPokeRef.current?.();
    };
    const plateTap = (uv) => { ripC.copy(uv); rippleRef.current = 0; tapFlash = 1; };
    const setPointer = (e) => {
      const b = mount.getBoundingClientRect();
      pointer.x = ((e.clientX - b.left) / b.width) * 2 - 1;
      pointer.y = ((e.clientY - b.top) / b.height) * 2 - 1;
    };
    const onMove = (e) => {
      setPointer(e);
      if (down) {
        const dx = e.clientX - down.lx;
        down.lx = e.clientX;
        down.moved = Math.max(down.moved, Math.hypot(e.clientX - down.x, e.clientY - down.y));
        spinY += dx * 0.008;
        spinV = dx * 0.008 * 60;
      } else {
        hover = hitOrb();
        renderer.domElement.style.cursor = hover ? "pointer" : "grab";
      }
    };
    const onDown = (e) => {
      if (e.button !== 0) return;
      setPointer(e);
      down = { x: e.clientX, y: e.clientY, lx: e.clientX, t: performance.now(), moved: 0 };
      renderer.domElement.style.cursor = "grabbing";
    };
    const onUp = () => {
      if (!down) return;
      const click = down.moved < 6 && performance.now() - down.t < 500;
      down = null;
      hover = hitOrb();
      renderer.domElement.style.cursor = hover ? "pointer" : "grab";
      if (!click || !inside()) return;
      if (hover) poke();
      else {
        const h = raycaster.intersectObject(disc)[0];
        if (h && h.uv) plateTap(h.uv);
      }
    };
    renderer.domElement.style.touchAction = "none";
    renderer.domElement.style.cursor = "grab";
    renderer.domElement.addEventListener("pointerdown", onDown);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointermove", onMove);

    const resize = () => {
      const w = mount.clientWidth || 1, h = mount.clientHeight || 1;
      renderer.setSize(w, h, false);
      renderer.domElement.style.width = "100%";
      renderer.domElement.style.height = "100%";
      camera.aspect = w / h;
      // keep the whole orb + plate in frame on narrow screens
      camera.position.z = camera.aspect < 0.8 ? 7.4 / Math.max(camera.aspect, 0.45) * 0.72 : 7.4;
      camera.updateProjectionMatrix();
      const px = (h * renderer.getPixelRatio() * 0.5) / Math.tan(THREE.MathUtils.degToRad(22.5));
      pMat.uniforms.uPx.value = px;
      moteMat.uniforms.uPx.value = px;
    };
    const ro = new ResizeObserver(resize);
    ro.observe(mount);
    resize();

    const tick = () => {
      raf = requestAnimationFrame(tick);
      const dt = Math.min(clock.getDelta(), 0.05);
      t += dt;
      const rawKey = (statusRef.current || 'idle').toLowerCase().trim();
      const key = ALIASES[rawKey] || rawKey;
      const target = STATES[key] || STATES.idle;
      const k = 1 - Math.exp(-dt * 3);

      NUM_KEYS.forEach((n) => { cur[n] += (target[n] - cur[n]) * k; });
      const emo = EMOTIONS[emoRef.current] || EMOTIONS.neutral;
      EM_KEYS.forEach((n) => { em[n] += (emo[n] - em[n]) * k; });
      tmpCore.set(emo.coreC || target.coreC); tmpRim.set(emo.rimC || target.rimC); tmpDeep.set(emo.deepC || target.deepC);
      cur.coreC.lerp(tmpCore, k); cur.rimC.lerp(tmpRim, k); cur.deepC.lerp(tmpDeep, k);
      if (party > 0) { // easter egg: rainbow party mode
        party -= dt;
        cur.coreC.setHSL((t * 0.3) % 1, 0.85, 0.6);
        cur.rimC.setHSL((t * 0.3 + 0.15) % 1, 0.9, 0.6);
        cur.deepC.setHSL((t * 0.3 + 0.6) % 1, 0.7, 0.25);
      }

      // speech amplitude (real if provided, simulated otherwise)
      let amp = 0;
      if (key === "speaking") {
        const real = audioRef.current;
        amp = typeof real === "number"
          ? real
          : simSpeech(t);
      }
      ampS += (Math.min(amp, 1) - ampS) * 0.25;

      tapFlash *= Math.exp(-dt * 1.8);
      const energy = Math.min(cur.energy + ampS * 0.5 + tapFlash * 0.6, 1.5);
      const flicker = cur.chaos * (Math.sin(t * 23) * Math.sin(t * 37)) * 0.08;

      ringT += dt * (cur.ring * em.spd + ringKick * 6);
      phase += dt * cur.flow * cur.pulse;
      rippleRef.current = Math.min(1, rippleRef.current + dt * 0.55);

      // interaction physics
      burst *= Math.exp(-dt * 2.2);
      ringKick *= Math.exp(-dt * 2.0);
      if (pokeQueue > 0) { pokeQueue -= dt; if (pokeQueue <= 0) spawnShock(); }
      hoverW += ((hover ? 1 : 0) - hoverW) * 0.12;
      if (!down) { // drag-to-spin with inertia, easing back to centre
        spinY += spinV * dt;
        spinV *= Math.exp(-dt * 1.6);
        spinY = ((((spinY + Math.PI) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2)) - Math.PI;
        spinY -= spinY * Math.min(dt * 0.7, 1);
      }

      // neural sparks
      sparkT -= dt;
      if (sparkT <= 0) {
        fireSpark();
        sparkT = (1.8 / (0.3 + cur.energy * 2.2 + cur.chaos * 2)) * (0.6 + rand() * 0.8);
      }
      sparks.forEach((sp) => {
        if (sp.life < 0) return;
        sp.life += dt / 0.32;
        if (sp.life >= 1) { sp.life = -1; sp.line.visible = false; return; }
        updateSpark(sp);
        sp.mat.opacity = (1 - sp.life) * 0.9;
        sp.mat.color.copy(cur.rimC).lerp(white, 0.55);
      });

      // shockwaves: one per spoken syllable, a slower heartbeat while working
      shockCool -= dt; workT += dt;
      if (key === "speaking" && ampS > 0.42 && prevAmp <= 0.42 && shockCool <= 0) {
        spawnShock(); shockCool = 0.38; rippleRef.current = 0; ripC.set(0.5, 0.5);
      }
      if (key === "working" && workT > 1.1) { spawnShock(-1); workT = 0; }
      prevAmp = ampS;
      shocks.forEach((sh) => {
        if (sh.life < 0) return;
        sh.life += dt / 1.25;
        if (sh.life >= 1) { sh.life = -1; sh.mesh.visible = false; return; }
        const e = 1 - Math.pow(1 - sh.life, 3);
        sh.mesh.scale.setScalar(0.4 + (sh.dir > 0 ? e : 1 - e) * 0.9);
        sh.mat.uniforms.uAlpha.value = sh.dir > 0 ? Math.pow(1 - sh.life, 1.5) * 0.9 : Math.sin(sh.life * Math.PI) * 0.5;
        sh.mat.uniforms.uColor.value.copy(cur.rimC).lerp(white, 0.35);
      });

      // orb
      const breathe = 1 + Math.sin(t * 1.2) * cur.breathe;
      sqV += (-sq * 60 - sqV * 6) * dt; // squash-and-bounce spring (poke)
      sq += sqV * dt;
      wobW += ((key === "speaking" ? 1 : 0) - wobW) * 0.12;
      const bob = Math.sin(t * em.freq) * (0.35 + ampS) * em.amp * wobW; // bounce rides the speech volume
      const idleW = 0.35 + 0.65 * wobW; // sway/tremble stay subtle outside speaking
      const bs = breathe * (1 + sq * 0.25);
      orb.scale.set(bs * (1 - bob * 0.25), bs * (1 + bob * 0.5), bs * (1 - bob * 0.25));
      orb.position.set(
        Math.sin(t * em.freq * 0.5 + 1) * em.sway * (0.3 + ampS) * idleW + (rand() - 0.5) * em.jitter * idleW,
        0.15 + Math.sin(t * 0.8) * 0.04 + em.droop + bob,
        0
      );
      shellMat.uniforms.uTime.value = t;
      shellMat.uniforms.uEnergy.value = energy;
      shellMat.uniforms.uCore.value.copy(cur.coreC);
      shellMat.uniforms.uRim.value.copy(cur.rimC);
      shellMat.uniforms.uDeep.value.copy(cur.deepC);

      coreMesh.scale.setScalar(cur.core * (1 + ampS * 0.4 + flicker));
      haloMat.color.copy(cur.coreC).lerp(cur.rimC, 0.35);
      haloMat.opacity = Math.min(cur.halo + ampS * 0.35 + flicker * 0.6 + hoverW * 0.12, 1);
      halo.scale.setScalar(5.2 * (1 + ampS * 0.12));

      eyeW += ((eyeRef.current ? 1 : 0) - eyeW) * 0.1;
      // core waveform + heart glow
      coreMat.uniforms.uTime.value = t;
      coreMat.uniforms.uAmp.value = ampS + 0.05;
      coreMat.uniforms.uWave.value = cur.wave;
      coreMat.uniforms.uChaos.value = cur.chaos;
      coreMat.uniforms.uVis.value = 1 - eyeW * 0.58; // plasma core softens to an aura around the eye
      coreMat.uniforms.uCore.value.copy(cur.coreC);
      coreMat.uniforms.uRim.value.copy(cur.rimC);
      heartMat.color.copy(cur.coreC).lerp(white, 0.12);
      heartMat.opacity = (0.22 + 0.18 * Math.min(energy, 1) + ampS * 0.2) * (1 - eyeW * 0.5);
      heart.scale.setScalar(1.25 * (1 + ampS * 0.4 + flicker));
      // the core's "eye" drifts toward your cursor
      const ex = pointer.sx * (0.16 + hoverW * 0.04), ey = -pointer.sy * 0.12;
      coreMesh.position.set(ex, ey, 0.04);
      heart.position.copy(coreMesh.position);
      swarm.position.set(ex * 0.4, ey * 0.4, 0);

      // inner detail: swarm, lattice, HUD ticks, scanner
      swarmT += dt * (0.5 + cur.ring * 0.8) * em.spd;
      pMat.uniforms.uT.value = swarmT;
      pMat.uniforms.uPhase.value = phase;
      pMat.uniforms.uSpread.value = cur.energy * 0.6 + ampS * 1.4;
      pMat.uniforms.uPull.value = cur.chaos;
      pMat.uniforms.uBurst.value = burst;
      pMat.uniforms.uAlpha.value = 0.45 + 0.4 * Math.min(energy, 1) + hoverW * 0.2;
      pMat.uniforms.uCore.value.copy(cur.coreC);
      pMat.uniforms.uRim.value.copy(cur.rimC);
      lattice.rotation.y = ringT * 0.3;
      lattice.rotation.x = ringT * 0.18;
      latMat.color.copy(cur.coreC).lerp(white, 0.25);
      latMat.opacity = 0.16 + 0.22 * Math.min(energy, 1) + Math.abs(flicker) * 0.8;
      ticks.rotation.y = ringT * 0.12;
      tickMat.color.copy(cur.coreC).lerp(white, 0.2);
      tickMat.opacity = 0.2 + 0.15 * Math.min(energy, 1);
      const sy = Math.sin(t * 1.9) * 0.97;
      scan.position.y = sy;
      scan.scale.setScalar(Math.sqrt(Math.max(1 - sy * sy, 0.02)) * 1.03);
      scanMat.opacity = cur.chaos * 0.8;
      scanMat.color.copy(cur.rimC).lerp(white, 0.4);

      // rings
      tmpLav.copy(cur.coreC).lerp(white, 0.45);
      ringRigs.forEach(({ cfg, pivot, tilted, mesh, mat }) => {
        pivot.rotation[cfg.axis] = ringT * cfg.speed;
        pivot.scale.setScalar(1 + ampS * 0.05); // rings breathe with speech
        tilted.rotation.x = cfg.tilt[0] + Math.sin(ringT * 0.9 + cfg.hue * 6) * 0.12; // gimbal wobble
        mesh.rotation.z = ringT * cfg.spin;
        mat.uniforms.uA.value.copy(cur.coreC);
        mat.uniforms.uB.value.copy(cur.rimC);
        mat.uniforms.uC.value.copy(tmpLav);
        mat.uniforms.uTime.value = t;
        mat.uniforms.uActive.value = 0.25 + cur.chaos * 0.9 + ampS * 0.5;
      });
      beadMat.color.copy(cur.rimC).lerp(white, 0.35);
      beadGlowMat.color.copy(cur.rimC);
      moonRingMat.color.copy(cur.rimC).lerp(white, 0.3);
      moonRings.forEach((m) => m.rotation.set(1.1, 0, ringT * 2));

      // plate
      plateMat.uniforms.uColor.value.copy(cur.coreC);
      plateMat.uniforms.uGlow.value = cur.plate + ampS * 0.4;
      plateMat.uniforms.uRip.value = rippleRef.current;
      plateMat.uniforms.uRipC.value.copy(ripC);
      lineMat.uniforms.uPhase.value = phase;
      lineMat.uniforms.uEnergy.value = energy;
      lineMat.uniforms.uFreq.value = cur.freq;
      lineMat.uniforms.uPulse.value.copy(cur.rimC).lerp(white, 0.35);
      lineMat.uniforms.uBase.value.copy(cur.coreC).lerp(cur.deepC, 0.35);
      lineMat.uniforms.uEdge.value.copy(cur.rimC).lerp(cur.deepC, 0.3);
      nodeMat.opacity = 0.35 + 0.55 * Math.min(energy, 1);
      nodeMat.color.copy(cur.rimC).lerp(white, 0.25);
      hubMat.opacity = 0.5 + 0.4 * Math.min(energy, 1);
      hubMat.color.copy(cur.coreC).lerp(white, 0.3);

      // environment
      moteT += dt * cur.mote;
      moteMat.uniforms.uT.value = moteT;
      moteMat.uniforms.uSwirl.value = cur.swirl;
      moteMat.uniforms.uAlpha.value = 0.5 + 0.3 * Math.min(energy, 1);
      moteMat.uniforms.uCore.value.copy(cur.coreC);
      moteMat.uniforms.uRim.value.copy(cur.rimC);
      beamMat.uniforms.uTime.value = t;
      beamMat.uniforms.uColor.value.copy(cur.coreC).lerp(cur.rimC, 0.3);
      beamMat.uniforms.uBeam.value = cur.beam + ampS * 0.3;
      floorMat.uniforms.uColor.value.copy(cur.coreC).lerp(cur.rimC, 0.4);
      floorMat.uniforms.uGlow.value = cur.plate + ampS * 0.3;
      floorMat.uniforms.uRip.value = rippleRef.current;
      nebMat.uniforms.uTime.value = t;
      nebMat.uniforms.uA.value.copy(cur.deepC);
      nebMat.uniforms.uB.value.copy(cur.coreC).lerp(cur.deepC, 0.5);
      nebMat.uniforms.uC.value.copy(cur.rimC);
      nebMat.uniforms.uEnergy.value = Math.min(energy, 1);

      // eyeball: follows the cursor, dilates with mood, blinks
      blinkT -= dt;
      if (blinkT <= 0) { blink = 1; blinkT = 2.5 + rand() * 4; }
      blink = Math.max(0, blink - dt * 7);
      const pupilBase = key === "listening" ? 0.62 : key === "working" ? 0.36 : key === "speaking" ? 0.5 : 0.46;
      eyeMat.uniforms.uTime.value = t;
      eyeMat.uniforms.uPupil.value = Math.min(0.85, Math.max(0.2, pupilBase + em.pupil + burst * 0.15 + hoverW * 0.05 + ampS * 0.08));
      eyeMat.uniforms.uIrisA.value.copy(cur.rimC);
      eyeMat.uniforms.uIrisB.value.copy(cur.coreC);
      eyeMat.uniforms.uSclera.value.copy(cur.deepC).lerp(white, 0.22);
      eyeMesh.visible = eyeW > 0.02;
      eyeMesh.position.copy(coreMesh.position);
      eyeMesh.scale.set(eyeW, eyeW * (1 - em.lid * 0.6) * (1 - blink * 0.9), eyeW);
      eyeDir.set(pointer.sx * 1.1, -pointer.sy * 0.8 + 0.18, 1).normalize();
      eyeDir.applyQuaternion(invQ.copy(world.quaternion).invert());
      eyeQ.setFromUnitVectors(zAxis, eyeDir);
      eyeMesh.quaternion.slerp(eyeQ, 0.25);

      // parallax + drifting stars
      pointer.sx += (pointer.x - pointer.sx) * 0.05;
      pointer.sy += (pointer.y - pointer.sy) * 0.05;
      world.rotation.y = pointer.sx * 0.35 + spinY;
      world.rotation.x = pointer.sy * 0.08;
      stars.rotation.y = t * 0.01;

      renderer.render(scene, camera);
    };
    tick();

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      renderer.domElement.removeEventListener("pointerdown", onDown);
      scene.traverse((o) => {
        o.geometry?.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
      });
      glowTex.dispose();
      renderer.dispose();
      mount.removeChild(renderer.domElement);
    };
  }, []);

  return (
    <div
      ref={mountRef}
      className={className}
      style={{
        width: "100%",
        height: "100%",
        minHeight: 320,
        background: "radial-gradient(ellipse at 50% 100%, #1b0f4a 0%, transparent 55%), radial-gradient(ellipse at 50% 40%, #120d3d 0%, #07071f 55%, #020208 100%)",
        ...style,
      }}
    />
  );
}

/* ---------- demo: drop this in to test every state ---------- */

const DEMO_STATES = ["idle", "listening", "working", "speaking"];
const DEMO_EMOTIONS = [["neutral", "😐"], ["happy", "😄"], ["excited", "🤩"], ["sad", "😢"], ["angry", "😠"], ["love", "😍"]];

export default function CortexOrbDemo() {
  const [status, setStatus] = useState("speaking");
  const [emotion, setEmotion] = useState("neutral");
  const [eye, setEye] = useState(true);
  const btn = (on) => ({
    padding: "8px 14px", borderRadius: 999, border: "none", cursor: "pointer",
    font: "500 13px system-ui, sans-serif", textTransform: "capitalize",
    color: on ? "#0a0820" : "#c9c0ff", background: on ? "#a99bff" : "transparent",
  });
  const bar = {
    display: "flex", gap: 6, padding: 6, borderRadius: 999, flexWrap: "wrap", justifyContent: "center",
    background: "rgba(20,16,60,0.55)", border: "1px solid rgba(140,120,255,0.25)", backdropFilter: "blur(8px)",
  };
  return (
    <div style={{ position: "relative", width: "100%", height: "100vh" }}>
      <CortexOrb status={status} emotion={emotion} eye={eye} />
      <div style={{
        position: "absolute", top: 16, left: 0, right: 0, textAlign: "center", pointerEvents: "none",
        font: "12px system-ui, sans-serif", color: "rgba(190,180,255,0.55)",
      }}>
        pick a mood + "speaking" to see the wobble · the eye follows your cursor · click the orb · drag to spin
      </div>
      <div style={{
        position: "absolute", bottom: 24, left: 0, right: 0, padding: "0 12px",
        display: "flex", flexDirection: "column", alignItems: "center", gap: 10,
      }}>
        <div style={bar}>
          {DEMO_EMOTIONS.map(([e, icon]) => (
            <button key={e} onClick={() => setEmotion(e)} style={btn(emotion === e)}>{icon} {e}</button>
          ))}
          <button onClick={() => setEye(!eye)} style={btn(eye)}>👁 eye</button>
        </div>
        <div style={bar}>
          {DEMO_STATES.map((st) => (
            <button key={st} onClick={() => setStatus(st)} style={btn(status === st)}>{st}</button>
          ))}
        </div>
      </div>
    </div>
  );
}
