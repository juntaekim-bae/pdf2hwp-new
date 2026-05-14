"""
Binary HWP5 writer — generates .hwp files compatible with Hangul 2010+.

Implements OLE2 (Compound File Binary) container with HWP5 tagged records.
Supports paragraphs with basic text formatting and proper table structures.
"""
import struct
import zlib

# ---------------------------------------------------------------------------
# OLE2 / CFB constants
# ---------------------------------------------------------------------------
_OLE2_MAGIC           = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'
_FREESECT             = 0xFFFFFFFF
_ENDOFCHAIN           = 0xFFFFFFFE
_FATSECT              = 0xFFFFFFFD
_NOSTREAM             = 0xFFFFFFFF
_SECTOR_SZ            = 512
_DIR_ENTRY_SZ         = 128
_ENTRIES_PER_DIR_SECTOR = _SECTOR_SZ // _DIR_ENTRY_SZ  # 4

# ---------------------------------------------------------------------------
# HWP5 tag IDs  (HWPTAG_BEGIN = 0x10)
# ---------------------------------------------------------------------------
TAG_DOC_PROPS   = 0x10   # DocumentProperties
TAG_ID_MAP      = 0x11   # IdMappings
TAG_FACENAME    = 0x13   # FaceName
TAG_BORDERFILL  = 0x14   # BorderFill
TAG_CHARSHAPE   = 0x15   # CharShape
TAG_TABDEF      = 0x16   # TabDef
TAG_PARASHAPE   = 0x19   # ParaShape
TAG_STYLE       = 0x1A   # Style
TAG_PARA_HDR    = 0x42   # ParaHeader   (BodyText)
TAG_PARA_TEXT   = 0x43   # ParaText
TAG_PARA_CS     = 0x44   # ParaCharShape
TAG_PARA_LS     = 0x45   # ParaLineSeg
TAG_CTRL_HEADER = 0x47   # CtrlHeader
TAG_LIST_HEADER = 0x48   # ListHeader
TAG_TABLE       = 0x4D   # TableBody

HWP5_SIGNATURE = b'HWP Document File' + b'\x00' * 15   # 32 bytes

# CHID for table: 'tbl ' stored reversed as bytes
_CHID_TBL = b'\x20\x6C\x62\x74'

# Layout unit: 1 HWPUNIT ≈ 0.01pt.  A4 page, 3cm margins → ~150mm content
# 150mm × 72pt/25.4mm × 100 HWPUNIT/pt ≈ 42520
_CONTENT_W = 42520


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _hwp_record(tag_id: int, data: bytes, level: int = 0) -> bytes:
    """Encode one HWP5 record with header uint32 = (size<<20)|(level<<10)|tag."""
    size = len(data)
    if size < 0xFFF:
        hdr = (size << 20) | (level << 10) | tag_id
        return struct.pack('<I', hdr) + data
    hdr = (0xFFF << 20) | (level << 10) | tag_id
    return struct.pack('<II', hdr, size) + data


def _bstr(s: str) -> bytes:
    """UINT16 char-count + UTF-16LE (no null terminator)."""
    enc = s.encode('utf-16-le')
    return struct.pack('<H', len(s)) + enc


def _dir_entry(
    name: str,
    entry_type: int,
    color: int   = 1,
    left: int    = _NOSTREAM,
    right: int   = _NOSTREAM,
    child: int   = _NOSTREAM,
    start: int   = _ENDOFCHAIN,
    size: int    = 0,
) -> bytes:
    name_utf16 = name.encode('utf-16-le')
    name_len   = len(name_utf16) + 2
    name_pad   = name_utf16 + b'\x00' * (64 - len(name_utf16))
    return (
        name_pad[:64]
        + struct.pack('<H', name_len)
        + struct.pack('<B', entry_type)
        + struct.pack('<B', color)
        + struct.pack('<I', left)
        + struct.pack('<I', right)
        + struct.pack('<I', child)
        + b'\x00' * 16           # CLSID
        + b'\x00' * 4            # state
        + b'\x00' * 8            # created
        + b'\x00' * 8            # modified
        + struct.pack('<I', start)
        + struct.pack('<I', size)
        + b'\x00' * 4            # size hi
    )                            # total 128 bytes


