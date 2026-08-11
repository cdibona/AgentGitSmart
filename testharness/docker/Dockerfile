FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates python3 \
 && rm -rf /var/lib/apt/lists/*
RUN git config --global protocol.allow always \
 && git config --global init.defaultBranch main \
 && git config --global user.email "agent@agentgitsmart" \
 && git config --global user.name "AgentGitSmart Agent"
WORKDIR /pack
