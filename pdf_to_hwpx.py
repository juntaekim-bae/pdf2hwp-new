#!/usr/bin/env python3
"""
PDF → HWPX 일괄 변환기
사용법:
  python3 pdf_to_hwpx.py input.pdf output.hwp          # 단일 파일
  python3 pdf_to_hwpx.py input_folder/ output_folder/   # 폴더 일괄 변환
"""
import sys
import logging
from pathlib import Path
from typing import List, Tuple

try:
    import fitz  # PyMuPDF
except ImportError:
    sys.exit("PyMuPDF가 설치되지 않았습니다. 먼저 실행하세요: pip3 install pymupdf")

from hwp5_bin_writer import HWP5Writer
from hwpx_writer import _color_hex

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 설정값
# ---------------------------------------------------------------------------
HEADING_SIZE_RATIO = 1.25   # 평균 글꼴 크기의 이 배수 이상이면 제목으로 판단
MIN_TEXT_LEN = 1            # 공백 제거 후 이 길이 미만 블록은 무시
MIN_IMAGE_AREA_PT = 40 * 40 # 이 면적 미만 이미지는 무시 (장식용 아이콘 등)
MAX_IMAGE_RATIO   = 0.85    # 페이지 면적의 이 비율 이상이면 배경 이미지로 간주, 무시


# ---------------------------------------------------------------------------
# PDF 분석 — 텍스트
# ---------------------------------------------------------------------------

def _avg_font_size(page: fitz.Page) -> float:
    """페이지에서 중앙값 글꼴 크기를 반환 (이상치 영향 감소)."""
    sizes = []
    for block in page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            for span in line['spans']:
                if span['text'].strip():
                    sizes.append(span['size'])
    if not sizes:
        return 10.0
    sizes.sort()
    return sizes[len(sizes) // 2]


def _is_bold(flags: int) -> bool:
    return bool(flags & 2**4)


def _dominant_color(line: dict) -> str:
    """줄에서 가장 긴 span의 색상을 반환."""
    best_text = ''
    best_color = 0
    for span in line['spans']:
        if len(span['text']) > len(best_text):
            best_text = span['text']
            best_color = span.get('color', 0)
    return _color_hex(best_color)


def _extract_text_blocks(page: fitz.Page) -> List[dict]:
    """
    페이지에서 텍스트 블록을 추출.
    반환: [{'text', 'size', 'bold', 'color', 'y0', 'x0', 'base_size'}]
    """
    base_size = _avg_font_size(page)
    blocks_raw = page.get_text('dict', flags=fitz.TEXT_PRESERVE_WHITESPACE)['blocks']

    result = []
    for block in blocks_raw:
        if block['type'] != 0:
            continue

        for line in block['lines']:
            line_text = ''
            line_size = 10.0
            line_bold = False

            for span in line['spans']:
                t = span['text']
                if not t:
                    continue
                line_text += t
                line_size = max(line_size, span['size'])
                if _is_bold(span['flags']):
                    line_bold = True

            line_text = line_text.strip()
            if len(line_text) < MIN_TEXT_LEN:
                continue

            result.append({
                'text':      line_text,
                'size':      line_size,
                'bold':      line_bold,
                'color':     _dominant_color(line),
                'y0':        line['bbox'][1],
                'x0':        line['bbox'][0],
                'base_size': base_size,
            })

    return result


# ---------------------------------------------------------------------------
# PDF 분석 — 이미지
# ---------------------------------------------------------------------------

def _extract_images(page: fitz.Page, doc: fitz.Document) -> List[dict]:
    """
    페이지에서 이미지를 추출.
    반환: [{'bytes', 'ext', 'width_pt', 'height_pt', 'y0'}]
    """
    result = []
    page_area = page.rect.width * page.rect.height
    seen = set()

    try:
        img_list = page.get_images(full=True)
    except Exception:
        return []

    for img in img_list:
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)

        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue

        for rect in rects:
            w_pt = rect.width
            h_pt = rect.height
            area = w_pt * h_pt

            if area < MIN_IMAGE_AREA_PT:
                continue
            if area > page_area * MAX_IMAGE_RATIO:
                continue

            try:
                img_data = doc.extract_image(xref)
            except Exception:
                continue

            result.append({
                'bytes':    img_data['image'],
                'ext':      img_data.get('ext', 'png'),
                'width_pt': w_pt,
                'height_pt': h_pt,
                'y0':       rect.y0,
            })

    return result


# ---------------------------------------------------------------------------
# PDF 분석 — 표
# ---------------------------------------------------------------------------