def _pad_sector(data: bytes) -> bytes:
    rem = len(data) % _SECTOR_SZ
    if rem:
        data += b'\x00' * (_SECTOR_SZ - rem)
    return data


def _sectors_needed(n: int) -> int:
    return (n + _SECTOR_SZ - 1) // _SECTOR_SZ


# ---------------------------------------------------------------------------
# OLE2 builder
# ---------------------------------------------------------------------------

_FAT_ENTRIES_PER_SECTOR = _SECTOR_SZ // 4   # 128 UINT32 per 512-byte sector


def _build_ole2(streams: list) -> bytes:
    """Build minimal OLE2 file.  streams = [(name, bytes), ...]
    Names may be 'Storage/Stream' to nest under a storage.
    Supports files larger than 128 sectors via multi-sector FAT."""
    storages = {}
    for name, _ in streams:
        if '/' in name:
            stg, _ = name.split('/', 1)
            storages.setdefault(stg, [])

    dir_entries = [None]   # index 0 = root placeholder

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
        children = [stream_idx[n] for (n, d) in streams if n.startswith(f'{stg}/')]
        if children:
            storages[stg] = children

    n_dir_entries = len(dir_entries)
    n_dir_sectors = (n_dir_entries + _ENTRIES_PER_DIR_SECTOR - 1) // _ENTRIES_PER_DIR_SECTOR

    # Stream data sector counts
    stream_nsectors = {
        name: _sectors_needed(len(data)) if data else 0
        for name, data in streams
    }
    n_data_sectors = sum(stream_nsectors.values())

    # Iteratively determine number of FAT sectors needed.
    # FAT sectors precede dir and data sectors in the layout.
    n_fat = 1
    while True:
        total = n_fat + n_dir_sectors + n_data_sectors
        needed = (total + _FAT_ENTRIES_PER_SECTOR - 1) // _FAT_ENTRIES_PER_SECTOR
        if needed <= n_fat:
            break
        n_fat = needed

    # Sector layout: [fat_0 .. fat_{n_fat-1}] [dir_0 ..] [stream data ..]
    dir_first   = n_fat
    data_first  = n_fat + n_dir_sectors

    # Assign stream start sectors
    stream_start = {}
    cur = data_first
    for name, data in streams:
        ns = stream_nsectors[name]
        if ns == 0:
            stream_start[name] = _ENDOFCHAIN
        else:
            stream_start[name] = cur
            cur += ns

    # Build FAT array (n_fat * 128 entries)
    total_sectors = n_fat + n_dir_sectors + n_data_sectors
    fat = [_FREESECT] * (n_fat * _FAT_ENTRIES_PER_SECTOR)

    # Mark FAT sectors
    for i in range(n_fat):
        fat[i] = _FATSECT

    # Mark dir sectors as a chain
    for i in range(n_dir_sectors):
        s = dir_first + i
        fat[s] = s + 1 if i < n_dir_sectors - 1 else _ENDOFCHAIN

    # Mark stream data sectors
    for name, data in streams:
        ns = stream_nsectors[name]
        if ns == 0:
            continue
        s = stream_start[name]
        for j in range(ns):
            fat[s + j] = s + j + 1 if j < ns - 1 else _ENDOFCHAIN

    # Serialise FAT sectors
    fat_bytes = struct.pack(f'<{n_fat * _FAT_ENTRIES_PER_SECTOR}I', *fat)

    def make_dir_bytes():
        parts = []
        n = len(dir_entries)
        for i, entry in enumerate(dir_entries):
            if i == 0:
                child = 1 if n > 1 else _NOSTREAM
                parts.append(_dir_entry('Root Entry', 5, child=child))
                continue
            kind = entry[0]
            if kind == 'storage':
                _, stg_name, idx = entry
                children = storages.get(stg_name, [])
                child = children[0] if children else _NOSTREAM
                right = i + 1 if i + 1 < n else _NOSTREAM
                parts.append(_dir_entry(stg_name, 1, child=child, right=right))
            else:
                _, full_name, idx, data = entry
                sname = full_name.split('/')[-1]
                s  = stream_start.get(full_name, _ENDOFCHAIN)
                sz = len(data) if data else 0
                right = i + 1 if i + 1 < n else _NOSTREAM
                parts.append(_dir_entry(sname, 2, start=s, size=sz, right=right))
        while len(parts) % _ENTRIES_PER_DIR_SECTOR:
            parts.append(_dir_entry('', 0))
        return b''.join(parts)

    dir_bytes = make_dir_bytes()

    # DIFAT in header: first 109 slots list the FAT sector indices
    difat = list(range(n_fat)) + [_FREESECT] * (109 - n_fat)
    difat_bytes = struct.pack('<110I', 0xFFFFFFFE, *difat)  # first word unused
    # Actually the header DIFAT layout is exactly 110 UINT32 slots (440 bytes)
    # slot 0..108: FAT sector locations; layout fills from slot 0
    difat_list = list(range(n_fat)) + [_FREESECT] * (109 - n_fat)
    difat_bytes = struct.pack('<109I', *difat_list)

    header = (
        _OLE2_MAGIC
        + b'\x00' * 16                       # ClassID
        + struct.pack('<H', 0x003E)           # minor version
        + struct.pack('<H', 0x0003)           # major version (v3, 512B sectors)
        + b'\xFE\xFF'                         # byte order LE
        + struct.pack('<H', 9)                # sector size power (2^9=512)
        + struct.pack('<H', 6)                # mini sector size power
        + b'\x00' * 6                         # reserved
        + struct.pack('<I', n_fat)            # total FAT sectors
        + struct.pack('<I', dir_first)        # first dir sector
        + b'\x00' * 4                         # transaction sig
        + struct.pack('<I', 4096)             # mini stream cutoff
        + struct.pack('<I', _FREESECT)        # first mini FAT (none)
        + struct.pack('<I', 0)                # mini FAT sector count
        + struct.pack('<I', _FREESECT)        # first DIFAT sector (none, ≤109 FAT sectors)
        + struct.pack('<I', 0)                # DIFAT sector count
        + difat_bytes                         # 109 × 4 = 436 bytes
        + b'\x00' * 4                         # padding to reach 512
    )
    assert len(header) == 512, f'header={len(header)}'

    data_bytes = b''
    for name, data in streams:
        if data:
            data_bytes += _pad_sector(data)

    return header + fat_bytes + dir_bytes + data_bytes


