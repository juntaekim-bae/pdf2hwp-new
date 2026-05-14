"""
Binary HWP5 writer — generates .hwp files compatible with Hangul 2010+.

Uses OLE2 (Compound File Binary) container with HWP5 record streams.
"""
import struct
import zlib
import io

# ---------------------------------------------------------------------------
# OLE2 / CFB constants
# ---------------------------------------------------------------------------
_OLE2_MAGIC   = b'\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1'
_FREESECT     = 0xFFFFFFFF
_ENDOFCHAIN   = 0xFFFFFFFE
_FATSECT      = 0xFFFFFFFD
_NOSTREAM     = 0xFFFFFFFF
_SECTOR_SZ    = 512
_DIR_ENTRY_SZ = 128
_ENTRIES_PER_DIR_SECTOR = _SECTOR_SZ // _DIR_ENTRY_SZ   # 4

# ---------------------------------------------------------------------------
# HWP5 tag IDs (from pyhwp / tagids.py)
# ---------------------------------------------------------------------------
TAG_DOC_PROPS    = 0x10   # DocumentProperties  (DocInfo)
TAG_ID_MAP       = 0x11   # IdMappings
TAG_FACENAME     = 0x13   # FaceName
TAG_BORDERFILL   = 0x14   # BorderFill
TAG_CHARSHAPE    = 0x15   # CharShape
TAG_TABDEF       = 0x16   # TabDef
TAG_PARASHAPE    = 0x19   # ParaShape
TAG_STYLE        = 0x1A   # Style
TAG_PARA_HDR     = 0x42   # Paragraph (Para header)  (BodyText)
TAG_PARA_TEXT    = 0x43   # ParaText
TAG_PARA_CS      = 0x44   # ParaCharShape
TAG_PARA_LS      = 0x45   # ParaLineSeg

HWP5_SIGNATURE = b'HWP Document File' + b'\x00' * 15   # 32 bytes


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _hwp_record(tag_id: int, data: bytes, level: int = 0) -> bytes:
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
    entry_type: int,     # 0=empty 1=storage 2=stream 5=root
    color: int   = 1,    # 0=red 1=black
    left: int    = _NOSTREAM,
    right: int   = _NOSTREAM,
    child: int   = _NOSTREAM,
    start: int   = _ENDOFCHAIN,
    size: int    = 0,
) -> bytes:
    name_utf16 = name.encode('utf-16-le')
    name_len   = len(name_utf16) + 2          # includes null terminator
    name_pad   = name_utf16 + b'\x00' * (64 - len(name_utf16))
    return (
        name_pad[:64]                         # 64 bytes
        + struct.pack('<H', name_len)         # 2
        + struct.pack('<B', entry_type)       # 1
        + struct.pack('<B', color)            # 1
        + struct.pack('<I', left)             # 4
        + struct.pack('<I', right)            # 4
        + struct.pack('<I', child)            # 4
        + b'\x00' * 16                        # CLSID  16
        + b'\x00' * 4                         # state  4
        + b'\x00' * 8                         # created 8
        + b'\x00' * 8                         # modified 8
        + struct.pack('<I', start)            # 4
        + struct.pack('<I', size)             # 4 (size lo)
        + b'\x00' * 4                         # 4 (size hi, v3 unused)
    )                                         # total 128


def _pad_sector(data: bytes) -> bytes:
    """Pad data to a multiple of _SECTOR_SZ with zeros."""
    rem = len(data) % _SECTOR_SZ
    if rem:
        data += b'\x00' * (_SECTOR_SZ - rem)
    return data


def _sectors_needed(data_len: int) -> int:
    return (data_len + _SECTOR_SZ - 1) // _SECTOR_SZ


# ---------------------------------------------------------------------------
# OLE2 file builder
# ---------------------------------------------------------------------------

