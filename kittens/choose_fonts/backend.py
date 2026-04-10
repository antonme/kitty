#!/usr/bin/env python
# License: GPLv3 Copyright: 2024, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import re
import string
import sys
import tempfile
from typing import TYPE_CHECKING, Any, Literal, Optional, TypedDict

from kitty.cli import create_default_opts
from kitty.conf.utils import to_color
from kitty.constants import kitten_exe
from kitty.fast_data_types import wcswidth
from kitty.fonts import Descriptor
from kitty.fonts.common import (
    face_from_descriptor,
    get_axis_map,
    get_font_files,
    get_named_style,
    get_variable_data_for_descriptor,
    get_variable_data_for_face,
    is_variable,
    spec_for_face,
)
from kitty.fonts.features import Type, known_features
from kitty.fonts.list import create_family_groups
from kitty.fonts.render import display_bitmap
from kitty.options.types import Options
from kitty.options.utils import parse_font_spec
from kitty.typing_compat import NotRequired
from kitty.utils import screen_size_function

if TYPE_CHECKING:
    from kitty.fast_data_types import FeatureData

def setup_debug_print() -> bool:
    if 'KITTY_STDIO_FORWARDED' in os.environ:
        try:
            fd = int(os.environ['KITTY_STDIO_FORWARDED'])
        except Exception:
            return False
        try:
            sys.stdout = open(fd, 'w', closefd=False)
            return True
        except OSError:
            return False
    return False


def send_to_kitten(x: Any) -> None:
    f = sys.__stdout__
    assert f is not None
    try:
        f.buffer.write(json.dumps(x).encode())
        f.buffer.write(b'\n')
        f.buffer.flush()
    except BrokenPipeError:
        raise SystemExit('Pipe to kitten was broken while sending data to it')


class TextStyle(TypedDict):
    font_size: float
    dpi_x: float
    dpi_y: float
    foreground: str
    background: str
    ansi_colors: NotRequired[list[str]]


OptNames = Literal['font_family', 'bold_font', 'italic_font', 'bold_italic_font']
FamilyKey = tuple[OptNames, ...]


def opts_from_cmd(cmd: dict[str, Any]) -> tuple[Options, FamilyKey, float, float]:
    opts = Options()
    ts: TextStyle = cmd['text_style']
    opts.font_size = ts['font_size']
    opts.foreground = to_color(ts['foreground'])
    opts.background = to_color(ts['background'])
    family_key = []
    def d(k: OptNames) -> None:
        if k in cmd:
            setattr(opts, k, parse_font_spec(cmd[k]))
            family_key.append(k)
    d('font_family')
    d('bold_font')
    d('italic_font')
    d('bold_italic_font')
    return opts, tuple(family_key), ts['dpi_x'], ts['dpi_y']


BaseKey = tuple[str, int, int]
FaceKey = tuple[str, BaseKey]
RenderedSample = tuple[bytes, dict[str, Any]]
RenderedSampleTransmit = dict[str, Any]
SAMPLE_TEXT = string.ascii_lowercase + ' ' + string.digits + ' ' + string.ascii_uppercase + ' ' + string.punctuation
SGR_PATTERN = re.compile(r'\x1b\[([0-9:;]*)m')
FALLBACK_ANSI_COLORS = (
    0x000000, 0xcd0000, 0x00cd00, 0xcdcd00, 0x0000ee, 0xcd00cd, 0x00cdcd, 0xe5e5e5,
    0x7f7f7f, 0xff0000, 0x00ff00, 0xffff00, 0x5c5cff, 0xff00ff, 0x00ffff, 0xffffff,
)
TAB_STOP = 4


class FD(TypedDict):
    is_index: bool
    name: NotRequired[str]
    tooltip: NotRequired[str]
    sample: NotRequired[str]
    params: NotRequired[tuple[str, ...]]



