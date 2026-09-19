FROM python:3.11-slim

LABEL maintainer="EMUNEL" \
      description="EMUNEL Core — Multi-protocol proxy runtime"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY emunel_core/ ./emunel_core/

EXPOSE 443

CMD ["python", "-m", "emunel_core"]
