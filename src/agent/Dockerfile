# AgentCore Runtime requires linux/arm64 images. CDK builds this with platform=LINUX_ARM64.
FROM public.ecr.aws/docker/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY support_agent ./support_agent

# Least privilege inside the microVM: never run the agent as root.
RUN useradd --create-home --uid 10001 agent
USER agent

EXPOSE 8080

# opentelemetry-instrument (AWS distro) exports Strands + custom spans to CloudWatch / X-Ray.
CMD ["opentelemetry-instrument", "python", "-m", "support_agent.app"]
