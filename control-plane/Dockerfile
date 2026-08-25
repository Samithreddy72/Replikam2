# Fleet control plane — backend API + the built admin panel in one image.
# (Header said Fly.io; it has been deployed on AWS Lightsail via
#  control-plane/deploy/aws/deploy.sh since the migration.)
# Build context is control-plane/ (so both backend/ and panel-dist/ are visible).
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# The panel's static files. main.py serves them from ../panel-dist relative to app/,
# i.e. /app/panel-dist here, so "/" shows the admin UI instead of 404.
COPY panel-dist ./panel-dist

# BUILD IDENTITY.
#
# Passed in by deploy.sh and surfaced by /healthz, so the running control plane can say which
# commit it is. Without this the only way to establish what production was running was to probe
# its behaviour -- which is how /docs was found still exposed days after being closed in source.
#
# Declared AFTER the COPY layers on purpose: these change on every deploy, and putting them
# earlier would invalidate the pip-install layer each time and make every deploy a full rebuild.
ARG CONTROL_PLANE_GIT_SHA=unknown
ARG CONTROL_PLANE_BUILD_ID=unknown
ARG CONTROL_PLANE_BUILT_AT=unknown
ENV CONTROL_PLANE_GIT_SHA=$CONTROL_PLANE_GIT_SHA \
    CONTROL_PLANE_BUILD_ID=$CONTROL_PLANE_BUILD_ID \
    CONTROL_PLANE_BUILT_AT=$CONTROL_PLANE_BUILT_AT

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
