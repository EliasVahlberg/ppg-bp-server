#!/usr/bin/env python3
"""
Generates the ppg-bp logo SVG from a parametric arterial/PPG pulse
waveform model, instead of hand-drawn or hand-copied path data.

Model: two Gaussians (systolic peak, dicrotic/diastolic wave) plus a
small Gaussian anacrotic rise and an exponential diastolic runoff --
the standard multi-Gaussian decomposition used in arterial pulse wave
literature and in synthetic PPG generators (e.g. NeuroKit2's PPG
simulator).

Usage:
    python3 generate_logo.py [--out logo.svg] [--preview preview.png]
"""
from __future__ import annotations

import argparse
import base64
import subprocess
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Waveform model
# ---------------------------------------------------------------------------

# Tweakable knobs. t is normalized 0..1 across one pulse cycle.
#
# Model, rebuilt against real reference tracings (brachial-artery BP
# waveform, arterial line trace with anacrotic-limb/dicrotic-notch
# labels, and a peripheral-artery pulse + bioimpedance figure):
#
#   systole:     asymmetric Gaussian -- fast rise (anacrotic limb +
#                upstroke), slower initial decline. A plain symmetric
#                Gaussian here made the peak look like an EKG spike;
#                real arterial/PPG systolic peaks are asymmetric.
#   notch_bump:  small asymmetric Gaussian for the dicrotic notch dip
#                and the diastolic wave rebound right after it. Needs
#                enough amplitude to actually carve a visible dip out
#                of the decay curve below -- too small and it just gets
#                absorbed into the decay with no visible notch at all.
#   decay:       a SLOW exponential (small rate) spread across most of
#                the remaining cycle. Diastole is the long, gently
#                concave two-thirds of the cycle in every reference
#                image; an exponential tuned to snap back to baseline
#                quickly (as in earlier drafts) is wrong on cycle
#                proportions, not just aesthetics.
#   baseline:    small constant floor so the trough doesn't touch zero.
PARAMS = dict(
    sys_A=1.0, sys_mu=0.16, sys_w_rise=0.06, sys_w_fall=0.10,
    notch_A=0.24, notch_mu=0.40, notch_w_rise=0.045, notch_w_fall=0.10,
    decay_amp=0.60, decay_rate=1.0,
    baseline=0.05,
)


def asym_gaussian(t: np.ndarray, A: float, mu: float, w_rise: float, w_fall: float) -> np.ndarray:
    """Gaussian with a different width on each side of the peak, so the
    rising and falling edges don't have to mirror each other -- needed
    to reproduce the fast-rise/slower-fall shape of a real systolic
    peak (a symmetric Gaussian looks like an EKG spike instead)."""
    w = np.where(t < mu, w_rise, w_fall)
    return A * np.exp(-((t - mu) / w) ** 2)


def arterial_waveform(t: np.ndarray, p: dict = PARAMS) -> np.ndarray:
    systole = asym_gaussian(t, p["sys_A"], p["sys_mu"], p["sys_w_rise"], p["sys_w_fall"])
    notch_bump = asym_gaussian(
        t, p["notch_A"], p["notch_mu"], p["notch_w_rise"], p["notch_w_fall"]
    )
    decay = p["decay_amp"] * np.exp(-p["decay_rate"] * t)
    return systole + notch_bump + decay + p["baseline"]


# ---------------------------------------------------------------------------
# Sampling + path fitting
# ---------------------------------------------------------------------------

VB_W, VB_H = 240, 240
MARGIN_X = 24
PEN_TOP = 34
PEN_BOTTOM = 176


def sample_points() -> list[tuple[float, float]]:
    """Adaptive sampling: dense through the systolic rise/peak and the
    notch/diastolic-wave region, sparser on the long decay tail."""
    t = np.unique(np.concatenate([
        np.linspace(0.00, 0.10, 8),    # trough into anacrotic rise
        np.linspace(0.10, 0.28, 20),   # systolic upstroke + peak + initial decline
        np.linspace(0.28, 0.45, 18),   # dicrotic notch + diastolic wave
        np.linspace(0.45, 1.00, 14),   # long diastolic decay tail
    ]))
    y = arterial_waveform(t)
    y_min, y_max = y.min(), y.max()
    cycle_w = VB_W - 2 * MARGIN_X

    pts = []
    for tv, yv in zip(t, y):
        x = MARGIN_X + tv * cycle_w
        norm = (yv - y_min) / (y_max - y_min)
        yy = PEN_BOTTOM - norm * (PEN_BOTTOM - PEN_TOP)
        pts.append((round(x, 1), round(yy, 1)))
    return pts


