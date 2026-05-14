"""
Binary HWP5 writer — .hwp compatible with Hangul 2010+.

Supports:
  - Paragraphs with bold / font-size / color preserved from the source PDF
  - Tables rendered as proper HWP5 table objects
  - Images embedded in the BinData OLE2 storage
"""
import struct
import zlib
import math

# ---------------------------------------------------------------------------
# OLE2 / CFB constants
# ---------------------------------------------------------------------------
_OLE2_MAGIC              = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'
_FREESECT                = 0xFFFFFFFF
_ENDOFCHAIN              = 0xFFFFFFFE
_FATSECT                 = 0xFFFFFFFD
_NOSTREAM                = 0xFFFFFFFF
_SECTOR_SZ               = 512
_DIR_ENTRY_SZ            = 128
_ENTRIES_PER_DIR_SECTOR  = _SECTOR_SZ // _DIR_ENTRY_SZ   # 4
_FAT_ENTRIES_PER_SECTOR  = _SECTOR_SZ // 4               # 128

# ---------------------------------------------------------------------------
# HWP5 tag IDs  (base = HWPTAG_BEGIN = 0x10)
# ---------------------------------------------------------------------------
TAG_DOC_PROPS       = 0x10
TAG_ID_MAP          = 0x11
TAG_BIN_DATA        = 0x12
TAG_FACENAME        = 0x13
TAG_BORDERFILL      = 0x14
TAG_CHARSHAPE       = 0x15
TAG_TABDEF          = 0x16
TAG_PARASHAPE       = 0x19
TAG_STYLE           = 0x1A
TAG_PARA_HDR        = 0x42
TAG_PARA_TEXT       = 0x43
TAG_PARA_CS         = 0x44
TAG_PARA_LS         = 0x45
TAG_CTRL_HEADER     = 0x47
TAG_LIST_HEADER     = 0x48
TAG_SHAPE_COMP      = 0x4C
TAG_TABLE           = 0x4D
TAG_SHAPE_COMP_PICT = 0x55

HWP5_SIGNATURE = b'HWP Document File' + b'\x00' * 15   # 32 bytes

# CHID values (4 ASCII chars stored reversed in file)
# CHID.decode reverses bytes: stored[3..0] → string
_CHID_TBL  = b'\x20\x6C\x62\x74'   # 'tbl '
_CHID_GSO  = b'\x20\x6F\x73\x67'   # 'gso '
_CHID_PICT = b'\x63\x69\x70\x24'   # '$pic'

# Control char 0x0B (DRAWING_TABLE_OBJECT) is EXTENDED = 8 UTF-16 units = 16 bytes
# Layout: code(2) + CHID(4) + param(8) + trailing(2)
_CTRL_CHAR_TBL  = b'\x0B\x00' + _CHID_TBL  + b'\x00' * 10
_CTRL_CHAR_GSO  = b'\x0B\x00' + _CHID_GSO  + b'\x00' * 10

# Layout units: 1 HWPUNIT ≈ 0.01 pt
# A4 page, 3 cm margins → ~150 mm content ≈ 42 520 HU
_CONTENT_W = 42520


# ---------------------------------------------------------------------------
# Color helper
# ---------------------------------------------------------------------------

def _color_to_hwp(hex_str: str) -> int:
    """#RRGGBB → Windows COLORREF uint32 (R in low byte: R | G<<8 | B<<16)."""
    h = (hex_str or '').lstrip('#')
    if len(h) == 6:
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return r | (g << 8) | (b << 16)
    return 0


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _hwp_record(tag_id: int, data: bytes, level: int = 0) -> bytes:
    size = len(data)
    if size < 0xFFF:
        return struct.pack('<I', (size << 20) | (level << 10) | tag_id) + data
    return struct.pack('<II', (0xFFF << 20) | (level << 10) | tag_id, size) + data


def _bstr(s: str) -> bytes:
    enc = s.encode('utf-16-le')
    return struct.pack('<H', len(s)) + enc