def get_features(features: dict[str, Optional['FeatureData']]) -> dict[str, FD]:
    ans = {}
    for tag, data in features.items():
        kf = known_features.get(tag)
        if kf is None or kf.type is Type.hidden:
            continue
        fd: FD = {'is_index': kf.type is Type.index}
        ans[tag] = fd
        if data is not None:
            if n := data.get('name'):
                fd['name'] = n
            if n := data.get('tooltip'):
                fd['tooltip'] = n
            if n := data.get('sample'):
                fd['sample'] = n
            if p := data.get('params'):
                fd['params'] = p
    return ans


def ansi_palette_from_text_style(ts: TextStyle, default_fg: int) -> list[int]:
    ans = list(FALLBACK_ANSI_COLORS)
    colors = ts.get('ansi_colors') or []
    for i in range(min(len(colors), 16)):
        if colors[i]:
            c = to_color(colors[i])
            if c is not None:
                ans[i] = c.rgb
    return ans


def color_from_256_index(idx: int, palette: list[int]) -> int:
    if 0 <= idx < 16:
        return palette[idx]
    if idx < 232:
        idx -= 16
        steps = (0, 95, 135, 175, 215, 255)
        r, idx = divmod(idx, 36)
        g, b = divmod(idx, 6)
        return (steps[r] << 16) | (steps[g] << 8) | steps[b]
    gray = 8 + (idx - 232) * 10
    return (gray << 16) | (gray << 8) | gray


def apply_sgr_to_fg(params: str, current_fg: int, default_fg: int, palette: list[int]) -> int:
    raw = params.replace(':', ';')
    items: list[int] = []
    for item in raw.split(';'):
        if item == '':
            items.append(0)
        else:
            try:
                items.append(int(item))
            except ValueError:
                pass
    if not items:
        items = [0]
    i = 0
    while i < len(items):
        p = items[i]
        if p == 0 or p == 39:
            current_fg = default_fg
        elif 30 <= p <= 37:
            current_fg = palette[p - 30]
        elif 90 <= p <= 97:
            current_fg = palette[p - 90 + 8]
        elif p == 38 and i + 1 < len(items):
            mode = items[i + 1]
            if mode == 5 and i + 2 < len(items):
                current_fg = color_from_256_index(items[i + 2], palette)
                i += 2
            elif mode == 2 and i + 4 < len(items):
                r, g, b = items[i + 2:i + 5]
                current_fg = (r << 16) | (g << 8) | b
                i += 4
        i += 1
    return current_fg


def line_cells(text: str) -> int:
    ans = wcswidth(text)
    return ans if ans > 0 else 0


