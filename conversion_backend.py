import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pdf_to_hwpx import convert_pdf as local_convert_pdf

log = logging.getLogger(__name__)


class ConversionError(Exception):
    pass


class ConversionBackend:
    name = 'unknown'

    def convert(self, input_path: str, output_path: str) -> None:
        raise NotImplementedError


class LocalBackend(ConversionBackend):
    name = 'local'

    def convert(self, input_path: str, output_path: str) -> None:
        success = local_convert_pdf(input_path, output_path)
        if not success or not Path(output_path).exists():
            raise ConversionError('로컬 변환에 실패했습니다.')


class ExternalAPIBackend(ConversionBackend):
    name = 'external'

    def __init__(self) -> None:
        self.url = os.environ.get('EXTERNAL_CONVERTER_URL')
        self.token = os.environ.get('EXTERNAL_CONVERTER_TOKEN')
        self.secret = os.environ.get('EXTERNAL_CONVERTER_SECRET')
        self.params = self._load_extra_params()
        if not self.url:
            raise ConversionError('EXTERNAL_CONVERTER_URL이 설정되지 않았습니다.')

    def _load_extra_params(self) -> Dict[str, Any]:
        raw = os.environ.get('EXTERNAL_CONVERTER_PARAMS', '{}').strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning('EXTERNAL_CONVERTER_PARAMS를 JSON으로 파싱할 수 없습니다. 기본값으로 빈 dict를 사용합니다.')
            return {}

    def convert(self, input_path: str, output_path: str) -> None:
        try:
            import requests
        except ImportError as exc:
            raise ConversionError('requests 패키지가 설치되지 않았습니다.') from exc

        files = {
            'file': (
                Path(input_path).name,
                open(input_path, 'rb'),
                'application/pdf',
            )
        }

        data: Dict[str, Any] = {
            'output': Path(output_path).suffix.lstrip('.'),
        }
        data.update(self.params)

        headers: Dict[str, str] = {
            'Accept': 'application/octet-stream',
        }
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        if self.secret:
            headers['X-API-SECRET'] = self.secret

        log.info('외부 변환 API 호출: %s', self.url)

        try:
            import requests
            response = requests.post(
                self.url,
                headers=headers,
                files=files,
                data=data,
                timeout=600,
            )
        finally:
            files['file'][1].close()

        if response.status_code != 200:
            raise ConversionError(
                f'외부 API 응답 오류: {response.status_code} {response.reason} {response.text[:200]}'
            )

        if not response.content:
            raise ConversionError('외부 API가 빈 결과를 반환했습니다.')

        Path(output_path).write_bytes(response.content)


def create_backend() -> ConversionBackend:
    external_url = os.environ.get('EXTERNAL_CONVERTER_URL')
    if external_url:
        try:
            return ExternalAPIBackend()
        except ConversionError as exc:
            log.warning('외부 변환 백엔드 초기화 실패: %s', exc)
    return LocalBackend()


def convert_pdf(input_path: str, output_path: str) -> tuple[bool, str]:
    backend = create_backend()
    try:
        backend.convert(input_path, output_path)
        return True, backend.name
    except ConversionError as exc:
        log.warning('백엔드 %s 변환 실패: %s', backend.name, exc)
        if backend.name != 'local':
            log.info('로컬 변환으로 재시도합니다.')
            try:
                LocalBackend().convert(input_path, output_path)
                return True, 'local-fallback'
            except ConversionError as local_exc:
                log.error('로컬 변환도 실패했습니다: %s', local_exc)
        return False, backend.name
