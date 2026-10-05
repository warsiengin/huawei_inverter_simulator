ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip

WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

COPY inverter_emulator.py /app/inverter_emulator.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

ENV PATH="/opt/venv/bin:${PATH}"

CMD ["/run.sh"]
