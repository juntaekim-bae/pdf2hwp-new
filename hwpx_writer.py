"""
HWPX document writer — Python 3.9+ compatible.
Generates HWPX (Hancom OWPML) ZIP archives that open in Hancom Hangul 2014+.
"""
import io
import re
import sys
import zipfile
import html as _html
from pathlib import Path
from typing import List, Optional


def _resource(filename: str) -> Path:
    """PyInstaller 번들 또는 일반 실행 모두에서 리소스 경로를 반환."""
    if hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS) / filename
    return Path(__file__).parent / filename

# HWPUNIT: 1/7200 inch; 1pt = 100 HU; 1mm ≈ 283 HU
MM = 283.46
PT = 100
A4_W, A4_H = 59528, 84186
MARGIN_L = MARGIN_R = 8504
MARGIN_T = 5668
MARGIN_B = 4252
MARGIN_HDR = MARGIN_FTR = 4252
CONTENT_W = A4_W - MARGIN_L - MARGIN_R  # 42520 HU

# Base charPr IDs 0-6 come from template header.xml (do not change values)
CS_NORMAL = 0   # 10pt black
CS_BALONG = 1   # 10pt 바탕
CS_SMALL  = 2   #  9pt
CS_SMALL2 = 3   #  9pt 바탕
CS_TINY   = 4   #  9pt tight
CS_HEAD   = 5   # 16pt blue heading
CS_MED    = 6   # 11pt

_CHARPR_BASE = 7   # dynamic IDs start here

PP_BODY  = 0
BF_NONE  = 1
BF_TABLE = 2


def _e(text: str) -> str:
    # Strip XML 1.0 illegal control chars; keep tab/LF/CR and printable chars
    cleaned = ''.join(
        c for c in str(text)
        if (ord(c) >= 0x20 or c in '\t\n\r')
        and ord(c) not in (0xFFFE, 0xFFFF)
        and c != '�'
    )
    return _html.escape(cleaned, quote=False)


def _color_hex(color_int) -> str:
    """Convert PyMuPDF integer color to '#RRGGBB' string."""
    if color_int is None:
        return '#000000'
    c = int(color_int)
    if c < 0:
        c = 0xFFFFFF + c + 1
    r = (c >> 16) & 0xFF
    g = (c >> 8) & 0xFF
    b = c & 0xFF
    return f'#{r:02X}{g:02X}{b:02X}'


# ---------------------------------------------------------------------------
# Namespace string shared across XML files
# ---------------------------------------------------------------------------
_NS = (
    'xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" '
    'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" '
    'xmlns:hp10="http://www.hancom.co.kr/hwpml/2016/paragraph" '
    'xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section" '
    'xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
    'xmlns:hh="http://www.hancom.co.kr/hwpml/2011/head" '
    'xmlns:hhs="http://www.hancom.co.kr/hwpml/2011/history" '
    'xmlns:hm="http://www.hancom.co.kr/hwpml/2011/master-page" '
    'xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:opf="http://www.idpf.org/2007/opf/" '
    'xmlns:ooxmlchart="http://www.hancom.co.kr/hwpml/2016/ooxmlchart" '
    'xmlns:hwpunitchar="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar" '
    'xmlns:epub="http://www.idpf.org/2007/ops" '
    'xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0"'
)

# ---------------------------------------------------------------------------
# Static XML files
# ---------------------------------------------------------------------------
_CONTAINER_XML = """\
<?xml version='1.0' encoding='UTF-8'?>
<ocf:container xmlns:ocf="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:hpf="http://www.hancom.co.kr/schema/2011/hpf">
  <ocf:rootfiles>
    <ocf:rootfile full-path="Contents/content.hpf" media-type="application/hwpml-package+xml"/>
    <ocf:rootfile full-path="Preview/PrvText.txt" media-type="text/plain"/>
    <ocf:rootfile full-path="META-INF/container.rdf" media-type="application/rdf+xml"/>
  </ocf:rootfiles>
</ocf:container>"""