def _build_ole2(streams: list) -> bytes:
    """
    Build a minimal OLE2 compound file.

    streams: list of (name, data_bytes) tuples.
      Name can be 'StorageName/StreamName' to nest under a storage.
    Returns bytes of the complete OLE2 file.
    """
    # Collect storages and streams
    storages = {}    # storage_name → child indices
    stream_data = {} # dir_index → data

    # Build directory entry list
    # Entry 0 = Root
    dir_entries = [None]  # placeholder for root

    # Identify storages
    for name, data in streams:
        if '/' in name:
            stg, _ = name.split('/', 1)
            storages.setdefault(stg, [])

    # Assign indices: storages first, then streams
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

    # Link children into each storage
    for stg, s_idx in storage_idx.items():
        children = [stream_idx[n]
                    for (n, d) in streams if n.startswith(f'{stg}/')]
        if children:
            storages[stg] = children

    # Lay out data sectors for each stream
    # sector numbering: sector 0 = first sector after header
    # We'll place: FAT sector(s), dir sector(s), then stream data

    # First pass: determine sizes
    stream_bytes = {}
    for name, data in streams:
        stream_bytes[name] = data

    # --- plan sectors ---
    # sector 0: FAT
    # sector 1+: dir entries (ceil(len(dir_entries) / 4) sectors)
    # then stream data

    n_dir_entries = len(dir_entries)
    n_dir_sectors = (n_dir_entries + _ENTRIES_PER_DIR_SECTOR - 1) // _ENTRIES_PER_DIR_SECTOR

    # start sector for each stream
    cur_sector = 1 + n_dir_sectors   # sector 0 = FAT, sectors 1..n_dir_sectors-1 = dir

    stream_start = {}
    stream_sectors = {}
    for name, data in streams:
        n = _sectors_needed(len(data)) if data else 0
        if n == 0:
            stream_start[name] = _ENDOFCHAIN
            stream_sectors[name] = 0
        else:
            stream_start[name] = cur_sector
            stream_sectors[name] = n
            cur_sector += n

    total_sectors = cur_sector   # excluding header (sector -1)

    # --- build FAT ---
    fat = [_FREESECT] * _SECTOR_SZ   # 128 entries per sector
    fat[0] = _FATSECT          # sector 0 = FAT sector itself

    # dir sectors chain: sectors 1 .. n_dir_sectors
    for i in range(1, 1 + n_dir_sectors):
        fat[i] = i + 1 if i < n_dir_sectors else _ENDOFCHAIN

    # stream data sectors
    for name, data in streams:
        if not data:
            continue
        s = stream_start[name]
        n = stream_sectors[name]
        for j in range(n):
            fat[s + j] = s + j + 1 if j < n - 1 else _ENDOFCHAIN

    fat_bytes = struct.pack('<128I', *fat[:128])

    # --- build directory sectors ---
    # Sort RB-tree: we use a trivial "all black" flat tree
    # Root entry: child = index 1 (first non-root)
    # We don't do a proper RB tree; just a linear right-child chain
    # This is not spec-compliant but works for simple readers

    def make_dir_bytes():
        parts = []
        n = len(dir_entries)
        for i, entry in enumerate(dir_entries):
            if i == 0:
                # Root entry: child = first real entry (if any)
                child = 1 if n > 1 else _NOSTREAM
                parts.append(_dir_entry('Root Entry', 5, child=child))
                continue

            kind = entry[0]
            if kind == 'storage':
                _, stg_name, idx = entry
                children = storages.get(stg_name, [])
                child = children[0] if children else _NOSTREAM
                # right sibling = next entry at same level
                right = i + 1 if i + 1 < n and dir_entries[i+1][0] != 'storage-child' else _NOSTREAM
                # Simple: no siblings within root (all siblings linked via root child)
                parts.append(_dir_entry(stg_name, 1, child=child,
                                        right=right if i + 1 < n else _NOSTREAM))
            else:  # stream
                _, full_name, idx, data = entry
                name = full_name.split('/')[-1]
                s = stream_start.get(full_name, _ENDOFCHAIN)
                sz = len(data) if data else 0
                right = i + 1 if i + 1 < n else _NOSTREAM
                parts.append(_dir_entry(name, 2, start=s, size=sz, right=right))

        # Pad to multiple of 4 entries
        while len(parts) % _ENTRIES_PER_DIR_SECTOR:
            parts.append(_dir_entry('', 0))

        return b''.join(parts)

    dir_bytes = make_dir_bytes()

    # --- build OLE2 header (512 bytes = sector -1) ---
    difat_entries = [0] + [_FREESECT] * 109   # sector 0 = FAT
    difat_bytes = struct.pack('<110I', *difat_entries)

    header = (
        _OLE2_MAGIC           # 8 bytes
        + b'\x00' * 16        # ClassID
        + struct.pack('<H', 0x003E)   # minor version
        + struct.pack('<H', 0x0003)   # major version (3 = 512-byte sectors)
        + b'\xFE\xFF'                 # byte order mark (LE)
        + struct.pack('<H', 9)        # sector size power (2^9 = 512)
        + struct.pack('<H', 6)        # mini sector size power (2^6 = 64)
        + b'\x00' * 6                 # reserved
        + struct.pack('<I', 1)        # FAT sector count = 1
        + struct.pack('<I', 1)        # first dir sector = 1
        + b'\x00' * 4                 # transaction signature
        + struct.pack('<I', 4096)     # mini stream cutoff
        + struct.pack('<I', _FREESECT)  # first mini FAT sector (none)
        + b'\x00' * 4                 # mini FAT count = 0
        + struct.pack('<I', _FREESECT)  # first DIFAT sector (none)
        + b'\x00' * 4                 # DIFAT count = 0
        + difat_bytes                 # 110 × 4 = 440 bytes
    )
    assert len(header) == 512, f'header size = {len(header)}'

    # --- assemble stream data ---
    data_bytes = b''
    for name, data in streams:
        if data:
            data_bytes += _pad_sector(data)

    return header + fat_bytes + dir_bytes + data_bytes