def catmull_rom_to_bezier(points: list[tuple[float, float]]) -> list[str]:
    """Fit a smooth cubic-Bezier spline through sampled points (Catmull-Rom
    conversion), so the path stays faithful to the underlying function
    instead of being a polyline or a hand-tuned curve."""
    p = [points[0]] + points + [points[-1]]
    segs = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        c1x = round(p1[0] + (p2[0] - p0[0]) / 6, 1)
        c1y = round(p1[1] + (p2[1] - p0[1]) / 6, 1)
        c2x = round(p2[0] - (p3[0] - p1[0]) / 6, 1)
        c2y = round(p2[1] - (p3[1] - p1[1]) / 6, 1)
        segs.append(f"C {c1x} {c1y}, {c2x} {c2y}, {p2[0]} {p2[1]}")
    return segs


def build_path_d() -> str:
    pts = sample_points()
    segs = catmull_rom_to_bezier(pts)
    lead_x = 14
    trail_x = VB_W - 14
    return (
        f"M {lead_x} {PEN_BOTTOM} L {pts[0][0]} {pts[0][1]} "
        + " ".join(segs)
        + f" L {trail_x} {PEN_BOTTOM}"
    )


# ---------------------------------------------------------------------------
# SVG assembly
# ---------------------------------------------------------------------------

def build_icon_group(x_offset: float = 0, y_offset: float = 0) -> str:
    """Return the <g> markup for the waveform mark (halo + crisp trace),
    translated by (x_offset, y_offset). Shared by the standalone icon
    and the wordmark lockup so both always draw the exact same mark."""
    path_d = build_path_d()
    transform = f' transform="translate({x_offset} {y_offset})"' if (x_offset or y_offset) else ""
    return (
        f'<g fill="none" stroke-linecap="round" stroke-linejoin="round"{transform}>'
        f'<path d="{path_d}" stroke="{FG_COLOR}" stroke-opacity="0.28" stroke-width="20" />'
        f'<path d="{path_d}" stroke="{FG_COLOR}" stroke-width="6" />'
        f"</g>"
    )


ICON_SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w} {vb_h}" width="{vb_w}" height="{vb_h}">
  <!--
    ppg-bp logo, arterial/PPG pulse waveform, generated (not
    hand-drawn) from a parametric model fitted against real reference
    tracings (brachial-artery BP waveform; an arterial-line trace
    labeled with anacrotic limb, systolic peak, dicrotic notch, and
    dicrotic limb; and a peripheral-artery pulse + bioimpedance
    figure):

      f(t) = asym_gaussian(systole)     fast rise, slower initial fall
           + asym_gaussian(notch_bump)  dicrotic notch dip + diastolic wave
           + decay_amp * exp(-decay_rate*t) + baseline   diastolic runoff

    where asym_gaussian uses a different Gaussian width on each side
    of its peak (needed for the systolic peak's fast-rise/slower-fall
    asymmetry, a symmetric Gaussian there looks like an EKG spike,
    not an arterial pulse). Parameters: {params}

    This is the same family of model used in the PPG/arterial-waveform
    literature (multi-Gaussian decomposition of the systolic peak,
    dicrotic notch, and diastolic wave) and in synthetic-PPG generators
    such as NeuroKit2, generated from an actual function, not a shape
    that just looks plausible. Regenerate with generate_logo.py if the
    parameters need retuning; do not hand-edit the path data below.

    Color is the PPG green (LED wavelength ~525-535nm). Two-tone flat
    treatment (wide low-opacity stroke behind + crisp stroke on top)
    suggests glow without blur filters, so it stays sharp at small
    sizes (favicon, avatar, terminal-adjacent contexts).

    Background rect is included for preview only, drop it for a
    transparent version to drop into light or dark READMEs.
  -->

  <rect x="0" y="0" width="{vb_w}" height="{vb_h}" rx="36" fill="{bg_color}" />
  {extra_glyph}
  {icon_group}
