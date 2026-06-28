# Production image for the Aalborg DK1 energy dashboard.
# Built on the server (amd64 native), so no --platform flag is needed.
# Python 3.14 to match the version-pins in requirements.txt.
FROM python:3.14-slim

WORKDIR /app

# psycopg[binary] bundles its own libpq, so no extra apt packages are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8050

# Production WSGI server, replacing app.run(debug=True). `app:server` is the
# Flask server Dash exposes (see `server = app.server` in app.py).
# --preload loads the dataset once in the master and shares it with workers via
# copy-on-write, instead of re-loading it in every worker.
CMD ["gunicorn", "app:server", \
     "--bind", "0.0.0.0:8050", \
     "--workers", "2", \
     "--timeout", "120", \
     "--preload"]