def _detect_tables(page: fitz.Page) -> List[dict]:
    try:
        tabs = page.find_tables()
        result = []
        for tab in tabs:
            rows = []
            for row in tab.rows:
                cells = [c.text.strip() if c else '' for c in row.cells]
                rows.append(cells)
            if rows:
                result.append({'bbox': tab.bbox, 'rows': rows})
        return result
    except Exception:
        return []


def _text_in_table(y0: float, tables: List[dict]) -> bool:
    for tbl in tables:
        bbox = tbl['bbox']
        if bbox[1] - 5 <= y0 <= bbox[3] + 5:
            return True
    return False


# ---------------------------------------------------------------------------
# 변환 핵심 로직
# ---------------------------------------------------------------------------

def convert_page(
    writer: HWP5Writer,
    page: fitz.Page,
    doc: fitz.Document,
    is_first: bool,
) -> None:
    """한 페이지를 HWP5 writer에 추가."""
    tables      = _detect_tables(page)
    text_blocks = _extract_text_blocks(page)
    images      = _extract_images(page, doc)

    # 표 영역 안의 텍스트 블록 제외
    filtered_text = [b for b in text_blocks if not _text_in_table(b['y0'], tables)]

    base = text_blocks[0]['base_size'] if text_blocks else 10.0

    # y 순서로 정렬된 모든 요소를 하나의 리스트로 합치기
    elements = []
    for b in filtered_text:
        elements.append(('text',  b['y0'], b))
    for tbl in tables:
        elements.append(('table', tbl['bbox'][1], tbl))
    for img in images:
        elements.append(('image', img['y0'], img))

    elements.sort(key=lambda x: x[1])

    first_item = True
    for kind, _y, item in elements:
        need_pb = (not is_first) and first_item
        first_item = False

        if kind == 'table':
            if need_pb:
                writer.add_paragraph('', page_break=True)
            writer.add_table(item['rows'], header_row=True)

        elif kind == 'image':
            if need_pb:
                writer.add_paragraph('', page_break=True)
            writer.add_image(
                item['bytes'],
                item['ext'],
                item['width_pt'],
                item['height_pt'],
            )

        else:  # text
            b = item
            is_heading = b['size'] >= base * HEADING_SIZE_RATIO
            writer.add_paragraph(
                b['text'],
                bold=b['bold'] or is_heading,
                size_pt=b['size'],
                page_break=need_pb,
                color=b['color'],
            )


def convert_pdf(input_path: str, output_path: str) -> bool:
    """PDF 파일 하나를 HWP5로 변환. 성공 시 True."""
    try:
        doc = fitz.open(input_path)
    except Exception as e:
        log.error("PDF 열기 실패: %s — %s", input_path, e)
        return False

    writer = HWP5Writer()
    total = doc.page_count
    log.info("변환 시작: %s  (%d 페이지)", Path(input_path).name, total)

    for i, page in enumerate(doc):
        try:
            convert_page(writer, page, doc, is_first=(i == 0))
            log.info("  페이지 %d/%d 완료", i + 1, total)
        except Exception as e:
            log.warning("  페이지 %d 처리 중 오류 (건너뜀): %s", i + 1, e)

    doc.close()

    try:
        writer.save(output_path)
        size_kb = Path(output_path).stat().st_size // 1024
        log.info("저장 완료: %s  (%d KB)", output_path, size_kb)
        return True
    except Exception as e:
        log.error("저장 실패: %s — %s", output_path, e)
        return False


def batch_convert(input_dir: str, output_dir: str) -> Tuple[int, int]:
    """폴더 내 모든 PDF를 일괄 변환. (성공 수, 실패 수) 반환."""
    pdfs = sorted(Path(input_dir).glob('**/*.pdf'))
    if not pdfs:
        log.warning("PDF 파일을 찾을 수 없습니다: %s", input_dir)
        return 0, 0

    ok = err = 0
    for pdf in pdfs:
        rel = pdf.relative_to(input_dir)
        out = Path(output_dir) / rel.with_suffix('.hwp')
        if convert_pdf(str(pdf), str(out)):
            ok += 1
        else:
            err += 1

    return ok, err


# ---------------------------------------------------------------------------
# CLI 진입점
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])

    if src.is_dir():
        ok, err = batch_convert(str(src), str(dst))
        print(f"\n완료: 성공 {ok}개, 실패 {err}개")
    elif src.is_file() and src.suffix.lower() == '.pdf':
        if dst.is_dir():
            dst = dst / src.with_suffix('.hwp').name
        success = convert_pdf(str(src), str(dst))
        sys.exit(0 if success else 1)
    else:
        print(f"오류: {src} 는 PDF 파일이나 폴더여야 합니다.")
        sys.exit(1)


if __name__ == '__main__':
    main()