</svg>
"""

BG_COLOR = "#0D1210"
FG_COLOR = "#1EFA8C"
GLYPH_COLOR = "#5A7A6E"  # brighter-than-before muted accent for the repo-variant glyphs


# ---------------------------------------------------------------------------
# Repo-variant glyphs (android / server), drawn in the empty upper-right
# quadrant so they never overlap the waveform mark itself -- one base mark,
# small consistent additions per repo, instead of three unrelated icons.
# ---------------------------------------------------------------------------

def _phone_glyph() -> str:
    """A small rounded phone outline in the icon's empty upper-right
    quadrant, for the android recorder repo. The waveform's own peak
    tops out around x=60,y=34 and its baseline runs along y=176, so the
    region roughly x=150-224, y=16-100 is clear."""
    return (
        f'<rect x="164" y="18" width="52" height="86" rx="9" '
        f'fill="none" stroke="{GLYPH_COLOR}" stroke-width="4" />'
        f'<circle cx="190" cy="94" r="2.2" fill="{GLYPH_COLOR}" />'
    )


def _server_glyph() -> str:
    """A small stacked-disk (canonical DB / server) outline in the
    icon's empty upper-right quadrant, for the ingest server repo."""
    cx = 190
    rows_y = [26, 42, 58]
    rx, ry = 26, 7
    disks = "".join(
        f'<ellipse cx="{cx}" cy="{y}" rx="{rx}" ry="{ry}" fill="none" '
        f'stroke="{GLYPH_COLOR}" stroke-width="3.5" />'
        for y in rows_y
    )
    sides = "".join(
        f'<line x1="{x}" y1="26" x2="{x}" y2="82" stroke="{GLYPH_COLOR}" stroke-width="3.5" />'
        for x in (cx - rx, cx + rx)
    )
    base = (
        f'<path d="M {cx - rx} 82 A {rx} {ry} 0 0 0 {cx + rx} 82" '
        f'fill="none" stroke="{GLYPH_COLOR}" stroke-width="3.5" />'
    )
    return disks + sides + base




VARIANTS = {
    "core": None,
    "android": _phone_glyph,
    "server": _server_glyph,
}


def render_icon_svg(variant: str = "core") -> str:
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}, choose from {list(VARIANTS)}")
    glyph_fn = VARIANTS[variant]
    extra_glyph = glyph_fn() if glyph_fn else ""
    params_str = ", ".join(f"{k}={v}" for k, v in PARAMS.items())
    svg = ICON_SVG_TEMPLATE.format(
        vb_w=VB_W, vb_h=VB_H, params=params_str,
        bg_color=BG_COLOR, extra_glyph=extra_glyph,
        icon_group=build_icon_group(),
    )
    _assert_no_bad_xml_comment(svg)
    return svg


def _assert_no_bad_xml_comment(svg: str) -> None:
    # XML comments cannot contain "--"; catch this before writing a
    # broken file instead of finding out when the SVG fails to render.
    comment_start = svg.index("<!--")
    comment_end = svg.index("-->") + 3
    comment_body = svg[comment_start + 4 : comment_end - 3]
    if "--" in comment_body:
        bad_line = next(line for line in comment_body.splitlines() if "--" in line)
        raise ValueError(f"SVG comment contains '--', which is invalid XML: {bad_line!r}")


# ---------------------------------------------------------------------------
# Wordmark lockup: icon + "ppg-bp" text, set in JetBrains Mono.
# ---------------------------------------------------------------------------

WORDMARK_SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w} {vb_h}" width="{vb_w}" height="{vb_h}">
  <!--
    ppg-bp wordmark: the waveform icon (see generate_logo.py for the
    waveform model) plus a "{label}" text lockup set in JetBrains Mono
    Bold (OFL-licensed, assets/fonts/JetBrainsMono-Bold.ttf). Monospace
    matches the icon's own oscilloscope/fixed-grid visual logic instead
    of fighting it with a humanist or serif face.

    Background rect is for preview only, drop it for a transparent
    version.
  -->
  <defs>
    <style>
      @font-face {{
        font-family: "JetBrains Mono";
        font-weight: 700;
        src: url("{font_data_uri}") format("truetype");
      }}
      .wordmark {{
        font-family: "JetBrains Mono", monospace;
        font-weight: 700;
        font-size: {font_size}px;
        fill: {fg_color};
      }}
    </style>
  </defs>

  <rect x="0" y="0" width="{vb_w}" height="{vb_h}" rx="20" fill="{bg_color}" />

  {icon_group}
  <text x="{text_x}" y="{text_y}" class="wordmark" dominant-baseline="middle">{label}</text>
