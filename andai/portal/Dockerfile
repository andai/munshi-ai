# syntax=docker/dockerfile:1
# andai-portal — build context is andai/portal.

FROM node:22-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-bookworm-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-bookworm-slim
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=3000 \
    ANDAI_VERSION=dev \
    ANDAI_CORE_URL=http://andai-core:8000
COPY --from=build --chown=node:node /app/.next/standalone ./
COPY --from=build --chown=node:node /app/.next/static ./.next/static
# Rewrites X-Forwarded-For to the address the portal believes before Next sees a request.
COPY --from=build --chown=node:node /app/lib/client-ip.cjs ./client-ip.cjs
USER node
EXPOSE 3000
CMD ["node", "--require", "./client-ip.cjs", "server.js"]