def _dir_entry(name, entry_type, color=1,
               left=_NOSTREAM, right=_NOSTREAM, child=_NOSTREAM,
               start=_ENDOFCHAIN, size=0) -> bytes:
    name_u = name.encode('utf-16-le')
    nlen   = len(name_u) + 2
    npad   = name_u + b'\x00' * (64 - len(name_u))
    return (npad[:64]
            + struct.pack('<H', nlen)
            + struct.pack('<B', entry_type)
            + struct.pack('<B', color)
            + struct.pack('<I', left)
            + struct.pack('<I', right)
            + struct.pack('<I', child)
            + b'\x00' * 16   # CLSID
            + b'\x00' * 4    # state
            + b'\x00' * 8    # created
            + b'\x00' * 8    # modified
            + struct.pack('<I', start)
            + struct.pack('<I', size)
            + b'\x00' * 4)   # size hi


def _pad_sector(data: bytes) -> bytes:
    rem = len(data) % _SECTOR_SZ
    return data + b'\x00' * (_SECTOR_SZ - rem) if rem else data


def _sectors_needed(n: int) -> int:
    return (n + _SECTOR_SZ - 1) // _SECTOR_SZ


# ---------------------------------------------------------------------------
# OLE2 builder (multi-FAT, supports arbitrarily large files)
# ---------------------------------------------------------------------------

def _build_ole2(streams: list) -> bytes:
    """Build OLE2 Compound File.  streams = [(name, bytes), ...]
    Use 'Storage/Stream' paths to nest under a sub-storage."""
    storages = {}
    for name, _ in streams:
        if '/' in name:
            stg = name.split('/')[0]
            storages.setdefault(stg, [])

    dir_entries = [None]   # index 0 = Root placeholder

    storage_idx = {}
    for stg in storages:
        idx = len(dir_entries)
        dir_entries.append(('storage', stg, idx))
        storage_idx[stg] = idx

    stream_idx = {}
    for name, data in streams:
        idx = len(dir_entries)
        dir_entries.append(('stream', name, idx, data))
        stream_idx[name] = idx

    for stg in storage_idx:
        ch = [stream_idx[n] for (n, _) in streams if n.startswith(f'{stg}/')]
        if ch:
            storages[stg] = ch

    n_dir_e = len(dir_entries)
    n_dir_s = (n_dir_e + _ENTRIES_PER_DIR_SECTOR - 1) // _ENTRIES_PER_DIR_SECTOR
    n_data_s = sum(_sectors_needed(len(d)) if d else 0 for _, d in streams)

    n_fat = 1
    while True:
        total = n_fat + n_dir_s + n_data_s
        needed = (total + _FAT_ENTRIES_PER_SECTOR - 1) // _FAT_ENTRIES_PER_SECTOR
        if needed <= n_fat:
            break
        n_fat = needed

    dir_first  = n_fat
    data_first = n_fat + n_dir_s

    stream_start = {}
    cur = data_first
    for name, data in streams:
        ns = _sectors_needed(len(data)) if data else 0
        if ns == 0:
            stream_start[name] = _ENDOFCHAIN
        else:
            stream_start[name] = cur
            cur += ns

    fat = [_FREESECT] * (n_fat * _FAT_ENTRIES_PER_SECTOR)
    for i in range(n_fat):
        fat[i] = _FATSECT
    for i in range(n_dir_s):
        s = dir_first + i
        fat[s] = s + 1 if i < n_dir_s - 1 else _ENDOFCHAIN
    for name, data in streams:
        ns = _sectors_needed(len(data)) if data else 0
        if ns == 0:
            continue
        s = stream_start[name]
        for j in range(ns):
            fat[s + j] = s + j + 1 if j < ns - 1 else _ENDOFCHAIN
    fat_bytes = struct.pack(f'<{n_fat * _FAT_ENTRIES_PER_SECTOR}I', *fat)

    def make_dir():
        parts = []
        n = len(dir_entries)
        for i, entry in enumerate(dir_entries):
            if i == 0:
                child = 1 if n > 1 else _NOSTREAM
                parts.append(_dir_entry('Root Entry', 5, child=child))
                continue
            kind = entry[0]
            if kind == 'storage':
                _, sname, _ = entry
                ch = storages.get(sname, [])
                right = i + 1 if i + 1 < n else _NOSTREAM
                parts.append(_dir_entry(sname, 1,
                                        child=ch[0] if ch else _NOSTREAM,
                                        right=right))
            else:
                _, full, _, data = entry
                sname = full.split('/')[-1]
                s  = stream_start.get(full, _ENDOFCHAIN)
                sz = len(data) if data else 0
                right = i + 1 if i + 1 < n else _NOSTREAM
                parts.append(_dir_entry(sname, 2, start=s, size=sz, right=right))
        while len(parts) % _ENTRIES_PER_DIR_SECTOR:
            parts.append(_dir_entry('', 0))
        return b''.join(parts)

    dir_bytes   = make_dir()
    difat_slots = list(range(n_fat)) + [_FREESECT] * (109 - n_fat)
    difat_bytes = struct.pack('<109I', *difat_slots)
    header = (
        _OLE2_MAGIC + b'\x00' * 16
        + struct.pack('<HH', 0x003E, 0x0003)
        + b'\xFE\xFF'
        + struct.pack('<HH', 9, 6)
        + b'\x00' * 6
        + struct.pack('<I', n_fat)
        + struct.pack('<I', dir_first)
        + b'\x00' * 4
        + struct.pack('<I', 4096)
        + struct.pack('<I', _FREESECT) + b'\x00' * 4
        + struct.pack('<I', _FREESECT) + b'\x00' * 4
        + difat_bytes + b'\x00' * 4
    )
    assert len(header) == 512
    data_bytes = b''.join(_pad_sector(d) for _, d in streams if d)
    return header + fat_bytes + dir_bytes + data_bytes


