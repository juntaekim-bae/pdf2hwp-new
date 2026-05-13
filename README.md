# pdf2hwp

PDF를 HWPX/HWP로 변환하는 Flask 웹 서비스입니다.

## 실행

1. 의존성 설치

```bash
pip install -r requirements.txt
```

2. 서비스 실행

```bash
python app.py
```

3. 브라우저에서 `http://localhost:5000` 접속

## 변환 백엔드

이 프로젝트는 다음 세 가지 백엔드 중 하나를 선택합니다.

1. `HANCOM_API_URL`이 설정된 경우 한컴 API 백엔드 사용
2. `EXTERNAL_CONVERTER_URL`이 설정된 경우 외부 변환 API 사용
3. 기본적으로 로컬 PDF→HWPX 변환 사용

### 한컴 API 설정

한컴 통합문서뷰어/문서 변환 API를 사용하는 경우 다음 환경 변수를 설정합니다.

- `HANCOM_API_URL`: 한컴 변환 서버 기본 URL (`http://host:8101`) or 미들웨어 URL
- `HANCOM_API_MODULE`: 모듈 코드 (기본값 `common`)
- `HANCOM_API_FUNCTION`: 변환 API 이름 (기본값 `pdf2hwpx`)
- `HANCOM_API_METHOD`: `POST` 또는 `GET` (기본값 `POST`)
- `HANCOM_API_UPLOAD`: 파일 업로드 모드 사용 여부 (`true` 또는 `false`, 기본값 `true`)
- `HANCOM_API_KEY`: 필요할 경우 API 키
- `HANCOM_API_PARAMS`: 추가 매개변수를 JSON 문자열로 전달

예시:

```bash
export HANCOM_API_URL="http://hancom-server:8101"
export HANCOM_API_MODULE="common"
export HANCOM_API_FUNCTION="pdf2hwpx"
export HANCOM_API_METHOD="POST"
export HANCOM_API_UPLOAD="true"
```

> 참고: 공개 문서에서는 PDF → HWP/HWPX 직접 변환 함수명이 명시되어 있지 않습니다. 한컴 측에 실제 모듈 코드와 변환 API 이름을 확인해야 합니다.

### 외부 변환 API 설정

외부 서비스가 이미 파일 업로드 형태의 변환 API를 제공하는 경우 다음 변수를 설정합니다.

- `EXTERNAL_CONVERTER_URL`
- `EXTERNAL_CONVERTER_TOKEN` 또는 `EXTERNAL_CONVERTER_SECRET` (옵션)
- `EXTERNAL_CONVERTER_PARAMS` (추가 파라미터, JSON 문자열)

### 로컬 변환

한컴 API나 외부 API가 없으면 기본적으로 로컬 변환을 시도합니다. 이 로컬 변환은 `pdf_to_hwpx.py`의 PDF 분석 및 HWPX 생성 로직을 사용합니다.

## 디버그

변환 실패 시 웹 UI에서 오류 메시지를 확인하고, 서버 로그를 통해 백엔드 이름과 에러 원인을 확인하세요.