# ---------------------------------------------------------------------------
# HWP5 stream builders
# ---------------------------------------------------------------------------

def _make_fileheader() -> bytes:
    """FileHeader stream: 256 bytes."""
    sig      = HWP5_SIGNATURE          # 32 bytes
    # version bytes stored reversed: [build, micro, minor, major]
    version  = b'\x00\x00\x00\x05'    # → (major=5, minor=0, micro=0, build=0)
    # flags bit 0 = compressed (BodyText streams are zlib-deflated)
    flags    = struct.pack('<I', 1)    # compressed = True
    reserved = b'\x00' * 216
    data = sig + version + flags + reserved
    assert len(data) == 256
    return data


def _facename(font_name: str) -> bytes:
    """Minimal FaceName record body."""
    # flags byte: bits 0-1 = font type (1 = TTF), no alternate/metric/default
    return b'\x01' + _bstr(font_name)


def _borderfill_none() -> bytes:
    """BorderFill with no visible borders."""
    # UINT16 attr (threeD=0, shadow=0, centerLine=0, breakSep=0)
    attr = struct.pack('<H', 0)
    # 5 border lines (left/right/top/bottom/diagonal): type(UINT16) + color(UINT32) + width(UINT16) = 8 bytes
    border = struct.pack('<HIH', 0, 0, 0)
    borders = border * 5               # 40 bytes
    # Fill: UINT32 type=0 (none), then UINT32 fill_color, then UINT32 shade_color
    fill = struct.pack('<III', 0, 0xFFFFFFFF, 0xFFFFFFFF)   # 12 bytes
    return attr + borders + fill       # 54 bytes