_CONTAINER_RDF = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description rdf:about="">'
    '<ns0:hasPart xmlns:ns0="http://www.hancom.co.kr/hwpml/2016/meta/pkg#" rdf:resource="Contents/header.xml"/>'
    '</rdf:Description>'
    '<rdf:Description rdf:about="Contents/header.xml">'
    '<rdf:type rdf:resource="http://www.hancom.co.kr/hwpml/2016/meta/pkg#HeaderFile"/>'
    '</rdf:Description>'
    '<rdf:Description rdf:about="">'
    '<ns0:hasPart xmlns:ns0="http://www.hancom.co.kr/hwpml/2016/meta/pkg#" rdf:resource="Contents/section0.xml"/>'
    '</rdf:Description>'
    '<rdf:Description rdf:about="Contents/section0.xml">'
    '<rdf:type rdf:resource="http://www.hancom.co.kr/hwpml/2016/meta/pkg#SectionFile"/>'
    '</rdf:Description>'
    '<rdf:Description rdf:about="">'
    '<rdf:type rdf:resource="http://www.hancom.co.kr/hwpml/2016/meta/pkg#Document"/>'
    '</rdf:Description>'
    '</rdf:RDF>'
)

_MANIFEST_XML = """\
<?xml version='1.0' encoding='UTF-8'?>
<odf:manifest xmlns:odf="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"/>"""

_CONTENT_HPF = f"""\
<?xml version='1.0' encoding='UTF-8'?>
<opf:package {_NS} version="" unique-identifier="" id="">
  <opf:metadata>
    <opf:title/>
    <opf:language>ko</opf:language>
    <opf:meta name="creator" content="pdf_to_hwpx"/>
    <opf:meta name="CreatedDate" content=""/>
    <opf:meta name="ModifiedDate" content=""/>
  </opf:metadata>
  <opf:manifest>
    <opf:item id="header" href="Contents/header.xml" media-type="application/xml"/>
    <opf:item id="section0" href="Contents/section0.xml" media-type="application/xml"/>
    <opf:item id="settings" href="settings.xml" media-type="application/xml"/>
  </opf:manifest>
  <opf:spine>
    <opf:itemref idref="header" linear="yes"/>
    <opf:itemref idref="section0" linear="yes"/>
  </opf:spine>
</opf:package>"""

_VERSION_XML = (
    "<?xml version='1.0' encoding='UTF-8'?>"
    '<hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version" '
    'targetApplication="WORDPROCESSOR" major="5" minor="0" micro="0" '
    'buildNumber="0" os="1" xmlVersion="1.0" '
    'application="Hancom Hangul" appVersion="10, 0, 0, 0"/>'
)

_SETTINGS_XML = """\
<?xml version='1.0' encoding='UTF-8'?>
<ha:HWPApplicationSetting xmlns:ha="http://www.hancom.co.kr/hwpml/2011/app" xmlns:config="urn:oasis:names:tc:opendocument:xmlns:config:1.0">
  <ha:CaretPosition listIDRef="0" paraIDRef="0" pos="0"/>
</ha:HWPApplicationSetting>"""