# ---------------------------------------------------------------------------
# DocInfo record builders
# ---------------------------------------------------------------------------

def _make_fileheader() -> bytes:
    data = HWP5_SIGNATURE + b'\x00\x00\x00\x05' + struct.pack('<I', 1) + b'\x00' * 216
    assert len(data) == 256
    return data


def _facename(font_name: str) -> bytes:
    return b'\x01' + _bstr(font_name)


def _border_line(stroke: int = 0, width: int = 0, color: int = 0) -> bytes:
    """StrokeType(1B) + Width(1B) + COLORREF(4B) = 6 bytes."""
    return struct.pack('<BBI', stroke & 0x1F, width & 0x1F, color)


def _borderfill(stroke: int = 0, width: int = 0, color: int = 0) -> bytes:
    """flags(2B) + 5×border(6B) + fillflags(4B) = 36 bytes."""
    bl = _border_line(stroke, width, color)
    return struct.pack('<H', 0) + bl * 4 + _border_line(0, 0, 0) + struct.pack('<I', 0)


def _charshape_rec(size_hu: int = 1000, bold: bool = False,
                   color: int = 0) -> bytes:
    """CharShape record (68 bytes)."""
    font_face = struct.pack('<7H', *([0] * 7))
    width_exp = struct.pack('<7B', *([100] * 7))
    spacing   = struct.pack('<7b', *([0]   * 7))
    rel_sz    = struct.pack('<7b', *([100] * 7))
    position  = struct.pack('<7b', *([0]   * 7))
    basesize  = struct.pack('<i', max(100, size_hu))
    cflags    = struct.pack('<I', 0b10 if bold else 0)
    shadow_sp = b'\x00\x00'
    colors    = struct.pack('<4I', color, 0, 0xFFFFFFFF, 0xC0C0C0)
    return (font_face + width_exp + spacing + rel_sz + position
            + basesize + cflags + shadow_sp + colors)


def _tabdef() -> bytes:
    return struct.pack('<II', 0, 0)