# ---------------------------------------------------------------------------
# HWP5 binary helpers: DocInfo records
# ---------------------------------------------------------------------------

def _make_fileheader() -> bytes:
    sig      = HWP5_SIGNATURE
    version  = b'\x00\x00\x00\x05'   # major=5, minor=0
    flags    = struct.pack('<I', 1)   # bit 0 = compressed
    reserved = b'\x00' * 216
    data = sig + version + flags + reserved
    assert len(data) == 256
    return data


def _facename(font_name: str) -> bytes:
    """Minimal FaceName record: flags(1B) + BSTR name."""
    return b'\x01' + _bstr(font_name)


def _border_line(stroke: int = 0, width: int = 0, color: int = 0) -> bytes:
    """One border line: StrokeType(1B) + Width(1B) + COLORREF(4B) = 6 bytes."""
    return struct.pack('<BBI', stroke & 0x1F, width & 0x1F, color)


def _borderfill(
    stroke: int = 0,
    width:  int = 0,
    color:  int = 0,
) -> bytes:
    """BorderFill record: flags(2B) + 5×border(6B) + fillflags(4B) = 36 bytes."""
    borderflags = struct.pack('<H', 0)
    bl = _border_line(stroke, width, color)
    diagonal    = _border_line(0, 0, 0)   # diagonal always none
    fillflags   = struct.pack('<I', 0)    # no fill
    return borderflags + bl * 4 + diagonal + fillflags