_FIRST_PARA = """\
  <hp:p id="1" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">
    <hp:run charPrIDRef="0">
      <hp:secPr id="" textDirection="HORIZONTAL" spaceColumns="1134" tabStop="8000" tabStopVal="4000" tabStopUnit="HWPUNIT" outlineShapeIDRef="1" memoShapeIDRef="0" textVerticalWidthHead="0" masterPageCnt="0">
        <hp:grid lineGrid="0" charGrid="0" wonggojiFormat="0"/>
        <hp:startNum pageStartsOn="BOTH" page="0" pic="0" tbl="0" equation="0"/>
        <hp:visibility hideFirstHeader="0" hideFirstFooter="0" hideFirstMasterPage="0" border="SHOW_ALL" fill="SHOW_ALL" hideFirstPageNum="0" hideFirstEmptyLine="0" showLineNumber="0"/>
        <hp:lineNumberShape restartType="0" countBy="0" distance="0" startNumber="0"/>
        <hp:pagePr landscape="WIDELY" width="59528" height="84186" gutterType="LEFT_ONLY">
          <hp:margin header="4252" footer="4252" gutter="0" left="8504" right="8504" top="5668" bottom="4252"/>
        </hp:pagePr>
        <hp:footNotePr>
          <hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>
          <hp:noteLine length="-1" type="SOLID" width="0.12 mm" color="#000000"/>
          <hp:noteSpacing betweenNotes="283" belowLine="567" aboveLine="850"/>
          <hp:numbering type="CONTINUOUS" newNum="1"/>
          <hp:placement place="EACH_COLUMN" beneathText="0"/>
        </hp:footNotePr>
        <hp:endNotePr>
          <hp:autoNumFormat type="DIGIT" userChar="" prefixChar="" suffixChar=")" supscript="0"/>
          <hp:noteLine length="14692344" type="SOLID" width="0.12 mm" color="#000000"/>
          <hp:noteSpacing betweenNotes="0" belowLine="567" aboveLine="850"/>
          <hp:numbering type="CONTINUOUS" newNum="1"/>
          <hp:placement place="END_OF_DOCUMENT" beneathText="0"/>
        </hp:endNotePr>
        <hp:pageBorderFill type="BOTH" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">
          <hp:offset left="1417" right="1417" top="1417" bottom="1417"/>
        </hp:pageBorderFill>
        <hp:pageBorderFill type="EVEN" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">
          <hp:offset left="1417" right="1417" top="1417" bottom="1417"/>
        </hp:pageBorderFill>
        <hp:pageBorderFill type="ODD" borderFillIDRef="1" textBorder="PAPER" headerInside="0" footerInside="0" fillArea="PAPER">
          <hp:offset left="1417" right="1417" top="1417" bottom="1417"/>
        </hp:pageBorderFill>
      </hp:secPr>
      <hp:ctrl>
        <hp:colPr id="" type="NEWSPAPER" layout="LEFT" colCount="1" sameSz="1" sameGap="0"/>
      </hp:ctrl>
    </hp:run>
    <hp:run charPrIDRef="0">
      <hp:t/>
    </hp:run>
  </hp:p>"""


# ---------------------------------------------------------------------------
# Dynamic charPr / binItems builders
# ---------------------------------------------------------------------------

def _charpr_entry_xml(cid: int, bold: bool, height: int, color: str) -> str:
    bold_tag = '        <hh:bold/>\n' if bold else ''
    return (
        f'      <hh:charPr id="{cid}" height="{height}" textColor="{color}" '
        f'shadeColor="none" useFontSpace="0" useKerning="0" symMark="NONE" borderFillIDRef="2">\n'
        f'        <hh:fontRef hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>\n'
        f'        <hh:ratio hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/>\n'
        f'        <hh:spacing hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>\n'
        f'        <hh:relSz hangul="100" latin="100" hanja="100" japanese="100" other="100" symbol="100" user="100"/>\n'
        f'        <hh:offset hangul="0" latin="0" hanja="0" japanese="0" other="0" symbol="0" user="0"/>\n'
        f'{bold_tag}'
        f'        <hh:underline type="NONE" shape="SOLID" color="#000000"/>\n'
        f'        <hh:strikeout shape="NONE" color="#000000"/>\n'
        f'        <hh:outline type="NONE"/>\n'
        f'        <hh:shadow type="NONE" color="#C0C0C0" offsetX="10" offsetY="10"/>\n'
        f'      </hh:charPr>\n'
    )


def _patch_header(raw: str, charpr_map: dict, images: list) -> str:
    """Inject dynamic charPr entries and binItems into header.xml."""
    total = _CHARPR_BASE + len(charpr_map)
    # Update itemCnt for charProperties
    raw = re.sub(
        r'(<hh:charProperties[^>]*itemCnt=")[^"]*(")',
        lambda m: m.group(1) + str(total) + m.group(2),
        raw,
    )
    # Inject new charPr entries before closing tag
    if charpr_map:
        extra = ''.join(
            _charpr_entry_xml(cid, bold, height, color)
            for (bold, height, color), cid in sorted(charpr_map.items(), key=lambda x: x[1])
        )
        raw = raw.replace('    </hh:charProperties>', extra + '    </hh:charProperties>', 1)

    # Inject binItems before </hh:refList>
    if images:
        items_xml = '\n'.join(
            f'    <hh:binItem id="{bid}" type="EMBEDDED" format="{ext}" compress="YES" filename="BIN{bid:03d}.{ext}"/>'
            for bid, _bytes, ext, _w, _h in images
        )
        bin_block = (
            f'  <hh:binItems itemCnt="{len(images)}">\n'
            f'{items_xml}\n'
            f'  </hh:binItems>\n'
        )
        raw = raw.replace('  </hh:refList>', bin_block + '  </hh:refList>', 1)

    return raw


