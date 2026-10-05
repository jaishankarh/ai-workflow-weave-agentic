FROM python:3.12-slim
RUN pip install --no-cache-dir "redis>=5,<6"
COPY app.py /app.py
EXPOSE 8080
CMD ["python", "-u", "/app.py"]