def _charshape(font_size_hu: int = 1000, bold: bool = False) -> bytes:
    """CharShape record (68 bytes)."""
    font_face = struct.pack('<7H', 0, 0, 0, 0, 0, 0, 0)    # 14 B
    width_exp = struct.pack('<7B', *([100] * 7))             #  7 B
    spacing   = struct.pack('<7b', *([0]   * 7))             #  7 B
    rel_sz    = struct.pack('<7b', *([100] * 7))             #  7 B
    position  = struct.pack('<7b', *([0]   * 7))             #  7 B
    basesize  = struct.pack('<i', font_size_hu)              #  4 B
    cflags    = struct.pack('<I', 0b10 if bold else 0)       #  4 B
    shadow_sp = b'\x00\x00'                                  #  2 B
    colors    = struct.pack('<4I', 0, 0, 0xFFFFFFFF, 0xC0C0C0)  # 16 B
    return (font_face + width_exp + spacing + rel_sz + position
            + basesize + cflags + shadow_sp + colors)        # 68 B


def _tabdef() -> bytes:
    """TabDef: flags(4B) + count(4B) = 8 bytes."""
    return struct.pack('<II', 0, 0)


def _parashape() -> bytes:
    """ParaShape (38 bytes)."""
    flags  = struct.pack('<I', 0)
    margin = struct.pack('<6i', 0, 0, 0, 0, 0, 0)  # ml, mr, indent, mt, mb, ls
    ls     = struct.pack('<i', 160)                 # linespacing 160%
    ids    = struct.pack('<3H', 0, 0, 0)            # tabdef, numbering, borderfill
    bdist  = struct.pack('<4H', 0, 0, 0, 0)         # border distances
    return flags + struct.pack('<6i', 0, 0, 0, 0, 0, 160) + ids + bdist


def _style_record(local_name: str, eng_name: str,
                  para_id: int = 0, char_id: int = 0) -> bytes:
    return (_bstr(local_name) + _bstr(eng_name)
            + struct.pack('<I', 0)
            + struct.pack('<B', 0)
            + struct.pack('<h', 1042)   # Korean lang id
            + struct.pack('<H', para_id)
            + struct.pack('<H', char_id)
            + struct.pack('<H', 0))


def _make_docinfo() -> bytes:
    """DocInfo stream (caller compresses with zlib)."""
    doc_props = struct.pack('<7H3I',
        1, 1, 1, 1, 1, 1, 1,
        0, 0, 0,
    )
    id_map = struct.pack('<15I',
        0,   # bindata
        1,   # ko_fonts
        1,   # en_fonts
        1,   # cn_fonts
        1,   # jp_fonts
        1,   # other_fonts
        1,   # symbol_fonts
        1,   # user_fonts
        2,   # borderfills  ← 2: one transparent + one solid (for tables)
        1,   # charshapes
        1,   # tabdefs
        0,   # numberings
        0,   # bullets
        1,   # parashapes
        1,   # styles
    )

    records  = _hwp_record(TAG_DOC_PROPS, doc_props)
    records += _hwp_record(TAG_ID_MAP,    id_map)

    fn = _facename('굴림')
    for _ in range(7):
        records += _hwp_record(TAG_FACENAME, fn)

    # BorderFill id=1: transparent (for normal text)
    records += _hwp_record(TAG_BORDERFILL, _borderfill(0, 0, 0))
    # BorderFill id=2: solid 0.4mm black (for table cells)
    records += _hwp_record(TAG_BORDERFILL, _borderfill(1, 6, 0x00000000))

    records += _hwp_record(TAG_CHARSHAPE, _charshape())
    records += _hwp_record(TAG_TABDEF,    _tabdef())
    records += _hwp_record(TAG_PARASHAPE, _parashape())
    records += _hwp_record(TAG_STYLE,     _style_record('바탕글', 'Normal', 0, 0))
    return records


# ---------------------------------------------------------------------------
# HWP5 binary helpers: BodyText records
# ---------------------------------------------------------------------------

