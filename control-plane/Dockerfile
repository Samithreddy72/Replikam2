# Fleet control plane for Fly.io — backend API + the built admin panel in one image.
# Build context is control-plane/ (so both backend/ and panel-dist/ are visible).
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
# The panel's static files. main.py serves them from ../panel-dist relative to app/,
# i.e. /app/panel-dist here, so "/" shows the admin UI instead of 404.
COPY panel-dist ./panel-dist

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