def _parashape() -> bytes:
    """ParaShape (42 bytes)."""
    flags  = struct.pack('<I', 0)
    fields = struct.pack('<6i', 0, 0, 0, 0, 0, 160)   # margins(5) + linespacing
    ids    = struct.pack('<3H', 0, 0, 0)
    bdist  = struct.pack('<4H', 0, 0, 0, 0)
    return flags + fields + ids + bdist


def _style_record(local_name: str, eng_name: str,
                  para_id: int = 0, char_id: int = 0) -> bytes:
    return (_bstr(local_name) + _bstr(eng_name)
            + struct.pack('<I', 0) + struct.pack('<B', 0)
            + struct.pack('<h', 1042)
            + struct.pack('<HHH', para_id, char_id, 0))


def _bin_data_record(storage_id: int, ext: str) -> bytes:
    """BIN_DATA record: EMBEDDING type, 1-based storage_id."""
    flags = struct.pack('<H', 1)   # bits 0-3 = EMBEDDING(1)
    return flags + struct.pack('<H', storage_id) + _bstr(ext)


def _make_docinfo(charshape_list: list, n_images: int = 0) -> bytes:
    """charshape_list: [(bold, size_hu, color_int), ...]  (index = charshape_id)"""
    n_cs = len(charshape_list) if charshape_list else 1

    doc_props = struct.pack('<7H3I', 1, 1, 1, 1, 1, 1, 1, 0, 0, 0)
    id_map = struct.pack('<15I',
        n_images,  # bindata
        1, 1, 1, 1, 1, 1, 1,   # 7 font categories (1 each)
        2,          # borderfills (transparent + solid)
        n_cs,       # charshapes
        1, 0, 0,    # tabdefs, numberings, bullets
        1, 1,       # parashapes, styles
    )

    records  = _hwp_record(TAG_DOC_PROPS, doc_props)
    records += _hwp_record(TAG_ID_MAP,    id_map)

    # BinData entries (one per image, 1-based id)
    for i in range(n_images):
        records += _hwp_record(TAG_BIN_DATA,
                               _bin_data_record(i + 1, 'png'))

    # FaceNames: 7 identical (one per language category)
    fn = _facename('굴림')
    for _ in range(7):
        records += _hwp_record(TAG_FACENAME, fn)

    # BorderFills: 1=transparent, 2=solid 0.4mm (for table cells)
    records += _hwp_record(TAG_BORDERFILL, _borderfill(0, 0, 0))
    records += _hwp_record(TAG_BORDERFILL, _borderfill(1, 6, 0))

    # CharShapes: one per unique style in the document
    if charshape_list:
        for bold, size_hu, color_int in charshape_list:
            records += _hwp_record(TAG_CHARSHAPE,
                                   _charshape_rec(size_hu, bold, color_int))
    else:
        records += _hwp_record(TAG_CHARSHAPE, _charshape_rec())

    records += _hwp_record(TAG_TABDEF,    _tabdef())
    records += _hwp_record(TAG_PARASHAPE, _parashape())
    records += _hwp_record(TAG_STYLE,     _style_record('바탕글', 'Normal', 0, 0))
    return records


# ---------------------------------------------------------------------------
# BodyText — paragraph helpers
# ---------------------------------------------------------------------------

def _para_header(text_len: int) -> bytes:
    has = 1 if text_len else 0
    return (struct.pack('<I', text_len)
            + struct.pack('<I', 0)      # control mask
            + struct.pack('<H', 0)      # parashape_id
            + struct.pack('<B', 0)      # style_id
            + struct.pack('<B', 0)      # split flags
            + struct.pack('<H', has)    # n_cs
            + struct.pack('<H', 0)      # n_rt
            + struct.pack('<H', has)    # n_ls
            + struct.pack('<I', 0))     # instance_id


def _para_char_shape(charshape_id: int = 0) -> bytes:
    return struct.pack('<II', 0, charshape_id)