def _charshape(font_size_hu: int = 1000, bold: bool = False) -> bytes:
    """CharShape record body (basic 10pt black text)."""
    # FontFace × 7 languages (WORD each): all 0 (font index 0)
    font_face  = struct.pack('<7H', 0, 0, 0, 0, 0, 0, 0)
    # LetterWidthExpansion × 7 (UINT8): 100%
    width_exp  = struct.pack('<7B', 100, 100, 100, 100, 100, 100, 100)
    # LetterSpacing × 7 (INT8): 0%
    spacing    = struct.pack('<7b', 0, 0, 0, 0, 0, 0, 0)
    # RelativeSize × 7 (INT8): 100%
    rel_sz     = struct.pack('<7b', 100, 100, 100, 100, 100, 100, 100)
    # Position × 7 (INT8): 0
    position   = struct.pack('<7b', 0, 0, 0, 0, 0, 0, 0)
    # basesize (INT32 HWPUNIT): 1000 = 10pt
    basesize   = struct.pack('<i', font_size_hu)
    # charshapeflags (UINT32): bit 1 = bold
    cflags     = struct.pack('<I', 0b10 if bold else 0)
    # ShadowSpace (INT8 x, y)
    shadow_sp  = b'\x00\x00'
    # Colors: text, underline, shade, shadow (COLORREF = UINT32 each)
    text_color = struct.pack('<I', 0x00000000)      # black
    ul_color   = struct.pack('<I', 0x00000000)
    shade_clr  = struct.pack('<I', 0xFFFFFFFF)
    shadow_clr = struct.pack('<I', 0x00C0C0C0)
    return (font_face + width_exp + spacing + rel_sz + position
            + basesize + cflags + shadow_sp
            + text_color + ul_color + shade_clr + shadow_clr)


def _tabdef() -> bytes:
    """Minimal TabDef (no custom tab stops)."""
    # flags (UINT32) + count (UINT32) = 8 bytes
    return struct.pack('<II', 0, 0)


def _parashape() -> bytes:
    """Minimal ParaShape (left-justified, 160% line spacing)."""
    # Flags (UINT32): alignment=0(JUSTIFY), vertical=0(BASELINE), etc.
    ps_flags   = struct.pack('<I', 0)
    # doubled_margin_left/right (INT32): 0
    ml = struct.pack('<i', 0)
    mr = struct.pack('<i', 0)
    # indent (SHWPUNIT = INT32): 0
    indent     = struct.pack('<i', 0)
    # doubled_margin_top/bottom (INT32): 0
    mt = struct.pack('<i', 0)
    mb = struct.pack('<i', 0)
    # linespacing (SHWPUNIT = INT32): 160 (treated as percent by default)
    ls         = struct.pack('<i', 160)
    # tabdef_id, numbering_bullet_id, borderfill_id (UINT16 each): all 0
    ids        = struct.pack('<HHH', 0, 0, 0)
    # border distances (HWPUNIT16 = UINT16 × 4): all 0
    borders    = struct.pack('<4H', 0, 0, 0, 0)
    return ps_flags + ml + mr + indent + mt + mb + ls + ids + borders


def _style_record(local_name: str, eng_name: str,
                  para_id: int = 0, char_id: int = 0) -> bytes:
    """Style record body."""
    return (_bstr(local_name) + _bstr(eng_name)
            + struct.pack('<I', 0)      # flags: paragraph style
            + struct.pack('<B', 0)      # next_style_id
            + struct.pack('<h', 1042)   # lang_id: Korean
            + struct.pack('<H', para_id)
            + struct.pack('<H', char_id)
            + struct.pack('<H', 0))     # unknown


def _make_docinfo() -> bytes:
    """DocInfo stream (uncompressed records; caller will zlib-compress)."""
    # --- DocumentProperties ---
    doc_props = struct.pack('<7H3I',
        1, 1, 1, 1, 1, 1, 1,   # section/page/footnote/endnote/pic/tbl/math startnum
        0, 0, 0,                # list_id, paragraph_id, char_unit_loc
    )

    # --- IdMappings ---
    # 7 font categories each with 1 font, 1 borderfill, 1 charshape,
    # 1 tabdef, 0 numberings, 0 bullets, 1 parashape, 1 style
    id_map = struct.pack('<15I',
        0,   # bindata
        1,   # ko_fonts
        1,   # en_fonts
        1,   # cn_fonts
        1,   # jp_fonts
        1,   # other_fonts
        1,   # symbol_fonts
        1,   # user_fonts
        1,   # borderfills
        1,   # charshapes
        1,   # tabdefs
        0,   # numberings
        0,   # bullets
        1,   # parashapes
        1,   # styles
    )

    records = (
        _hwp_record(TAG_DOC_PROPS,  doc_props)
        + _hwp_record(TAG_ID_MAP,   id_map)
    )

    # 7 identical FaceName records (same font for all language categories)
    fn = _facename('굴림')
    for _ in range(7):
        records += _hwp_record(TAG_FACENAME, fn)

    records += _hwp_record(TAG_BORDERFILL, _borderfill_none())
    records += _hwp_record(TAG_CHARSHAPE,  _charshape())
    records += _hwp_record(TAG_TABDEF,     _tabdef())
    records += _hwp_record(TAG_PARASHAPE,  _parashape())
    records += _hwp_record(TAG_STYLE,
                           _style_record('바탕글', 'Normal', 0, 0))
    return records