</svg>
"""


def render_wordmark_svg(label: str = "ppg-bp") -> str:
    icon_scale = 0.62
    icon_size = VB_H * icon_scale
    icon_margin = 20
    # Wordmark canvas is wider than the icon's own square viewBox.
    wm_w = 560
    wm_h = 240

    icon_group = build_icon_group(x_offset=icon_margin, y_offset=(wm_h - icon_size) / 2)
    # build_icon_group draws in the icon's own 240x240 coordinate space;
    # scale it down to icon_size via a wrapping transform, rather than
    # rebuilding the geometry, so it's guaranteed to match the standalone
    # icon exactly.
    scale = icon_size / VB_H
    icon_group = (
        f'<g transform="translate({icon_margin} {(wm_h - icon_size) / 2}) scale({scale:.4f})">'
        + build_icon_group()
        + "</g>"
    )

    font_path = Path(__file__).parent / "assets" / "fonts" / "JetBrainsMono-Bold.ttf"
    font_data_uri = "data:font/ttf;base64," + base64.b64encode(font_path.read_bytes()).decode()

    text_x = icon_margin + icon_size + 22
    text_y = wm_h / 2
    font_size = 64

    svg = WORDMARK_SVG_TEMPLATE.format(
        vb_w=wm_w, vb_h=wm_h, label=label,
        font_data_uri=font_data_uri, font_size=font_size,
        fg_color=FG_COLOR, bg_color=BG_COLOR,
        icon_group=icon_group, text_x=text_x, text_y=text_y,
    )
    _assert_no_bad_xml_comment(svg)
    return svg


# ---------------------------------------------------------------------------
# README banner + GitHub Social Preview: same wordmark lockup, just a
# wider canvas so the icon+text sit centered with breathing room around
# them, rather than filling the frame edge-to-edge like the compact
# wordmark does. Same source markup (build_icon_group + the wordmark
# text), no separate artwork.
# ---------------------------------------------------------------------------

BANNER_SVG_TEMPLATE = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w} {vb_h}" width="{vb_w}" height="{vb_h}">
  <!--
    ppg-bp banner: the same wordmark lockup as wordmark.svg (icon plus
    "{label}" text in JetBrains Mono Bold), centered on a wider canvas
    with margin for use as a README header image or a GitHub repo
    Social Preview image. Generated from generate_logo.py, not
    hand-assembled; regenerate using the banner or social kind if
    the waveform or wordmark changes.
  -->
  <defs>
    <style>
      @font-face {{
        font-family: "JetBrains Mono";
        font-weight: 700;
        src: url("{font_data_uri}") format("truetype");
      }}
      .wordmark {{
        font-family: "JetBrains Mono", monospace;
        font-weight: 700;
        font-size: {font_size}px;
        fill: {fg_color};
      }}
    </style>
  </defs>

  <rect x="0" y="0" width="{vb_w}" height="{vb_h}" fill="{bg_color}" />

  {icon_group}
  <text x="{text_x}" y="{text_y}" class="wordmark" dominant-baseline="middle">{label}</text>
</svg>
"""