def _para_header(text_len: int) -> bytes:
    """ParaHeader (22 bytes)."""
    has = 1 if text_len else 0
    return (
        struct.pack('<I', text_len)   # char count
        + struct.pack('<I', 0)        # control mask
        + struct.pack('<H', 0)        # parashape_id
        + struct.pack('<B', 0)        # style_id
        + struct.pack('<B', 0)        # split flags
        + struct.pack('<H', has)      # n_cs
        + struct.pack('<H', 0)        # n_rt
        + struct.pack('<H', has)      # n_ls
        + struct.pack('<I', 0)        # instance_id
    )


def _para_char_shape() -> bytes:
    """Single CharShape range covering the whole paragraph (8 bytes)."""
    return struct.pack('<II', 0, 0)   # start_pos=0, charshape_id=0


def _para_line_seg(text_len: int, width: int = _CONTENT_W) -> bytes:
    """One LineSeg entry (36 bytes)."""
    return struct.pack('<9i',
        0,       # chpos
        0,       # y
        1200,    # height (~12pt)
        1000,    # height_text
        800,     # height_baseline
        0,       # space_below
        0,       # x
        width,   # width
        0,       # flags
    )


def _clean_text(text: str) -> str:
    return ''.join(
        c for c in str(text) if ord(c) >= 0x20 or c == '\t'
    )


def _text_para_records(text: str, level: int = 0) -> bytes:
    """Records for a single text paragraph."""
    text = _clean_text(text)
    n = len(text)
    out  = _hwp_record(TAG_PARA_HDR, _para_header(n), level)
    if n:
        out += _hwp_record(TAG_PARA_TEXT, text.encode('utf-16-le'), level)
        out += _hwp_record(TAG_PARA_CS,   _para_char_shape(),       level)
        out += _hwp_record(TAG_PARA_LS,   _para_line_seg(n),        level)
    return out


# ---------------------------------------------------------------------------
# Table binary helpers
# ---------------------------------------------------------------------------

# A table control char in ParaText is 0x0B (DRAWING_TABLE_OBJECT, EXTENDED).
# EXTENDED = 8 UTF-16 units = 16 bytes:
#   code(2) + CHID(4) + param(8) + trailing(2)
_TBL_CTRL_CHAR = (
    b'\x0B\x00'       # char code 0x000B
    + _CHID_TBL       # CHID 'tbl ' reversed
    + b'\x00' * 10    # param(8) + trailing(2)
)   # = 16 bytes, occupies 8 char positions


def _ctrl_header_tbl(table_w: int, table_h: int) -> bytes:
    """CTRL_HEADER record for a table (40 bytes)."""
    chid       = _CHID_TBL
    flags      = struct.pack('<I', 0)          # CommonControlFlags
    y          = struct.pack('<i', 0)          # SHWPUNIT INT32
    x          = struct.pack('<i', 0)
    width      = struct.pack('<I', table_w)    # HWPUNIT UINT32
    height     = struct.pack('<I', table_h)
    z_order    = struct.pack('<h', 0)          # INT16
    unknown1   = struct.pack('<h', 0)
    margin     = struct.pack('<4H', 0, 0, 0, 0)  # Margin (4×HWPUNIT16)
    inst_id    = struct.pack('<I', 0)
    return chid + flags + y + x + width + height + z_order + unknown1 + margin + inst_id


def _table_cell_header(col: int, row: int, cell_w: int, cell_h: int) -> bytes:
    """LIST_HEADER for a table cell (38 bytes)."""
    # ListHeader base: paragraphs, unknown1, listflags
    base = struct.pack('<HHI', 1, 0, 0)
    # TableCell extension
    ext  = (
        struct.pack('<HH', col, row)               # col, row
        + struct.pack('<HH', 1, 1)                 # colspan, rowspan
        + struct.pack('<i', cell_w)                # width  SHWPUNIT
        + struct.pack('<i', cell_h)                # height SHWPUNIT
        + struct.pack('<4H', 510, 510, 142, 142)   # padding Margin
        + struct.pack('<H', 2)                     # borderfill_id=2 (solid)
        + struct.pack('<i', cell_w)                # unknown_width
    )
    return base + ext


