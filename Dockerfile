# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.9.6

FROM python:${PYTHON_VERSION}-slim

LABEL fly_launch_runtime="flask"

WORKDIR /code

COPY requirements.txt requirements.txt
RUN pip3 install -r requirements.txt

COPY . .

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]

EXPOSE 8080

CMD ["python3", "app.py"]
