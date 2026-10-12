# platmon in a container. It monitors the host, so it needs the host mounts in deploy/docker/compose.yaml
# (plus deploy/docker/compose.jetson.yaml on Jetson); see README "Docker".
# Build it yourself; images are not published (the base image contains GPL/LGPL software,
# see README "Container image licensing").
# Plain Python image: platmon only reads /proc and /sys, so no NVIDIA runtime or L4T base is needed.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /opt/platmon
COPY platmon.py platmon.ini ./
COPY collector ./collector
COPY runtime ./runtime
COPY frontends ./frontends

# everything platmon reads in /sys and /proc is world-readable; no root needed
USER 65534:65534
EXPOSE 9797
# /api/stats answers 503 while there is no current data, which marks the container unhealthy.
# Follow the configured bind/port, including isolated checks on a non-default host port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python3", "-c", "import urllib.request; from platmon import load_config; h = load_config('/opt/platmon/platmon.ini')['http']; host = '127.0.0.1' if h['bind'] == '0.0.0.0' else h['bind']; urllib.request.urlopen('http://' + host + ':' + h['port'] + '/api/stats', timeout=4)"]
ENTRYPOINT ["python3", "/opt/platmon/platmon.py"]
CMD ["/opt/platmon/platmon.ini"]