def _para_line_seg(text_len: int, width: int = _CONTENT_W) -> bytes:
    return struct.pack('<9i', 0, 0, 1200, 1000, 800, 0, 0, width, 0)


def _clean(text: str) -> str:
    return ''.join(c for c in str(text) if ord(c) >= 0x20 or c == '\t')


def _text_para_records(text: str, cs_id: int = 0, level: int = 0,
                       width: int = _CONTENT_W) -> bytes:
    text = _clean(text)
    n   = len(text)
    out  = _hwp_record(TAG_PARA_HDR, _para_header(n), level)
    if n:
        out += _hwp_record(TAG_PARA_TEXT, text.encode('utf-16-le'), level)
        out += _hwp_record(TAG_PARA_CS,   _para_char_shape(cs_id),  level)
        out += _hwp_record(TAG_PARA_LS,   _para_line_seg(n, width), level)
    return out


# ---------------------------------------------------------------------------
# BodyText — table helpers
# ---------------------------------------------------------------------------

def _ctrl_header(chid: bytes, width: int, height: int,
                 flags: int = 0) -> bytes:
    """CommonControl: CHID(4) + flags(4) + y(4) + x(4) + w(4) + h(4)
       + z(2) + unk(2) + margin(8) + inst_id(4) = 40 bytes."""
    return (chid
            + struct.pack('<I', flags)
            + struct.pack('<ii', 0, 0)        # y, x  SHWPUNIT
            + struct.pack('<II', width, height)
            + struct.pack('<hh', 0, 0)        # z_order, unknown1
            + struct.pack('<4H', 0, 0, 0, 0)  # Margin
            + struct.pack('<I', 0))            # instance_id


def _table_cell_header(col: int, row: int, cw: int, ch: int) -> bytes:
    """LIST_HEADER for a table cell (38 bytes)."""
    return (struct.pack('<HHI', 1, 0, 0)          # paragraphs, unk, listflags
            + struct.pack('<HH', col, row)
            + struct.pack('<HH', 1, 1)             # colspan, rowspan
            + struct.pack('<ii', cw, ch)            # width, height SHWPUNIT
            + struct.pack('<4H', 510, 510, 142, 142)  # padding Margin
            + struct.pack('<H', 2)                  # borderfill_id=solid
            + struct.pack('<i', cw))                # unknown_width


def _table_body(n_rows: int, n_cols: int, row_h: int) -> bytes:
    return (struct.pack('<I', 0)                        # flags
            + struct.pack('<HH', n_rows, n_cols)
            + struct.pack('<H', 0)                      # cellspacing HWPUNIT16
            + struct.pack('<4H', 0, 0, 0, 0)            # padding Margin
            + struct.pack(f'<{n_rows}H', *([n_cols] * n_rows))  # rowcols
            + struct.pack('<H', 2))                     # borderfill_id


def _table_records(rows: list) -> bytes:
    n_rows = len(rows)
    n_cols = max((len(r) for r in rows), default=1)
    col_w  = _CONTENT_W // n_cols
    row_h  = 2000
    tbl_w  = col_w * n_cols

    out  = _hwp_record(TAG_PARA_HDR,  _para_header(8))
    out += _hwp_record(TAG_PARA_TEXT, _CTRL_CHAR_TBL)
    out += _hwp_record(TAG_PARA_CS,   _para_char_shape(0))
    out += _hwp_record(TAG_PARA_LS,   _para_line_seg(8, tbl_w))

    out += _hwp_record(TAG_CTRL_HEADER,
                       _ctrl_header(_CHID_TBL, tbl_w, row_h * n_rows),
                       level=0)

    for ri, row in enumerate(rows):
        for ci in range(n_cols):
            text = _clean(str(row[ci]) if ci < len(row) else '')
            out += _hwp_record(TAG_LIST_HEADER,
                               _table_cell_header(ci, ri, col_w, row_h),
                               level=1)
            out += _text_para_records(text, cs_id=0, level=2, width=col_w)

    out += _hwp_record(TAG_TABLE,
                       _table_body(n_rows, n_cols, row_h),
                       level=1)
    return out


