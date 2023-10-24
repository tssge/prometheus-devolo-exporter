FROM ubuntu:20.04

RUN apt-get update
RUN apt-get install -y python3.9
RUN apt-get install -y python3-pip

COPY requirements.txt /app/requirements.txt
COPY devolo_exporter /app/devolo_exporter
COPY run.py /app/run.py
COPY resources /app/resources
RUN /usr/bin/python3.9 -m pip install -r /app/requirements.txt

EXPOSE 5642

VOLUME ["/app/config.yml"]
ENTRYPOINT ["/usr/bin/python3.9", "/app/run.py"]
CMD ["/app/config.yml"]