def render_banner_svg(
    label: str = "ppg-bp", vb_w: int = 900, vb_h: int = 400, variant: str = "core",
) -> str:
    """Centered icon+wordmark lockup on an arbitrary canvas. Used for both
    the README header banner (~900x400) and the GitHub Social Preview
    image (1280x640) -- same layout logic, just a different canvas size,
    since both are "logo centered on a plain background" images.

    The icon's drawn content (waveform path plus its halo stroke) does
    not fill its own 240x240 viewBox -- it spans roughly x=[14,226],
    y=[24,186] once the 20px halo stroke is accounted for (see
    build_path_d's lead-in/out at x=14/226 and PEN_TOP/PEN_BOTTOM=34/176
    plus half the halo's stroke width). Scaling as if the icon filled
    the full 240x240 box undersizes the visible mark and throws off the
    centering math against the wordmark text next to it, so the visible
    bounding box below is measured from those constants instead of
    assumed to be the full viewBox.
    """
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}, choose from {list(VARIANTS)}")

    halo_pad = 10  # half of the 20px halo stroke width
    icon_x0, icon_x1 = 14 - halo_pad, (VB_W - 14) + halo_pad
    icon_y0, icon_y1 = PEN_TOP - halo_pad, PEN_BOTTOM + halo_pad
    icon_w, icon_h = icon_x1 - icon_x0, icon_y1 - icon_y0

    target_icon_h = vb_h * 0.62
    scale = target_icon_h / icon_h
    icon_w_scaled = icon_w * scale

    gap = target_icon_h * 0.18
    # JetBrains Mono's fixed advance width is exactly 0.6em (measured from
    # the font's own hmtx table), so text width for a label at a given
    # font_size is exact, not a rough estimate -- this lets us solve for
    # the largest font_size that keeps icon + gap + text within the
    # canvas width, instead of guessing a size and clipping on long
    # labels (e.g. "ppg-bp-android" is much wider than "ppg-bp").
    char_w_ratio = 0.6
    max_text_w = vb_w * 0.82 - icon_w_scaled - gap  # leave ~9% margin each side
    font_size_for_width = max_text_w / (len(label) * char_w_ratio)
    # Also cap by the icon's own height ratio so short labels (e.g. the
    # bare "ppg-bp") don't blow up to fill all the leftover width.
    font_size_for_height = target_icon_h * (64 / (0.62 * VB_H)) * (icon_h / VB_H)
    font_size = min(font_size_for_width, font_size_for_height)
    text_w_est = len(label) * font_size * char_w_ratio

    block_w = icon_w_scaled + gap + text_w_est
    origin_x = (vb_w - block_w) / 2
    origin_y = (vb_h - target_icon_h) / 2

    glyph_fn = VARIANTS[variant]
    extra_glyph = glyph_fn() if glyph_fn else ""
    # Translate so the icon's visible top-left (icon_x0, icon_y0), not the
    # viewBox origin, lands at (origin_x, origin_y) after scaling.
    tx = origin_x - icon_x0 * scale
    ty = origin_y - icon_y0 * scale
    icon_group = (
        f'<g transform="translate({tx:.1f} {ty:.1f}) scale({scale:.4f})">'
        + extra_glyph + build_icon_group()
        + "</g>"
    )

    font_path = Path(__file__).parent / "assets" / "fonts" / "JetBrainsMono-Bold.ttf"
    font_data_uri = "data:font/ttf;base64," + base64.b64encode(font_path.read_bytes()).decode()

    text_x = origin_x + icon_w_scaled + gap
    text_y = vb_h / 2

    svg = BANNER_SVG_TEMPLATE.format(
        vb_w=vb_w, vb_h=vb_h, label=label,
        font_data_uri=font_data_uri, font_size=round(font_size, 1),
        fg_color=FG_COLOR, bg_color=BG_COLOR,
        icon_group=icon_group, text_x=round(text_x, 1), text_y=round(text_y, 1),
    )
    _assert_no_bad_xml_comment(svg)
    return svg


def _screenshot(svg_path: Path, png_path: Path, width: int = 480, height: int = 480) -> None:
    subprocess.run(
        [
            "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
            f"--screenshot={png_path}",
            f"--window-size={width},{height}",
            "--default-background-color=00000000",
            # embedded @font-face (data URI) needs a moment to decode/apply
            # before the screenshot fires, otherwise text renders with the
            # fallback font or not at all.
            "--virtual-time-budget=3000",
            f"file://{svg_path.resolve()}",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--kind", choices=["icon", "wordmark", "banner", "social", "all"], default="all",
        help="icon: waveform mark only. wordmark: icon + text lockup "
             "(compact, square-ish icon). banner: icon + text lockup "
             "centered on a ~900x400 canvas, for a README header image. "
             "social: same lockup on a 1280x640 canvas, for a GitHub "
             "repo Social Preview image. all: every icon variant + the "
             "wordmark (does not include banner/social, generate those "
             "explicitly).",
    )
    ap.add_argument(
        "--variant", choices=list(VARIANTS), default="core",
        help="Which repo-specific glyph to draw behind the icon "
             "(only used with --kind icon).",
    )
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--no-preview", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    def write(name: str, svg: str, size: tuple[int, int] = (480, 480)) -> None:
        svg_path = out_dir / f"{name}.svg"
        svg_path.write_text(svg)
        print(f"wrote {svg_path}")
        if not args.no_preview:
            png_path = out_dir / f"{name}-preview.png"
            _screenshot(svg_path, png_path, width=size[0], height=size[1])
            print(f"wrote {png_path}")

    if args.kind in ("icon", "all"):
        variants = list(VARIANTS) if args.kind == "all" else [args.variant]
        for variant in variants:
            name = "icon" if variant == "core" else f"icon-{variant}"
            write(name, render_icon_svg(variant))

    if args.kind in ("wordmark", "all"):
        write("wordmark", render_wordmark_svg(), size=(560, 240))

    if args.kind == "banner":
        label = "ppg-bp" if args.variant == "core" else f"ppg-bp-{args.variant}"
        write(
            "banner", render_banner_svg(label=label, vb_w=900, vb_h=400, variant=args.variant),
            size=(900, 400),
        )

    if args.kind == "social":
        label = "ppg-bp" if args.variant == "core" else f"ppg-bp-{args.variant}"
        write(
            "social-preview",
            render_banner_svg(label=label, vb_w=1280, vb_h=640, variant=args.variant),
            size=(1280, 640),
        )


if __name__ == "__main__":
    main()