def render_rich_sample_text(face: Any, width: int, height: int, default_fg: int, sample_text: str, text_style: TextStyle) -> tuple[bytes, int, int]:
    _, cell_width, cell_height = face.render_sample_text('M', width, height, default_fg)
    if not cell_width or not cell_height or width <= 0 or height <= 0:
        return b'', cell_width, cell_height
    max_cols = max(1, width // cell_width)
    max_lines = max(1, height // cell_height)
    palette = ansi_palette_from_text_style(text_style, default_fg)
    normalized = sample_text.replace('\r\n', '\n').replace('\r', '\n')
    lines: list[list[tuple[str, int]]] = [[]]
    line_idx = 0
    col = 0
    current_fg = default_fg
    current_span_fg = default_fg
    current_span: list[str] = []
    stopped = False

    def flush_span() -> None:
        nonlocal current_span
        if not current_span:
            return
        text = ''.join(current_span)
        current_span = []
        line = lines[line_idx]
        if line and line[-1][1] == current_span_fg:
            prev_text, prev_fg = line[-1]
            line[-1] = (prev_text + text, prev_fg)
        else:
            line.append((text, current_span_fg))

    def new_line() -> bool:
        nonlocal line_idx, col, current_span_fg
        flush_span()
        if line_idx + 1 >= max_lines:
            return False
        lines.append([])
        line_idx += 1
        col = 0
        current_span_fg = current_fg
        return True

    pos = 0
    while pos < len(normalized) and not stopped:
        m = SGR_PATTERN.search(normalized, pos)
        segment = normalized[pos:] if m is None else normalized[pos:m.start()]
        for ch in segment:
            if ch == '\n':
                if not new_line():
                    stopped = True
                    break
                continue
            if ch == '\t':
                spaces = TAB_STOP - (col % TAB_STOP)
                for _ in range(spaces):
                    if col + 1 > max_cols and not new_line():
                        stopped = True
                        break
                    if col >= max_cols:
                        continue
                    if current_span_fg != current_fg:
                        flush_span()
                        current_span_fg = current_fg
                    current_span.append(' ')
                    col += 1
                if stopped:
                    break
                continue
            ch_width = wcswidth(ch)
            if ch_width < 0:
                ch_width = 1
            if col + ch_width > max_cols and col > 0 and not new_line():
                stopped = True
                break
            if current_span_fg != current_fg:
                flush_span()
                current_span_fg = current_fg
            current_span.append(ch)
            col += max(ch_width, 0)
        if stopped or m is None:
            break
        flush_span()
        current_fg = apply_sgr_to_fg(m.group(1), current_fg, default_fg, palette)
        current_span_fg = current_fg
        pos = m.end()
    flush_span()

    canvas_height = min(height, max(1, len(lines)) * cell_height)
    canvas = bytearray(width * canvas_height * 4)
    for y, line in enumerate(lines):
        x = 0
        y_offset = y * cell_height
        if y_offset >= canvas_height:
            break
        for text, fg in line:
            span_cells = line_cells(text)
            if span_cells <= 0:
                continue
            span_width = min(width - x, span_cells * cell_width)
            if span_width <= 0:
                break
            bitmap, _, _ = face.render_sample_text(text, span_width, cell_height, fg)
            bitmap_height = len(bitmap) // (4 * span_width) if span_width else 0
            for row in range(min(bitmap_height, canvas_height - y_offset)):
                src_start = row * span_width * 4
                src_end = src_start + span_width * 4
                dest_start = ((y_offset + row) * width + x) * 4
                dest_end = dest_start + span_width * 4
                canvas[dest_start:dest_end] = bitmap[src_start:src_end]
            x += span_width
    return bytes(canvas), cell_width, cell_height


def render_face_sample(font: Descriptor, opts: Options, dpi_x: float, dpi_y: float, width: int, height: int, sample_text: str = '', text_style: Optional[TextStyle] = None) -> RenderedSample:
    face = face_from_descriptor(font, opts.font_size, dpi_x, dpi_y)
    face.set_size(opts.font_size, dpi_x, dpi_y)
    metadata = {
        'variable_data': get_variable_data_for_face(face),
        'style': font['style'],
        'psname': face.postscript_name(),
        'features': get_features(face.get_features()),
        'applied_features': face.applied_features(),
        'spec': spec_for_face(font['family'], face).as_setting,
        'cell_width': 0, 'cell_height': 0, 'canvas_height': 0, 'canvas_width': width,
    }
    if is_variable(font):
        ns = get_named_style(face)
        if ns:
            metadata['variable_named_style'] = ns
        metadata['variable_axis_map'] = get_axis_map(face)
    text = sample_text or SAMPLE_TEXT
    if text_style is not None and any(ch in text for ch in ('\x1b', '\n', '\r', '\t')):
        bitmap, cell_width, cell_height = render_rich_sample_text(face, width, height, opts.foreground.rgb, text, text_style)
    else:
        bitmap, cell_width, cell_height = face.render_sample_text(text, width, height, opts.foreground.rgb)
    metadata['cell_width'] = cell_width
    metadata['cell_height'] = cell_height
    metadata['canvas_height'] = len(bitmap) // (4 *width)
    return bitmap, metadata


def render_family_sample(
    opts: Options, family_key: FamilyKey, dpi_x: float, dpi_y: float, width: int, height: int, output_dir: str,
    cache: dict[FaceKey, RenderedSampleTransmit], sample_text: str = '', text_style: Optional[TextStyle] = None
) -> dict[str, RenderedSampleTransmit]:
    base_key: BaseKey = opts.font_family.created_from_string, width, height
    ans: dict[str, RenderedSampleTransmit] = {}
    font_files = get_font_files(opts)
    for x in family_key:
        key: FaceKey = x + ': ' + str(getattr(opts, x)), base_key
        if x == 'font_family':
            desc = font_files['medium']
        elif x == 'bold_font':
            desc = font_files['bold']
        elif x == 'italic_font':
            desc = font_files['italic']
        elif x == 'bold_italic_font':
            desc = font_files['bi']
        cached = cache.get(key)
        if cached is not None:
            ans[x] = cached
        else:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.rgba', dir=output_dir) as tf:
                bitmap, metadata = render_face_sample(desc, opts, dpi_x, dpi_y, width, height, sample_text=sample_text, text_style=text_style)
                tf.write(bitmap)
            metadata['path'] = tf.name
            cache[key] = ans[x] = metadata
    return ans


ResolvedFace = dict[Literal['family', 'spec', 'setting'], str]


def spec_for_descriptor(d: Descriptor, font_size: float) -> str:
    face = face_from_descriptor(d, font_size, 288, 288)
    return spec_for_face(d['family'], face).as_setting


def resolved_faces(opts: Options) -> dict[OptNames, ResolvedFace]:
    font_files = get_font_files(opts)
    ans: dict[OptNames, ResolvedFace] = {}
    def d(key: Literal['medium', 'bold', 'italic', 'bi'], opt_name: OptNames) -> None:
        descriptor = font_files[key]
        ans[opt_name] = {
                'family': descriptor['family'], 'spec': spec_for_descriptor(descriptor, opts.font_size),
                'setting': getattr(opts, opt_name).created_from_string
        }
    d('medium', 'font_family')
    d('bold', 'bold_font')
    d('italic', 'italic_font')
    d('bi', 'bold_italic_font')
    return ans


def main() -> None:
    setup_debug_print()
    cache: dict[FaceKey, RenderedSampleTransmit] = {}
    for line in sys.stdin.buffer:
        cmd = json.loads(line)
        action = cmd.get('action', '')
        if action == 'list_monospaced_fonts':
            opts = create_default_opts()
            send_to_kitten({'fonts': create_family_groups(), 'resolved_faces': resolved_faces(opts)})
        elif action == 'read_variable_data':
            ans = []
            for descriptor in cmd['descriptors']:
                ans.append(get_variable_data_for_descriptor(descriptor))
            send_to_kitten(ans)
        elif action == 'render_family_samples':
            opts, family_key, dpi_x, dpi_y = opts_from_cmd(cmd)
            send_to_kitten(render_family_sample(
                opts, family_key, dpi_x, dpi_y, cmd['width'], cmd['height'], cmd['output_dir'], cache,
                sample_text=cmd.get('sample_text') or '', text_style=cmd['text_style']
            ))
        else:
            raise SystemExit(f'Unknown action: {action}')


def query_kitty() -> dict[str, str]:
    import subprocess
    ans = {}
    for line in subprocess.check_output([kitten_exe(), 'query-terminal']).decode().splitlines():
        k, sep, v = line.partition(':')
        if sep == ':':
            ans[k] = v.strip()
    return ans


def showcase(family: str = 'family="Fira Code"', sample_text: str = '') -> None:
    q = query_kitty()
    opts = Options()
    opts.foreground = to_color(q['foreground'])
    opts.background = to_color(q['background'])
    opts.font_size = float(q['font_size'])
    opts.font_family = parse_font_spec(family)
    font_files = get_font_files(opts)
    desc = font_files['medium']
    ss = screen_size_function()()
    width = ss.cell_width * ss.cols
    height = 5 * ss.cell_height
    bitmap, m = render_face_sample(desc, opts, float(q['dpi_x']), float(q['dpi_y']), width, height, sample_text=sample_text)
    display_bitmap(bitmap, m['canvas_width'], m['canvas_height'])


def test_render(spec: str = 'family="Fira Code"', width: int = 1560, height: int = 116, font_size: float = 12, dpi: float = 288) -> None:
    opts = Options()
    opts.font_family = parse_font_spec(spec)
    opts.font_size = font_size
    opts.foreground = to_color('white')
    desc = get_font_files(opts)['medium']
    bitmap, m = render_face_sample(desc, opts, float(dpi), float(dpi), width, height)
    display_bitmap(bitmap, m['canvas_width'], m['canvas_height'])
