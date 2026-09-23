FROM python:3.12-alpine
WORKDIR /app
RUN apk add --no-cache tzdata
RUN pip install --no-cache-dir flask docker
COPY app.py .
EXPOSE 5000
CMD ["python", "app.py"]