# ---------------------------------------------------------------------------
# HWPXWriter
# ---------------------------------------------------------------------------

class HWPXWriter:
    def __init__(self, header_xml_path: Optional[str] = None):
        self._items: list = []
        self._pid = 2
        self._header_path = header_xml_path or str(
            Path(__file__).parent / 'header.xml'
        )
        # (bold, height_hu, color_hex) → charPr id
        self._charpr_map: dict = {}
        self._charpr_next = _CHARPR_BASE

        # (bin_id, img_bytes, ext_str, width_hu, height_hu)
        self._images: list = []
        self._bin_next = 1

    # ------------------------------------------------------------------
    # CharPr ID resolution
    # ------------------------------------------------------------------

    def _get_charpr(self, bold: bool, size_pt: float, color: str = '#000000') -> int:
        color = color.upper()
        # Use predefined IDs for standard black text sizes
        if color in ('#000000', '#000'):
            if size_pt >= 14:
                return CS_HEAD        # 16pt blue — special case kept
            if size_pt >= 10.5 and not bold and size_pt < 11.5:
                return CS_MED         # 11pt
            if size_pt < 9.5 and not bold:
                return CS_SMALL       # 9pt
            if size_pt < 10.5 and not bold:
                return CS_NORMAL      # 10pt

        height = max(100, round(size_pt * PT))
        key = (bold, height, color)
        if key not in self._charpr_map:
            self._charpr_map[key] = self._charpr_next
            self._charpr_next += 1
        return self._charpr_map[key]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_paragraph(
        self,
        text: str,
        bold: bool = False,
        size_pt: float = 10.0,
        align: str = 'JUSTIFY',
        page_break: bool = False,
        color: str = '#000000',
    ) -> None:
        self._items.append(('para', {
            'id':         self._pid,
            'text':       text,
            'char_pr':    self._get_charpr(bold, size_pt, color),
            'para_pr':    PP_BODY,
            'page_break': page_break,
        }))
        self._pid += 1

    def add_image(
        self,
        img_bytes: bytes,
        ext: str,
        width_pt: float,
        height_pt: float,
    ) -> None:
        if not img_bytes or width_pt <= 0 or height_pt <= 0:
            return
        ext = ext.lower()
        if ext == 'jpeg':
            ext = 'jpg'
        # Scale to fit content width
        w_hu = int(width_pt * PT)
        h_hu = int(height_pt * PT)
        if w_hu > CONTENT_W:
            scale = CONTENT_W / w_hu
            w_hu = CONTENT_W
            h_hu = int(h_hu * scale)

        bid = self._bin_next
        self._bin_next += 1
        self._images.append((bid, img_bytes, ext, w_hu, h_hu))
        self._items.append(('image', {
            'id':     self._pid,
            'bin_id': bid,
            'width':  w_hu,
            'height': h_hu,
            'ext':    ext,
        }))
        self._pid += 1

    def add_table(
        self,
        rows: List[List[str]],
        col_widths_mm: Optional[List[float]] = None,
        header_row: bool = False,
    ) -> None:
        if not rows:
            return
        n_cols = max(len(r) for r in rows)
        n_rows = len(rows)
        if col_widths_mm:
            col_widths = [int(w * MM) for w in col_widths_mm]
        else:
            cw = CONTENT_W // n_cols
            col_widths = [cw] * n_cols
        row_h = int(8 * MM)
        # ID 예약 순서: tbl_id → 셀 p ID들 → 외부 p ID
        # 이렇게 해야 이후 add_paragraph가 더 높은 ID를 받아 top-level p ID가 오름차순 유지
        tbl_id = self._pid
        self._pid += 1
        cell_pids = []
        for ri in range(n_rows):
            row_pids = []
            for ci in range(n_cols):
                row_pids.append(self._pid)
                self._pid += 1
            cell_pids.append(row_pids)
        outer_pid = self._pid
        self._pid += 1
        self._items.append(('table', {
            'id':         tbl_id,
            'outer_pid':  outer_pid,
            'cell_pids':  cell_pids,
            'rows':       rows,
            'n_rows':     n_rows,
            'n_cols':     n_cols,
            'col_widths': col_widths,
            'row_h':      row_h,
            'total_w':    sum(col_widths),
            'total_h':    row_h * n_rows,
            'header_row': header_row,
        }))

    def add_page_break(self) -> None:
        self.add_paragraph('', page_break=True)

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        header_raw = Path(self._header_path).read_text('utf-8')
        header_patched = _patch_header(header_raw, self._charpr_map, self._images)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            mi = zipfile.ZipInfo('mimetype')
            mi.compress_type = zipfile.ZIP_STORED
            zf.writestr(mi, 'application/hwp+zip')

            zf.writestr('META-INF/container.xml',  _CONTAINER_XML)
            zf.writestr('META-INF/container.rdf',  _CONTAINER_RDF)
            zf.writestr('META-INF/manifest.xml',   _MANIFEST_XML)
            zf.writestr('Contents/content.hpf',    _CONTENT_HPF)
            zf.writestr('Contents/header.xml',     header_patched.encode('utf-8'))
            zf.writestr('Contents/section0.xml',   self._section_xml().encode('utf-8'))
            zf.writestr('Preview/PrvText.txt',     self._preview_text().encode('utf-8'))
            zf.writestr('settings.xml',            _SETTINGS_XML)
            zf.writestr('version.xml',             _VERSION_XML)

            for bid, img_bytes, ext, _w, _h in self._images:
                zf.writestr(f'BinData/BIN{bid:03d}.{ext}', img_bytes)

        return buf.getvalue()

    # ------------------------------------------------------------------
    # XML builders
    # ------------------------------------------------------------------

    def _text_run_xml(self, text: str) -> str:
        """Convert text with tabs/newlines into HWPX run content."""
        if not text:
            return '      <hp:t/>'

        text = text.replace('\r\n', '\n').replace('\r', '\n')
        parts = []
        buffer = []

        def flush():
            if buffer:
                parts.append('      <hp:t>' + _e(''.join(buffer)) + '</hp:t>')
                buffer.clear()

        for ch in text:
            if ch == '\t':
                flush()
                parts.append('      <hp:tab/>')
            elif ch == '\n':
                flush()
                parts.append('      <hp:lineBreak/>')
            else:
                buffer.append(ch)
        flush()
        return '\n'.join(parts)

    def _para_xml(self, item: dict) -> str:
        pb = '1' if item['page_break'] else '0'
        return (
            f'  <hp:p id="{item["id"]}" paraPrIDRef="{item["para_pr"]}" '
            f'styleIDRef="0" pageBreak="{pb}" columnBreak="0" merged="0">\n'
            f'    <hp:run charPrIDRef="{item["char_pr"]}">\n'
            f'{self._text_run_xml(item["text"])}\n'
            f'    </hp:run>\n'
            f'  </hp:p>\n'
        )

    def _picture_xml(self, item: dict) -> str:
        pid = item['id']
        bid = item['bin_id']
        w   = item['width']
        h   = item['height']
        ext = item['ext']
        return (
            f'  <hp:p id="{pid}" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">\n'
            f'    <hp:run charPrIDRef="0">\n'
            f'      <hp:ctrl>\n'
            f'        <hp:picture treatAsChar="0" vertRelTo="PARA" horzRelTo="PARA" '
            f'vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0" '
            f'width="{w}" height="{h}" protect="0" lock="0" flip="NONE">\n'
            f'          <hp:sz width="{w}" height="{h}"/>\n'
            f'          <hp:pos vertRelTo="PARA" horzRelTo="PARA" vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>\n'
            f'          <hp:outMargin left="0" right="0" top="0" bottom="0"/>\n'
            f'          <hp:imgInfo>\n'
            f'            <hp:img pathRef="BinData/BIN{bid:03d}.{ext}" binItemIDRef="{bid}"/>\n'
            f'            <hp:imgEffect/>\n'
            f'          </hp:imgInfo>\n'
            f'          <hp:insideMargin left="0" right="0" top="0" bottom="0"/>\n'
            f'        </hp:picture>\n'
            f'      </hp:ctrl>\n'
            f'    </hp:run>\n'
            f'  </hp:p>\n'
        )

    def _table_xml(self, item: dict) -> str:
        rows       = item['rows']
        col_widths = item['col_widths']
        row_h      = item['row_h']
        n_cols     = item['n_cols']
        header_row = item['header_row']

        cell_pids = item['cell_pids']
        rows_xml = []
        for ri, row in enumerate(rows):
            is_header = header_row and ri == 0
            cells = []
            for ci in range(n_cols):
                text    = str(row[ci]) if ci < len(row) else ''
                char_pr = self._get_charpr(is_header, 10.0)
                cw      = col_widths[ci]
                cpid    = cell_pids[ri][ci]
                cells.append(
                    f'      <hp:tc borderFillIDRef="2">\n'
                    f'        <hp:cellAddr colAddr="{ci}" rowAddr="{ri}" colSpan="1" rowSpan="1"/>\n'
                    f'        <hp:cellSz width="{cw}" height="{row_h}"/>\n'
                    f'        <hp:cellMargin left="510" right="510" top="142" bottom="142"/>\n'
                    f'        <hp:subList>\n'
                    f'          <hp:p id="{cpid}" paraPrIDRef="0" styleIDRef="0" '
                    f'pageBreak="0" columnBreak="0" merged="0">\n'
                    f'            <hp:run charPrIDRef="{char_pr}">\n'
                    f'{self._text_run_xml(text)}\n'
                    f'            </hp:run>\n'
                    f'          </hp:p>\n'
                    f'        </hp:subList>\n'
                    f'      </hp:tc>\n'
                )
            rows_xml.append(f'    <hp:tr>\n{"".join(cells)}    </hp:tr>\n')

        outer_pid = item['outer_pid']
        tbl_xml = (
            f'      <hp:tbl id="{item["id"]}" rowCnt="{item["n_rows"]}" '
            f'colCnt="{n_cols}" cellSpacing="0" borderFillIDRef="2">\n'
            f'        <hp:sz width="{item["total_w"]}" height="{item["total_h"]}"/>\n'
            f'        <hp:pos treatAsChar="0" affectLSpacing="0" flowWithText="1" '
            f'allowOverlap="0" holdAnchorAndSO="0" vertRelTo="PARA" horzRelTo="PARA" '
            f'vertAlign="TOP" horzAlign="LEFT" vertOffset="0" horzOffset="0"/>\n'
            f'        <hp:outMargin left="0" right="0" top="0" bottom="0"/>\n'
            f'{"".join(rows_xml)}'
            f'      </hp:tbl>\n'
        )
        return (
            f'  <hp:p id="{outer_pid}" paraPrIDRef="0" styleIDRef="0" '
            f'pageBreak="0" columnBreak="0" merged="0">\n'
            f'    <hp:run charPrIDRef="0">\n'
            f'{tbl_xml}'
            f'    </hp:run>\n'
            f'  </hp:p>\n'
        )

    def _section_xml(self) -> str:
        parts = [
            "<?xml version='1.0' encoding='UTF-8'?>\n",
            f'<hs:sec {_NS}>\n',
            _FIRST_PARA + '\n',
        ]
        for typ, item in self._items:
            if typ == 'para':
                parts.append(self._para_xml(item))
            elif typ == 'table':
                parts.append(self._table_xml(item))
            elif typ == 'image':
                parts.append(self._picture_xml(item))
        parts.append('</hs:sec>\n')
        return ''.join(parts)

    def _preview_text(self) -> str:
        lines = []
        for typ, item in self._items:
            if typ == 'para' and item['text']:
                lines.append(item['text'])
            elif typ == 'table':
                for row in item['rows']:
                    lines.append('\t'.join(str(c) for c in row))
        return '\n'.join(lines[:100])
