FROM python:3.12-slim

# Prevent Python from writing pyc files and enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates

# Download the latest installer
ADD https://astral.sh/uv/install.sh /uv-installer.sh

# Run the installer then remove it
RUN sh /uv-installer.sh && rm /uv-installer.sh

## Ensure the installed binary is on the `PATH`
ENV PATH="/root/.local/bin/:$PATH"

WORKDIR /app

# Copy project files
COPY . /app/

# Install dependencies
RUN uv sync --locked

# Expose Django's default port
EXPOSE 8000

# Run the Django prod server
# CMD ["sleep", "infinity"]
CMD ["uv", "run", "hypercorn", "--bind", "0.0.0.0:8000", "settledown.asgi:application"]