def _para_header(text_len: int) -> bytes:
    """ParaHeader record body (22 bytes)."""
    # Flags: low 31 bits = char count (UINT32)
    text_field   = struct.pack('<I', text_len)
    control_mask = struct.pack('<I', 0)
    para_shape   = struct.pack('<H', 0)    # parashape_id = 0
    style_id     = struct.pack('<B', 0)
    split_flags  = struct.pack('<B', 0)
    n_cs         = struct.pack('<H', 1 if text_len else 0)  # char shape ranges
    n_rt         = struct.pack('<H', 0)                     # range tags
    n_ls         = struct.pack('<H', 1 if text_len else 0)  # line segs
    inst_id      = struct.pack('<I', 0)
    return (text_field + control_mask + para_shape + style_id
            + split_flags + n_cs + n_rt + n_ls + inst_id)


def _para_char_shape() -> bytes:
    """One CharShape range covering the whole paragraph (8 bytes)."""
    # start_pos(UINT32) = 0, charshape_id(UINT32) = 0
    return struct.pack('<II', 0, 0)


def _para_line_seg(text_len: int) -> bytes:
    """One LineSeg entry for the paragraph (36 bytes)."""
    # chpos(INT32), y, height, height_text, height_baseline,
    # space_below, x, width, flags (all INT32)
    return struct.pack('<9i',
        0,      # chpos
        0,      # y
        1200,   # height (12pt line)
        1000,   # height_text
        800,    # height_baseline
        0,      # space_below
        0,      # x
        42520,  # width (A4 content width)
        0,      # lineseg_flags
    )


def _make_section0(paragraphs: list) -> bytes:
    """BodyText/Section0 stream (uncompressed; caller will zlib-compress)."""
    records = b''
    for text in paragraphs:
        text = str(text) if text is not None else ''
        # Strip control chars
        text = ''.join(c for c in text
                       if ord(c) >= 0x20 or c in '\t'
                       and ord(c) not in (0xFFFE, 0xFFFF))
        n = len(text)
        records += _hwp_record(TAG_PARA_HDR, _para_header(n))
        if n:
            records += _hwp_record(TAG_PARA_TEXT, text.encode('utf-16-le'))
            records += _hwp_record(TAG_PARA_CS,   _para_char_shape())
            records += _hwp_record(TAG_PARA_LS,   _para_line_seg(n))
    return records


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class HWP5Writer:
    """Write paragraphs to binary HWP5 format compatible with Hangul 2010+."""

    def __init__(self):
        self._paragraphs: list = []   # list of str

    def add_paragraph(self, text: str, **kwargs) -> None:
        self._paragraphs.append(text if text else '')

    def add_table(self, rows, **kwargs) -> None:
        for row in rows:
            self._paragraphs.append('\t'.join(str(c) for c in row))

    def add_image(self, *args, **kwargs) -> None:
        pass   # images not supported in binary HWP5 writer

    def add_page_break(self) -> None:
        self._paragraphs.append('')

    def to_bytes(self) -> bytes:
        fileheader = _make_fileheader()
        docinfo    = zlib.compress(_make_docinfo())
        section0   = zlib.compress(_make_section0(self._paragraphs))

        streams = [
            ('FileHeader',         fileheader),
            ('DocInfo',            docinfo),
            ('BodyText/Section0',  section0),
        ]
        return _build_ole2(streams)

    def save(self, path: str) -> None:
        from pathlib import Path
        Path(path).write_bytes(self.to_bytes())