# ---------------------------------------------------------------------------
# BodyText — image helpers
# ---------------------------------------------------------------------------

def _identity_matrix() -> bytes:
    """2D transform matrix (a,c,e,b,d,f) as 6 doubles = 48 bytes. Identity."""
    return struct.pack('<6d', 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def _shape_component(width_hu: int, height_hu: int) -> bytes:
    """ShapeComponent record for a '$pic' inside a 'gso ' control.

    chid0 present (parent is GSO), chid = '$pic', scalerotations_count = 1.
    Total size: 4+4+4+4+2+2+4+4+4+4+4+2+8+2+48+96 = 196 bytes
    """
    chid0 = _CHID_PICT      # present because parent is GShapeObjectControl
    chid  = _CHID_PICT
    x_in_group   = struct.pack('<i', 0)
    y_in_group   = struct.pack('<i', 0)
    level_in_grp = struct.pack('<H', 0)
    local_ver    = struct.pack('<H', 0)
    init_w = struct.pack('<i', width_hu)
    init_h = struct.pack('<i', height_hu)
    width  = struct.pack('<i', width_hu)
    height = struct.pack('<i', height_hu)
    flags  = struct.pack('<I', 0)
    angle  = struct.pack('<H', 0)
    rot_cx = struct.pack('<ii', 0, 0)   # Coord rotation_center
    scale_count = struct.pack('<H', 1)
    translation = _identity_matrix()
    # ScaleRotationMatrix = 2 × Matrix = 96 bytes (scaler + rotator, both identity)
    scalerot = _identity_matrix() + _identity_matrix()
    return (chid0 + chid
            + x_in_group + y_in_group
            + level_in_grp + local_ver
            + init_w + init_h + width + height
            + flags + angle + rot_cx
            + scale_count + translation + scalerot)


def _shape_picture(width_hu: int, height_hu: int, bindata_id: int) -> bytes:
    """ShapeComponentPicture record (73 bytes for v5.0.0.0).

    BorderLine(12) + ImageRect(32) + ImageClip(16) + Margin(8) + PictureInfo(5)
    """
    # BorderLine: color(4) + width(4) + flags(4) = 12 bytes (no border)
    border = struct.pack('<IiI', 0, 0, 0)

    # ImageRect: 4 corners (p0=TL, p1=TR, p2=BR, p3=BL), each Coord = 2×SHWPUNIT
    rect = struct.pack('<8i',
        0, 0,
        width_hu, 0,
        width_hu, height_hu,
        0, height_hu,
    )

    # ImageClip: left, top, right, bottom crop offsets (0 = no crop)
    clip = struct.pack('<4i', 0, 0, 0, 0)

    # Margin: left, right, top, bottom (HWPUNIT16 × 4) = 8 bytes
    padding = struct.pack('<4H', 0, 0, 0, 0)

    # PictureInfo: brightness(1) + contrast(1) + effect(1) + bindata_id(2) = 5 bytes
    pic_info = struct.pack('<BBBh', 0, 0, 0, bindata_id)
    # Wait: PictureInfo has INT8 + INT8 + BYTE + UINT16 = 5 bytes
    pic_info = struct.pack('<bbBH', 0, 0, 0, bindata_id)

    return border + rect + clip + padding + pic_info


def _image_records(width_hu: int, height_hu: int, bindata_id: int) -> bytes:
    """Generate HWP5 records for one inline picture."""
    # Paragraph containing the picture control char (8 UTF-16 positions)
    out  = _hwp_record(TAG_PARA_HDR,  _para_header(8))
    out += _hwp_record(TAG_PARA_TEXT, _CTRL_CHAR_GSO)
    out += _hwp_record(TAG_PARA_CS,   _para_char_shape(0))
    out += _hwp_record(TAG_PARA_LS,   _para_line_seg(8, width_hu))

    # CTRL_HEADER for 'gso ' (inline flag set via flags=1)
    out += _hwp_record(TAG_CTRL_HEADER,
                       _ctrl_header(_CHID_GSO, width_hu, height_hu, flags=1),
                       level=0)

    # SHAPE_COMPONENT (level=1)
    out += _hwp_record(TAG_SHAPE_COMP,
                       _shape_component(width_hu, height_hu),
                       level=1)

    # SHAPE_COMPONENT_PICTURE (level=1)
    out += _hwp_record(TAG_SHAPE_COMP_PICT,
                       _shape_picture(width_hu, height_hu, bindata_id),
                       level=1)
    return out


# ---------------------------------------------------------------------------
# Section builder
# ---------------------------------------------------------------------------

def _make_section0(items: list) -> bytes:
    """items: [('para', text, cs_id) | ('table', rows) | ('image', id, w, h)]"""
    records = b''
    for item in items:
        kind = item[0]
        if kind == 'para':
            _, text, cs_id = item
            records += _text_para_records(text, cs_id)
        elif kind == 'table':
            _, rows = item
            records += _table_records(rows)
        elif kind == 'image':
            _, img_id, w_hu, h_hu = item
            records += _image_records(w_hu, h_hu, img_id)
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HWP5Writer:
    """Write paragraphs, tables, and images to binary HWP5 (.hwp) format."""

    def __init__(self):
        self._items: list = []
        self._cs_map: dict = {}      # (bold, size_hu, color) → index (0-based)
        self._cs_list: list = []     # [(bold, size_hu, color), ...]
        self._images: list = []      # [(bytes, ext), ...]

    # -- CharShape registry --------------------------------------------------

    def _cs_id(self, bold: bool, size_pt: float, color_hex: str) -> int:
        size_hu    = max(100, int(round(size_pt * 100)))
        color_int  = _color_to_hwp(color_hex)
        key        = (bool(bold), size_hu, color_int)
        if key not in self._cs_map:
            self._cs_map[key] = len(self._cs_list)
            self._cs_list.append(key)
        return self._cs_map[key]

    # -- Public add methods --------------------------------------------------

    def add_paragraph(self, text: str,
                      bold: bool  = False,
                      size_pt: float = 10.0,
                      color: str  = '#000000',
                      **kwargs) -> None:
        cs_id = self._cs_id(bold, size_pt, color)
        self._items.append(('para', text or '', cs_id))

    def add_table(self, rows, **kwargs) -> None:
        if rows:
            self._items.append(('table', rows))

    def add_image(self, img_bytes: bytes, ext: str,
                  width_pt: float, height_pt: float, **kwargs) -> None:
        if not img_bytes:
            return
        img_id = len(self._images) + 1   # 1-based
        self._images.append((img_bytes, (ext or 'png').lower()))
        w_hu = max(100, int(round(width_pt  * 100)))
        h_hu = max(100, int(round(height_pt * 100)))
        self._items.append(('image', img_id, w_hu, h_hu))

    def add_page_break(self) -> None:
        self._items.append(('para', '', 0))

    # -- Serialisation -------------------------------------------------------

    def to_bytes(self) -> bytes:
        # Ensure at least one charshape exists
        if not self._cs_list:
            self._cs_id(False, 10.0, '#000000')

        fileheader = _make_fileheader()
        docinfo    = zlib.compress(_make_docinfo(self._cs_list, len(self._images)))
        section0   = zlib.compress(_make_section0(self._items))

        streams = [
            ('FileHeader',        fileheader),
            ('DocInfo',           docinfo),
            ('BodyText/Section0', section0),
        ]

        # Add each image as a BinData stream
        for i, (img_bytes, ext) in enumerate(self._images):
            name = f'BinData/BIN{i+1:04d}.{ext}'
            streams.append((name, img_bytes))

        return _build_ole2(streams)

    def save(self, path: str) -> None:
        from pathlib import Path
        Path(path).write_bytes(self.to_bytes())
