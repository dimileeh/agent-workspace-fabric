FROM node:22-bookworm-slim AS deps

WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1

COPY apps/console/package.json apps/console/package-lock.json ./
COPY apps/console/scripts ./scripts
RUN npm ci

FROM deps AS build

# Public path/config baked at build time for local or hosted console modes.
# Do NOT pass runtime secrets (AWF_API_TOKEN, etc.) as ARG — inject those at runtime.
ARG NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=5000
ARG NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=
ARG NEXT_PUBLIC_AWF_CONSOLE_API_BASE=/api/awf
ARG NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=/api/operator
ENV NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=${NEXT_PUBLIC_AWF_CONSOLE_POLL_MS}
ENV NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=${NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH}
ENV NEXT_PUBLIC_AWF_CONSOLE_API_BASE=${NEXT_PUBLIC_AWF_CONSOLE_API_BASE}
ENV NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=${NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE}

COPY apps/console ./
RUN npm run build
RUN npm prune --omit=dev

FROM node:22-bookworm-slim AS runtime

WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=3000

# next.config.ts derives basePath from env at `next start`; carry the same
# public build-time path/config into the runtime image so hosted builds stay
# consistent without requiring operators to re-supply these at launch.
ARG NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=5000
ARG NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=
ARG NEXT_PUBLIC_AWF_CONSOLE_API_BASE=/api/awf
ARG NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=/api/operator
ENV NEXT_PUBLIC_AWF_CONSOLE_POLL_MS=${NEXT_PUBLIC_AWF_CONSOLE_POLL_MS}
ENV NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH=${NEXT_PUBLIC_AWF_CONSOLE_BASE_PATH}
ENV NEXT_PUBLIC_AWF_CONSOLE_API_BASE=${NEXT_PUBLIC_AWF_CONSOLE_API_BASE}
ENV NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE=${NEXT_PUBLIC_AWF_CONSOLE_OPERATOR_BASE}

COPY --from=build /app/package.json ./package.json
COPY --from=build /app/next.config.ts ./next.config.ts
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/.next ./.next

EXPOSE 3000

CMD ["./node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