def _table_body(n_rows: int, n_cols: int, col_w: int, row_h: int) -> bytes:
    """TABLE record data."""
    flags      = struct.pack('<I', 0)
    rows       = struct.pack('<H', n_rows)
    cols       = struct.pack('<H', n_cols)
    cellspace  = struct.pack('<H', 0)             # HWPUNIT16
    padding    = struct.pack('<4H', 0, 0, 0, 0)  # Margin
    # rowcols: n_rows UINT16 values = number of columns in each row
    rowcols    = struct.pack(f'<{n_rows}H', *([n_cols] * n_rows))
    borderfill = struct.pack('<H', 2)
    return flags + rows + cols + cellspace + padding + rowcols + borderfill


def _table_records(rows: list) -> bytes:
    """Generate HWP5 binary records for a complete table."""
    n_rows  = len(rows)
    n_cols  = max((len(r) for r in rows), default=1)
    col_w   = _CONTENT_W // n_cols
    row_h   = 2000   # ~20pt per row
    tbl_w   = col_w * n_cols
    tbl_h   = row_h * n_rows

    out = b''

    # Paragraph that contains the table control char (8 char positions)
    out += _hwp_record(TAG_PARA_HDR,  _para_header(8))
    out += _hwp_record(TAG_PARA_TEXT, _TBL_CTRL_CHAR)
    out += _hwp_record(TAG_PARA_CS,   _para_char_shape())
    out += _hwp_record(TAG_PARA_LS,   _para_line_seg(8, tbl_w))

    # CTRL_HEADER for table (level 0)
    out += _hwp_record(TAG_CTRL_HEADER, _ctrl_header_tbl(tbl_w, tbl_h), level=0)

    # Cells first (before TABLE body), then TABLE body — pyhwp confirmed order
    for ri, row in enumerate(rows):
        for ci in range(n_cols):
            text = _clean_text(str(row[ci]) if ci < len(row) else '')
            n    = len(text)
            # LIST_HEADER for cell (level 1)
            out += _hwp_record(TAG_LIST_HEADER,
                               _table_cell_header(ci, ri, col_w, row_h),
                               level=1)
            # Paragraph inside cell (level 2)
            out += _hwp_record(TAG_PARA_HDR, _para_header(n), level=2)
            if n:
                out += _hwp_record(TAG_PARA_TEXT, text.encode('utf-16-le'), level=2)
                out += _hwp_record(TAG_PARA_CS,   _para_char_shape(),       level=2)
                out += _hwp_record(TAG_PARA_LS,   _para_line_seg(n, col_w), level=2)

    # TABLE body record (level 1, after all cells)
    out += _hwp_record(TAG_TABLE,
                       _table_body(n_rows, n_cols, col_w, row_h),
                       level=1)
    return out


def _make_section0(items: list) -> bytes:
    """BodyText/Section0 stream (caller compresses with zlib).

    items: list of ('para', text) or ('table', rows)
    """
    records = b''
    for kind, payload in items:
        if kind == 'para':
            records += _text_para_records(payload)
        elif kind == 'table':
            records += _table_records(payload)
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HWP5Writer:
    """Write content to binary HWP5 format compatible with Hangul 2010+."""

    def __init__(self):
        self._items: list = []

    def add_paragraph(self, text: str, **kwargs) -> None:
        self._items.append(('para', text if text else ''))

    def add_table(self, rows, **kwargs) -> None:
        if rows:
            self._items.append(('table', rows))

    def add_image(self, *args, **kwargs) -> None:
        pass   # image embedding in binary HWP5 not yet implemented

    def add_page_break(self) -> None:
        self._items.append(('para', ''))

    def to_bytes(self) -> bytes:
        fileheader = _make_fileheader()
        docinfo    = zlib.compress(_make_docinfo())
        section0   = zlib.compress(_make_section0(self._items))

        streams = [
            ('FileHeader',        fileheader),
            ('DocInfo',           docinfo),
            ('BodyText/Section0', section0),
        ]
        return _build_ole2(streams)

    def save(self, path: str) -> None:
        from pathlib import Path
        Path(path).write_bytes(self.to_bytes())
