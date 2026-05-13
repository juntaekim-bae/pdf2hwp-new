import os
from pathlib import Path
from tempfile import TemporaryDirectory

from flask import Flask, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from pdf_to_hwpx import convert_pdf

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'change-this-secret')
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

ALLOWED_EXTENSIONS = {'pdf'}


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/pdf2hwpx')
def pdf2hwpx_redirect():
    return redirect(url_for('index'))


@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'pdf_file' not in request.files:
            flash('PDF 파일을 선택해주세요.')
            return redirect(url_for('index'))

        uploaded_file = request.files['pdf_file']
        if uploaded_file.filename == '':
            flash('파일 이름이 비어 있습니다.')
            return redirect(url_for('index'))

        if not allowed_file(uploaded_file.filename):
            flash('PDF 파일만 업로드할 수 있습니다.')
            return redirect(url_for('index'))

        filename = secure_filename(uploaded_file.filename)
        with TemporaryDirectory() as tmpdir:
            input_path = Path(tmpdir) / filename
            uploaded_file.save(input_path)

            output_name = input_path.with_suffix('.hwpx').name
            output_path = Path(tmpdir) / output_name

            success = convert_pdf(str(input_path), str(output_path))
            if not success or not output_path.exists():
                flash('PDF 변환에 실패했습니다. 업로드한 파일이 유효한지 확인하세요.')
                return redirect(url_for('index'))

            return send_file(
                output_path,
                as_attachment=True,
                download_name=output_name,
                mimetype='application/octet-stream',
            )

    return render_template('index.html')


@app.errorhandler(413)
def request_entity_too_large(error):
    flash('업로드 가능한 파일 크기는 100MB 이하입니다.')
    return redirect(url_for('index'))


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
